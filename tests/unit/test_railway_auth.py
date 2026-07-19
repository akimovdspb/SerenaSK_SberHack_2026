from __future__ import annotations

import secrets

from fastapi.testclient import TestClient

from railway.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    AuthConfig,
    _derive_password,
    _read_session,
    create_app,
)


def _config(*, max_failures: int = 5) -> AuthConfig:
    salt = secrets.token_bytes(16)
    return AuthConfig(
        username="demo_user",
        password_salt=salt,
        password_digest=_derive_password("synthetic-password", salt),
        session_secret=secrets.token_bytes(48),
        cookie_secure=False,
        session_ttl_seconds=600,
        max_failures=max_failures,
        failure_window_seconds=300,
    )


def test_cookie_session_login_csrf_and_logout() -> None:
    with TestClient(create_app(_config())) as client:
        assert client.get("/auth/session").json() == {"authenticated": False}
        logged_in = client.post(
            "/auth/login",
            json={"username": "demo_user", "password": "synthetic-password"},
            headers={"X-CF-Client-IP": "192.0.2.10"},
        )

        assert logged_in.status_code == 200
        session_cookie = next(
            value
            for value in logged_in.headers.get_list("set-cookie")
            if value.startswith(f"{SESSION_COOKIE}=")
        )
        assert "HttpOnly" in session_cookie
        assert "SameSite=lax" in session_cookie
        assert client.get("/auth/session").json() == {
            "authenticated": True,
            "username": "demo_user",
        }
        verified = client.get(
            "/auth/verify",
            headers={"X-Forwarded-Method": "GET", "X-Forwarded-Uri": "/"},
        )
        assert verified.status_code == 204
        assert verified.headers["X-CF-Actor"] == "demo_user"
        assert verified.headers["X-CF-Actor-Role"] == "human"

        missing_csrf = client.get(
            "/auth/verify",
            headers={"X-Forwarded-Method": "POST", "X-Forwarded-Uri": "/api/v1/campaigns"},
        )
        assert missing_csrf.status_code == 403
        csrf = client.cookies.get(CSRF_COOKIE)
        assert csrf
        mutation = client.get(
            "/auth/verify",
            headers={
                "X-Forwarded-Method": "POST",
                "X-Forwarded-Uri": "/api/v1/campaigns",
                "X-CF-CSRF": csrf,
            },
        )
        assert mutation.status_code == 204
        assert client.post("/auth/logout", headers={"X-CF-CSRF": csrf}).status_code == 200
        assert client.get("/auth/session").json() == {"authenticated": False}


def test_html_request_redirects_to_login_and_api_request_stays_unauthorized() -> None:
    with TestClient(create_app(_config()), follow_redirects=False) as client:
        html = client.get(
            "/auth/verify",
            headers={
                "Accept": "text/html",
                "X-Forwarded-Method": "GET",
                "X-Forwarded-Uri": "/campaigns/cmp_123?tab=email",
            },
        )
        api = client.get(
            "/auth/verify",
            headers={
                "Accept": "application/json",
                "X-Forwarded-Method": "POST",
                "X-Forwarded-Uri": "/api/v1/campaigns",
            },
        )

        assert html.status_code == 303
        assert html.headers["location"].startswith("/login?next=")
        assert api.status_code == 401


def test_failed_login_is_rate_limited_without_revealing_which_field_failed() -> None:
    with TestClient(create_app(_config(max_failures=2))) as client:
        messages: list[str] = []
        for username, password in (("wrong", "synthetic-password"), ("demo_user", "wrong")):
            response = client.post(
                "/auth/login",
                json={"username": username, "password": password},
                headers={"X-CF-Client-IP": "192.0.2.20"},
            )
            messages.append(response.json()["detail"])
            assert response.status_code == 401
        blocked = client.post(
            "/auth/login",
            json={"username": "demo_user", "password": "synthetic-password"},
            headers={"X-CF-Client-IP": "192.0.2.20"},
        )

        assert len(set(messages)) == 1
        assert blocked.status_code == 429


def test_malformed_non_ascii_session_token_is_rejected() -> None:
    assert _read_session(_config(), "не-base64.signature", now=0) is None
