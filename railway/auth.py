from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

SESSION_COOKIE = "cf_session"
CSRF_COOKIE = "cf_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _decode_secret(value: str, *, name: str, minimum: int) -> bytes:
    try:
        encoded = value.encode("ascii")
        decoded = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError(f"{name} is invalid") from exc
    if len(decoded) < minimum:
        raise RuntimeError(f"{name} is too short")
    return decoded


@dataclass(frozen=True)
class AuthConfig:
    username: str
    password_salt: bytes
    password_digest: bytes
    session_secret: bytes
    cookie_secure: bool = True
    session_ttl_seconds: int = 8 * 60 * 60
    max_failures: int = 5
    failure_window_seconds: int = 5 * 60

    @classmethod
    def from_environment(cls) -> AuthConfig:
        username = str(os.environ.get("AUTH_USERNAME") or "").strip()
        if not username:
            raise RuntimeError("AUTH_USERNAME is missing")
        return cls(
            username=username,
            password_salt=_decode_secret(
                str(os.environ.get("AUTH_PASSWORD_SALT") or ""),
                name="AUTH_PASSWORD_SALT",
                minimum=16,
            ),
            password_digest=_decode_secret(
                str(os.environ.get("AUTH_PASSWORD_DIGEST") or ""),
                name="AUTH_PASSWORD_DIGEST",
                minimum=32,
            ),
            session_secret=_decode_secret(
                str(os.environ.get("AUTH_SESSION_SECRET") or ""),
                name="AUTH_SESSION_SECRET",
                minimum=32,
            ),
            cookie_secure=str(os.environ.get("AUTH_COOKIE_SECURE") or "true").lower() == "true",
        )


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class FailureLimiter:
    def __init__(self, *, maximum: int, window_seconds: int) -> None:
        self._maximum = maximum
        self._window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_blocked(self, key: str, now: float) -> bool:
        with self._lock:
            recent = [
                timestamp
                for timestamp in self._failures.get(key, [])
                if now - timestamp < self._window_seconds
            ]
            if recent:
                self._failures[key] = recent
            else:
                self._failures.pop(key, None)
            return len(recent) >= self._maximum

    def record_failure(self, key: str, now: float) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def _derive_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


