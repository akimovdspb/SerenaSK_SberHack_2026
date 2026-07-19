from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.api.app.settings import Settings
from railway import start

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _environment(tmp_path: pathlib.Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin",
        "APP_ACCESS_USERNAME": "demo_user",
        "APP_ACCESS_PASSWORD": "synthetic-test-password",
        "OPENROUTER_API_KEY": "synthetic-test-provider-value",
        # Railway suggests values from the Compose-only .env.example. They must never
        # override the isolated hosted profile.
        "CF_RUNTIME_PROVIDER": "openai",
        "OPENROUTER_ENABLED": "false",
        "OUROBOROS_MODEL": "openai::gpt-5.4-mini",
        "OUROBOROS_MODEL_LIGHT": "openai::gpt-5.4-mini",
        "CF_STATE_ROOT": str(tmp_path / "state"),
        "RAILWAY_DEPLOYMENT_ID": "deployment-test-01",
        "PORT": "8080",
    }


def test_launch_plan_keeps_provider_and_password_out_of_app_and_gateway(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: dict[str, str] = {}
    monkeypatch.setattr(start, "_prepare_directories", lambda _: None)
    monkeypatch.setattr(
        start,
        "_write_secret",
        lambda path, value: written.update(path=str(path), value=value),
    )
    monkeypatch.setattr(start, "_password_verifier", lambda _: ("salt-value", "digest-value"))

    plan = start.build_launch_plan(_environment(tmp_path))

    assert "APP_ACCESS_USERNAME" not in plan.gateway_env
    assert "APP_ACCESS_PASSWORD" not in plan.gateway_env
    assert "OPENROUTER_API_KEY" not in plan.gateway_env
    assert "MCP_SHARED_TOKEN" not in plan.gateway_env
    assert "APP_ACCESS_PASSWORD" not in plan.app_env
    assert "OPENROUTER_API_KEY" not in plan.app_env
    assert plan.runtime_env["OPENROUTER_API_KEY_FILE"] == written["path"]
    assert "OPENROUTER_API_KEY" not in plan.runtime_env
    assert plan.runtime_env["OUROBOROS_MODEL"] == "openrouter::z-ai/glm-5.2"
    assert plan.runtime_env["OUROBOROS_MODEL_FALLBACKS"] == ""
    assert plan.runtime_env["OUROBOROS_EFFORT_TASK"] == "low"
    assert plan.runtime_env["CF_PROVIDER_PROFILE"] == "openrouter-glm-5.2-campaign-authoring"
    assert plan.runtime_env["OUROBOROS_TOOL_TIMEOUT_SEC"] == "30"
    assert plan.runtime_env["OUROBOROS_SAFETY_CALL_TIMEOUT_SEC"] == "20"
    assert plan.runtime_env["OUROBOROS_SERVER_HOST"] == "127.0.0.1"
    assert written["value"] == "synthetic-test-provider-value"
    assert plan.auth_env["AUTH_USERNAME"] == "demo_user"
    assert plan.auth_env["AUTH_PASSWORD_SALT"] == "salt-value"
    assert plan.auth_env["AUTH_PASSWORD_DIGEST"] == "digest-value"
    assert "APP_ACCESS_PASSWORD" not in plan.auth_env
    assert "OPENROUTER_API_KEY" not in plan.auth_env
    assert plan.app_env["DEFAULT_EXECUTION_MODE"] == "live_ouroboros"
    assert plan.app_env["RUNTIME_READY_PATH"].endswith("railway.ready.json")
    assert plan.app_env["LIVE_PROVIDER_PROFILE"] == "openrouter-glm-5.2-campaign-authoring"
    assert plan.app_env["LIVE_TASK_TIMEOUT_SECONDS"] == "600"
    assert plan.app_env["LIVE_RUN_TERMINAL_DEADLINE_SECONDS"] == "900"
    assert plan.app_env["LIVE_USAGE_EXPECTED_PROVIDER"] == "openrouter"
    assert plan.app_env["LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY"] == "false"
    assert plan.app_env["CONTROLLED_PROVIDER_RETRY_ENABLED"] == "true"
    assert plan.app_env["CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE"] == "none"
    assert plan.app_env["HUMAN_ACTIONS_TEST_ONLY"] == "false"
    assert plan.app_env["DEMO_RESET_ENABLED"] == "true"
    assert plan.app_env["SESSION_AUTH_ENABLED"] == "true"

    for name in start.SECRET_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    start.validate_secret_isolation(plan)


def test_launch_plan_accepts_mounted_secret_files_for_local_http(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password_file = tmp_path / "password.txt"
    provider_file = tmp_path / "openrouter.txt"
    password_file.write_text("synthetic-local-password\n", encoding="utf-8")
    provider_file.write_text("synthetic-local-provider-key\n", encoding="utf-8")
    environment = _environment(tmp_path)
    environment.pop("APP_ACCESS_PASSWORD")
    environment.pop("OPENROUTER_API_KEY")
    environment.update(
        {
            "APP_ACCESS_PASSWORD_FILE": str(password_file.resolve()),
            "OPENROUTER_API_KEY_FILE": str(provider_file.resolve()),
            "AUTH_COOKIE_SECURE": "false",
        }
    )
    written: dict[str, str] = {}
    monkeypatch.setattr(start, "_prepare_directories", lambda _: None)
    monkeypatch.setattr(
        start,
        "_write_secret",
        lambda path, value: written.update(path=str(path), value=value),
    )
    monkeypatch.setattr(start, "_password_verifier", lambda _: ("salt", "digest"))

    plan = start.build_launch_plan(environment)

    assert plan.auth_env["AUTH_COOKIE_SECURE"] == "false"
    assert written["value"] == "synthetic-local-provider-key"
    assert "APP_ACCESS_PASSWORD_FILE" not in plan.app_env
    assert "OPENROUTER_API_KEY_FILE" not in plan.gateway_env


def test_launch_plan_rejects_value_and_file_for_the_same_secret(
    tmp_path: pathlib.Path,
) -> None:
    password_file = tmp_path / "password.txt"
    password_file.write_text("synthetic-local-password\n", encoding="utf-8")
    environment = _environment(tmp_path)
    environment["APP_ACCESS_PASSWORD_FILE"] = str(password_file.resolve())

    with pytest.raises(start.RailwayStartupError, match="either a value or a file"):
        start.build_launch_plan(environment)


def test_launch_plan_requires_a_password(tmp_path: pathlib.Path) -> None:
    environment = _environment(tmp_path)
    environment.pop("APP_ACCESS_PASSWORD")

    with pytest.raises(start.RailwayStartupError, match="application access password"):
        start.build_launch_plan(environment)


def test_deployment_identity_is_stable_and_bound_to_the_deployment() -> None:
    first = start.deployment_identity({"RAILWAY_DEPLOYMENT_ID": "one"})
    repeated = start.deployment_identity({"RAILWAY_DEPLOYMENT_ID": "one"})
    second = start.deployment_identity({"RAILWAY_DEPLOYMENT_ID": "two"})

    assert first == repeated
    assert first != second
    assert first.startswith("sha256:") and len(first) == 71


def test_parent_environment_is_scrubbed_after_child_environments_are_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ACCESS_PASSWORD", "synthetic-test-password")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-provider-value")
    monkeypatch.setenv("MCP_SHARED_TOKEN", "synthetic-test-mcp-token")
    monkeypatch.setenv("APP_ACCESS_PASSWORD_FILE", "/synthetic/password")
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", "/synthetic/provider")

    start.scrub_parent_environment()

    assert "APP_ACCESS_PASSWORD" not in start.os.environ
    assert "OPENROUTER_API_KEY" not in start.os.environ
    assert "MCP_SHARED_TOKEN" not in start.os.environ
    assert "APP_ACCESS_PASSWORD_FILE" not in start.os.environ
    assert "OPENROUTER_API_KEY_FILE" not in start.os.environ


def test_railway_profile_is_one_service_with_public_health_and_private_core() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    caddyfile = (ROOT / "railway" / "Caddyfile").read_text(encoding="utf-8")
    config = (ROOT / "railway.toml").read_text(encoding="utf-8")

    assert 'builder = "DOCKERFILE"' in config
    assert 'dockerfilePath = "Dockerfile"' in config
    assert 'healthcheckPath = "/healthz"' in config
    assert "healthcheckTimeout = 1200" in config
    assert "rewrite * /readyz" in caddyfile
    assert 'respond "ok" 200' not in caddyfile
    assert "openrouter::z-ai/glm-5.2" in dockerfile
    assert "openrouter-glm-5.2-campaign-authoring" in dockerfile
    assert "a00d51dd414f794d830cacf7da760061e442fa88" in dockerfile
    assert "handle @health" in caddyfile
    assert "forward_auth 127.0.0.1:8090" in caddyfile
    assert "handle /login*" in caddyfile
    assert "@public_static path /assets/* /favicon.svg" in caddyfile
    assert "basic_auth" not in caddyfile
    assert "reverse_proxy 127.0.0.1:8000" in caddyfile
    assert "reverse_proxy 127.0.0.1:8765" not in caddyfile
    assert "COPY railway/auth.py" in dockerfile


def _launch_plan_for_bootstrap(tmp_path: pathlib.Path) -> start.LaunchPlan:
    return start.LaunchPlan(
        app_env={},
        auth_env={},
        gateway_env={},
        runtime_env={},
        state_root=tmp_path,
        runtime_identity=f"sha256:{'a' * 64}",
    )


def test_bootstrap_retries_transient_failures_and_marks_ready(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _launch_plan_for_bootstrap(tmp_path)
    attempts: list[int] = []
    marked_ready: list[start.LaunchPlan] = []

    def bootstrap(_: start.LaunchPlan) -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise start.RailwayStartupError("synthetic transient failure")

    monkeypatch.setattr(start, "_bootstrap", bootstrap)
    monkeypatch.setattr(start, "_mark_runtime_ready", marked_ready.append)
    monkeypatch.setattr(start.time, "sleep", lambda _: None)

    start._bootstrap_with_retries(plan)

    assert attempts == [1, 2, 3]
    assert marked_ready == [plan]


def test_bootstrap_retry_count_is_bounded(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _launch_plan_for_bootstrap(tmp_path)
    attempts: list[int] = []

    def bootstrap(_: start.LaunchPlan) -> None:
        attempts.append(len(attempts) + 1)
        raise start.RailwayStartupError("synthetic persistent failure")

    monkeypatch.setattr(start, "_bootstrap", bootstrap)
    monkeypatch.setattr(start.time, "sleep", lambda _: None)

    with pytest.raises(start.RailwayStartupError, match="after 3 bounded attempts"):
        start._bootstrap_with_retries(plan)

    assert attempts == [1, 2, 3]


def test_skill_review_allows_one_automatic_retry_then_stops(tmp_path: pathlib.Path) -> None:
    marker = tmp_path / "bootstrap-review.json"
    identity = {
        "schema_version": 1,
        "content_hash": "content-hash",
        "model": start.MODEL,
        "retry_id": "initial",
    }

    first = start._begin_skill_review_attempt(marker, identity)
    assert first["attempt_number"] == 1
    start._atomic_marker(marker, {**first, "status": "failed"})

    second = start._begin_skill_review_attempt(marker, identity)
    assert second["attempt_number"] == 2
    start._atomic_marker(marker, {**second, "status": "failed"})

    with pytest.raises(start.RailwayStartupError, match="exhausted"):
        start._begin_skill_review_attempt(marker, identity)


def test_railway_readiness_stays_closed_until_bootstrap_marker_exists(
    tmp_path: pathlib.Path,
) -> None:
    ready_marker = tmp_path / "railway.ready.json"
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{tmp_path / 'factory.db'}",
        ARTIFACTS_DIR=tmp_path / "artifacts",
        EVIDENCE_DIR=tmp_path / "evidence",
        SYNTHETIC_DATA_DIR=ROOT / "data" / "synthetic",
        RUNTIME_READY_PATH=ready_marker,
        MCP_SHARED_TOKEN="synthetic-mcp-token-that-is-at-least-32-chars",
    )

    with TestClient(create_app(settings)) as client:
        starting = client.get("/readyz")
        assert starting.status_code == 503
        assert starting.json() == {"status": "starting"}

        ready_marker.write_text("{}\n", encoding="utf-8")
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}
        assert client.get("/healthz").status_code == 200