def _session_token(config: AuthConfig, *, csrf: str, now: int) -> str:
    payload = _b64(
        json.dumps(
            {
                "v": 1,
                "sub": config.username,
                "csrf": csrf,
                "iat": now,
                "exp": now + config.session_ttl_seconds,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signature = _b64(hmac.digest(config.session_secret, payload.encode("ascii"), "sha256"))
    return f"{payload}.{signature}"


def _read_session(config: AuthConfig, token: str | None, *, now: int) -> dict[str, Any] | None:
    if not token or token.count(".") != 1:
        return None
    payload, supplied_signature = token.split(".", 1)
    try:
        encoded_payload = payload.encode("ascii")
    except UnicodeEncodeError:
        return None
    expected_signature = _b64(hmac.digest(config.session_secret, encoded_payload, "sha256"))
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None
    try:
        value = json.loads(_unb64(payload))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if (
        value.get("v") != 1
        or value.get("sub") != config.username
        or not isinstance(value.get("csrf"), str)
        or not isinstance(value.get("iat"), int)
        or not isinstance(value.get("exp"), int)
        or value["iat"] > now + 30
        or value["exp"] <= now
        or value["exp"] - value["iat"] != config.session_ttl_seconds
    ):
        return None
    return value


def _client_key(request: Request) -> str:
    value = str(request.headers.get("x-cf-client-ip") or "unknown").strip()
    return value[:128] or "unknown"


def _safe_next(request: Request) -> str:
    raw = str(request.headers.get("x-forwarded-uri") or "/")
    parsed = urlsplit(raw)
    candidate = parsed.path or "/"
    if parsed.query:
        candidate = f"{candidate}?{parsed.query}"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate[:2_000]


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def create_app(config: AuthConfig | None = None) -> FastAPI:
    effective = config or AuthConfig.from_environment()
    limiter = FailureLimiter(
        maximum=effective.max_failures,
        window_seconds=effective.failure_window_seconds,
    )
    app = FastAPI(
        title="Communication Factory session gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/auth/health")
    async def health() -> Response:
        return _no_store(JSONResponse({"status": "ok"}))

    @app.post("/auth/login")
    async def login(payload: LoginRequest, request: Request) -> Response:
        now_float = time.time()
        key = _client_key(request)
        if limiter.is_blocked(key, now_float):
            return _no_store(
                JSONResponse(
                    {"detail": "Слишком много попыток. Повторите вход через несколько минут."},
                    status_code=429,
                    headers={"Retry-After": str(effective.failure_window_seconds)},
                )
            )
        supplied = _derive_password(payload.password, effective.password_salt)
        username_valid = hmac.compare_digest(payload.username, effective.username)
        password_valid = hmac.compare_digest(supplied, effective.password_digest)
        valid = username_valid and password_valid
        if not valid:
            limiter.record_failure(key, now_float)
            return _no_store(
                JSONResponse({"detail": "Неверный логин или пароль."}, status_code=401)
            )
        limiter.clear(key)
        csrf = secrets.token_urlsafe(24)
        response = JSONResponse({"status": "ok", "username": effective.username})
        response.set_cookie(
            SESSION_COOKIE,
            _session_token(effective, csrf=csrf, now=int(now_float)),
            max_age=effective.session_ttl_seconds,
            secure=effective.cookie_secure,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            CSRF_COOKIE,
            csrf,
            max_age=effective.session_ttl_seconds,
            secure=effective.cookie_secure,
            httponly=False,
            samesite="lax",
            path="/",
        )
        return _no_store(response)

    @app.get("/auth/session")
    async def session(request: Request) -> Response:
        value = _read_session(
            effective,
            request.cookies.get(SESSION_COOKIE),
            now=int(time.time()),
        )
        if value is None:
            return _no_store(JSONResponse({"authenticated": False}))
        return _no_store(JSONResponse({"authenticated": True, "username": value["sub"]}))

    @app.post("/auth/logout")
    async def logout(request: Request) -> Response:
        value = _read_session(
            effective,
            request.cookies.get(SESSION_COOKIE),
            now=int(time.time()),
        )
        if value is None or not hmac.compare_digest(
            str(request.headers.get("x-cf-csrf") or ""),
            str(value.get("csrf") or ""),
        ):
            return _no_store(JSONResponse({"detail": "Сессия не подтверждена."}, status_code=403))
        response = JSONResponse({"status": "signed_out"})
        response.delete_cookie(SESSION_COOKIE, path="/", secure=effective.cookie_secure)
        response.delete_cookie(CSRF_COOKIE, path="/", secure=effective.cookie_secure)
        return _no_store(response)

    @app.get("/auth/verify")
    async def verify(request: Request) -> Response:
        value = _read_session(
            effective,
            request.cookies.get(SESSION_COOKIE),
            now=int(time.time()),
        )
        original_method = str(request.headers.get("x-forwarded-method") or "GET").upper()
        if value is None:
            accepts_html = "text/html" in str(request.headers.get("accept") or "")
            if original_method in {"GET", "HEAD"} and accepts_html:
                target = urlencode({"next": _safe_next(request)})
                return _no_store(RedirectResponse(f"/login?{target}", status_code=303))
            return _no_store(JSONResponse({"detail": "Требуется вход."}, status_code=401))
        if original_method not in SAFE_METHODS and not hmac.compare_digest(
            str(request.headers.get("x-cf-csrf") or ""),
            str(value.get("csrf") or ""),
        ):
            return _no_store(
                JSONResponse({"detail": "CSRF-подтверждение отсутствует."}, status_code=403)
            )
        return _no_store(
            Response(
                status_code=204,
                headers={
                    "X-CF-Actor": effective.username,
                    "X-CF-Actor-Role": "human",
                },
            )
        )

    return app
