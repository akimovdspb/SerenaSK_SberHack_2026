from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from provider_profiles import CAMPAIGN_AUTHORING_PROFILE_NAME, provider_profile

APP_UID = 10002
APP_GID = 10002
GATEWAY_UID = 10003
GATEWAY_GID = 10003
RUNTIME_UID = 10001
RUNTIME_GID = 10001
CONTRACT_GID = 10004
RAILWAY_PROVIDER_PROFILE = provider_profile(CAMPAIGN_AUTHORING_PROFILE_NAME)
MODEL = RAILWAY_PROVIDER_PROFILE.runtime_route
RUNTIME_COMMIT = "a00d51dd414f794d830cacf7da760061e442fa88"
SECRET_ENV_NAMES = {
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "APP_ACCESS_PASSWORD",
    "APP_ACCESS_PASSWORD_HASH",
    "AUTH_PASSWORD_SALT",
    "AUTH_PASSWORD_DIGEST",
    "AUTH_SESSION_SECRET",
    "MCP_SHARED_TOKEN",
}
SYSTEM_ENV_NAMES = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)


class RailwayStartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class LaunchPlan:
    app_env: dict[str, str]
    auth_env: dict[str, str]
    gateway_env: dict[str, str]
    runtime_env: dict[str, str]
    state_root: pathlib.Path
    runtime_identity: str


def _required(environment: dict[str, str], name: str) -> str:
    value = str(environment.get(name) or "").strip()
    if not value:
        raise RailwayStartupError(f"required Railway variable {name} is missing")
    return value


def _positive_float(environment: dict[str, str], name: str, default: str) -> float:
    try:
        value = float(str(environment.get(name) or default))
    except ValueError as exc:
        raise RailwayStartupError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise RailwayStartupError(f"{name} must be a positive number")
    return value


def deployment_identity(environment: dict[str, str]) -> str:
    deployment = str(
        environment.get("RAILWAY_DEPLOYMENT_ID")
        or environment.get("RAILWAY_GIT_COMMIT_SHA")
        or environment.get("CF_BUILD_REVISION")
        or "local-railway-image"
    ).strip()
    payload = f"railway-deployment-v1\0{RUNTIME_COMMIT}\0{deployment}".encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _system_environment(environment: dict[str, str]) -> dict[str, str]:
    selected = {
        name: environment[name] for name in SYSTEM_ENV_NAMES if environment.get(name) is not None
    }
    selected.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return selected


def _write_secret(path: pathlib.Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, value.encode("utf-8"))
    finally:
        os.close(descriptor)


def _password_verifier(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return (
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def _prepare_directories(state_root: pathlib.Path) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    directories = (
        (state_root / "app", APP_UID, APP_GID, 0o750),
        (state_root / "app" / "artifacts", APP_UID, APP_GID, 0o750),
        (state_root / "app" / "evidence", APP_UID, APP_GID, 0o750),
        (state_root / "ouroboros", RUNTIME_UID, RUNTIME_GID, 0o750),
        (state_root / "ouroboros" / "data", RUNTIME_UID, RUNTIME_GID, 0o750),
        (state_root / "contracts", RUNTIME_UID, CONTRACT_GID, 0o2770),
        (pathlib.Path("/tmp/cf-caddy-config"), GATEWAY_UID, GATEWAY_GID, 0o700),
        (pathlib.Path("/tmp/cf-caddy-data"), GATEWAY_UID, GATEWAY_GID, 0o700),
    )
    for path, uid, gid, mode in directories:
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, uid, gid)
        os.chmod(path, mode)
    for path in (pathlib.Path("/tmp/cf-runtime-repo"), pathlib.Path("/tmp/cf-probe-repo")):
        if path.exists():
            if path.parent != pathlib.Path("/tmp") or not path.name.startswith("cf-"):
                raise RailwayStartupError("refusing to clear an unexpected runtime directory")
            shutil.rmtree(path)
        path.mkdir(mode=0o700)
        os.chown(path, RUNTIME_UID, RUNTIME_GID)


def build_launch_plan(source: dict[str, str] | None = None) -> LaunchPlan:
    environment = dict(source or os.environ)
    username = _required(environment, "APP_ACCESS_USERNAME")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", username):
        raise RailwayStartupError("APP_ACCESS_USERNAME contains unsupported characters")
    password = str(environment.pop("APP_ACCESS_PASSWORD", ""))
    environment.pop("APP_ACCESS_PASSWORD_HASH", None)
    if not password:
        raise RailwayStartupError("required Railway variable APP_ACCESS_PASSWORD is missing")
    password_salt, password_digest = _password_verifier(password)
    password = ""

    provider_key = str(environment.pop("OPENROUTER_API_KEY", ""))
    if not provider_key or provider_key != provider_key.strip() or "\n" in provider_key:
        raise RailwayStartupError("OPENROUTER_API_KEY has an invalid shape")
    for name in SECRET_ENV_NAMES:
        environment.pop(name, None)
    secret_path = pathlib.Path("/run/cf-secrets/openrouter_api_key")
    _write_secret(secret_path, provider_key)
    provider_key = ""

    total_budget = _positive_float(environment, "TOTAL_BUDGET", "20")
    per_task_budget = _positive_float(environment, "OUROBOROS_PER_TASK_COST_USD", "2")
    if per_task_budget > total_budget:
        raise RailwayStartupError("per-task budget must not exceed TOTAL_BUDGET")

    state_root = pathlib.Path(
        str(environment.get("CF_STATE_ROOT") or "/var/lib/communication-factory")
    )
    if not state_root.is_absolute():
        raise RailwayStartupError("CF_STATE_ROOT must be an absolute path")
    _prepare_directories(state_root)
    identity = deployment_identity(environment)
    mcp_token = secrets.token_urlsafe(36)
    system = _system_environment(environment)

    app_env = {
        **system,
        "HOME": "/home/factory",
        "PYTHONPATH": "/srv/app",
        "APP_ENV": "railway",
        "DATABASE_URL": f"sqlite:///{state_root / 'app' / 'factory.db'}",
        "ARTIFACTS_DIR": str(state_root / "app" / "artifacts"),
        "EVIDENCE_DIR": str(state_root / "app" / "evidence"),
        "MVP_REPORT_DIR": "/srv/app/reports/basket03-mvp-testing",
        "SYNTHETIC_DATA_DIR": "/srv/app/data/synthetic",
        "CONTRACT_LOCK_PATH": str(state_root / "contracts" / "communication_factory.lock.json"),
        "SKILL_PATH": "/skills/communication_factory/SKILL.md",
        "OUROBOROS_BASE_URL": "http://127.0.0.1:8765",
        "RUNTIME_CONTRACT_IDENTITY_KIND": "railway_deployment",
        "RUNTIME_CONTRACT_IDENTITY": identity,
        "MCP_SHARED_TOKEN": mcp_token,
        "MCP_ALLOWED_HOSTS": "127.0.0.1:8000",
        "MCP_MAX_PAYLOAD_BYTES": "65536",
        "DEFAULT_EXECUTION_MODE": "live_ouroboros",
        "LIVE_PROVIDER_PROFILE": RAILWAY_PROVIDER_PROFILE.name,
        "LIVE_TASK_TIMEOUT_SECONDS": str(RAILWAY_PROVIDER_PROFILE.task_timeout_seconds),
        "LIVE_RUN_TERMINAL_DEADLINE_SECONDS": str(
            RAILWAY_PROVIDER_PROFILE.terminal_deadline_seconds
        ),
        "LIVE_USAGE_EXPECTED_PROVIDER": RAILWAY_PROVIDER_PROFILE.ledger_provider,
        "LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY": str(
            RAILWAY_PROVIDER_PROFILE.require_post_task_summary
        ).lower(),
        "CONTROLLED_PROVIDER_RETRY_ENABLED": "true",
        "CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE": "none",
        "HUMAN_ACTIONS_TEST_ONLY": "false",
        "DEMO_RESET_ENABLED": "true",
        "SESSION_AUTH_ENABLED": "true",
    }
    runtime_data = state_root / "ouroboros" / "data"
    runtime_env = {
        **system,
        "HOME": "/home/ouroboros",
        "PYTHONPATH": "/opt/ouroboros",
        "CF_DEPLOYMENT_PROFILE": "railway",
        "CF_RUNTIME_PROVIDER": "openrouter",
        "CF_PROVIDER_PROFILE": RAILWAY_PROVIDER_PROFILE.name,
        "OPENROUTER_ENABLED": "true",
        "OPENROUTER_API_KEY_FILE": str(secret_path),
        "OUROBOROS_MODEL": MODEL,
        "OUROBOROS_MODEL_HEAVY": MODEL,
        "OUROBOROS_MODEL_LIGHT": MODEL,
        "OUROBOROS_MODEL_FALLBACKS": "",
        "OUROBOROS_APP_ROOT": str(state_root / "ouroboros"),
        "OUROBOROS_REPO_DIR": "/tmp/cf-runtime-repo",
        "OUROBOROS_DATA_DIR": str(runtime_data),
        "OUROBOROS_SETTINGS_PATH": str(runtime_data / "settings.json"),
        "OUROBOROS_PID_FILE": str(runtime_data / "state" / "ouroboros.pid"),
        "OUROBOROS_PORT_FILE": str(runtime_data / "state" / "server_port"),
        "OUROBOROS_SERVER_HOST": "127.0.0.1",
        "OUROBOROS_SERVER_PORT": "8765",
        "OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD": "1",
        "OUROBOROS_SKILLS_REPO_PATH": "/skills",
        "OUROBOROS_RUNTIME_MODE": "light",
        "OUROBOROS_CONTEXT_MODE": "low",
        "OUROBOROS_SAFETY_MODE": "full",
        "OUROBOROS_TASK_REVIEW_MODE": "off",
        "OUROBOROS_ACCEPTANCE_MAX_IMPROVEMENT_PASSES": "0",
        "OUROBOROS_POST_TASK_EVOLUTION": "false",
        "OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS": "false",
        "OUROBOROS_MAX_ACTIVE_SUBAGENTS_PER_ROOT": "1",
        "OUROBOROS_MAX_SUBAGENT_DEPTH": "0",
        "OUROBOROS_MAX_WORKERS": "1",
        "OUROBOROS_MAX_ROUNDS": "8",
        # Use the bounded campaign-authoring profile so GLM can complete the required
        # context/read-and-save handshake before the application terminal deadline.
        "OUROBOROS_EFFORT_TASK": RAILWAY_PROVIDER_PROFILE.reasoning_effort,
        "OUROBOROS_EFFORT_REVIEW": RAILWAY_PROVIDER_PROFILE.reasoning_effort,
        "OUROBOROS_RETURN_REASONING": "false",
        "OUROBOROS_GENERATIVE_PROBE": "0",
        "OUROBOROS_TOOL_TIMEOUT_SEC": str(RAILWAY_PROVIDER_PROFILE.tool_call_timeout_seconds),
        "OUROBOROS_SAFETY_CALL_TIMEOUT_SEC": str(
            RAILWAY_PROVIDER_PROFILE.safety_call_timeout_seconds
        ),
        "OUROBOROS_FINALIZATION_GRACE_SEC": "2",
        "FACTORY_MCP_URL": "http://127.0.0.1:8000/internal/mcp",
        "MCP_SHARED_TOKEN": mcp_token,
        "TOTAL_BUDGET": str(total_budget),
        "OUROBOROS_PER_TASK_COST_USD": str(per_task_budget),
        "AUTO_BOOTSTRAP_SKILL_REVIEW": str(
            environment.get("AUTO_BOOTSTRAP_SKILL_REVIEW") or "true"
        ).lower(),
        "AUTO_BOOTSTRAP_REVIEW_RETRY_ID": str(
            environment.get("AUTO_BOOTSTRAP_REVIEW_RETRY_ID") or "initial"
        ),
    }
    auth_env = {
        **system,
        "HOME": "/home/gateway",
        "PYTHONPATH": "/opt/communication-factory",
        "AUTH_USERNAME": username,
        "AUTH_PASSWORD_SALT": password_salt,
        "AUTH_PASSWORD_DIGEST": password_digest,
        "AUTH_SESSION_SECRET": base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii"),
        "AUTH_COOKIE_SECURE": "true",
    }
    gateway_env = {
        "PATH": system.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"),
        "HOME": "/home/gateway",
        "XDG_CONFIG_HOME": "/tmp/cf-caddy-config",
        "XDG_DATA_HOME": "/tmp/cf-caddy-data",
        "PORT": str(environment.get("PORT") or "8080"),
    }
    return LaunchPlan(
        app_env=app_env,
        auth_env=auth_env,
        gateway_env=gateway_env,
        runtime_env=runtime_env,
        state_root=state_root,
        runtime_identity=identity,
    )


def scrub_parent_environment() -> None:
    for name in SECRET_ENV_NAMES:
        os.environ.pop(name, None)


def validate_secret_isolation(plan: LaunchPlan) -> None:
    provider_keys = {"OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"}
    if provider_keys & plan.app_env.keys() or "APP_ACCESS_PASSWORD" in plan.app_env:
        raise RailwayStartupError("application environment contains a provider or UI secret")
    gateway_forbidden = provider_keys | {
        "APP_ACCESS_PASSWORD",
        "MCP_SHARED_TOKEN",
        "OPENROUTER_API_KEY_FILE",
    }
    if gateway_forbidden & plan.gateway_env.keys():
        raise RailwayStartupError("gateway environment contains a private runtime secret")
    auth_forbidden = provider_keys | {"APP_ACCESS_PASSWORD", "MCP_SHARED_TOKEN"}
    if auth_forbidden & plan.auth_env.keys():
        raise RailwayStartupError("authentication environment contains an unrelated secret")
    if (
        not {
            "AUTH_USERNAME",
            "AUTH_PASSWORD_SALT",
            "AUTH_PASSWORD_DIGEST",
            "AUTH_SESSION_SECRET",
        }
        <= plan.auth_env.keys()
    ):
        raise RailwayStartupError("authentication verifier material is incomplete")
    if provider_keys & plan.runtime_env.keys() or "APP_ACCESS_PASSWORD" in plan.runtime_env:
        raise RailwayStartupError("runtime received a plaintext provider or UI secret")
    if not plan.runtime_env.get("OPENROUTER_API_KEY_FILE"):
        raise RailwayStartupError("runtime provider secret path is missing")
    if SECRET_ENV_NAMES & os.environ.keys():
        raise RailwayStartupError("container supervisor environment was not scrubbed")


def _drop_to(uid: int, gid: int, groups: tuple[int, ...] = ()) -> Any:
    def apply_identity() -> None:
        os.setgroups(list(groups))
        os.setgid(gid)
        os.setuid(uid)

    return apply_identity


def _spawn_processes(plan: LaunchPlan) -> dict[str, subprocess.Popen[bytes]]:
    processes = {
        "auth": subprocess.Popen(
            [
                "/opt/app-venv/bin/uvicorn",
                "railway.auth:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8090",
                "--no-server-header",
            ],
            cwd="/opt/communication-factory",
            env=plan.auth_env,
            preexec_fn=_drop_to(GATEWAY_UID, GATEWAY_GID),
        ),
        "app": subprocess.Popen(
            [
                "/opt/app-venv/bin/uvicorn",
                "apps.api.app.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--no-server-header",
            ],
            cwd="/srv/app",
            env=plan.app_env,
            preexec_fn=_drop_to(APP_UID, APP_GID, (CONTRACT_GID,)),
        ),
        "ouroboros": subprocess.Popen(
            [
                sys.executable,
                "/opt/communication-factory/entrypoint.py",
                sys.executable,
                "/opt/communication-factory/runtime_launcher.py",
            ],
            cwd="/opt/communication-factory",
            env=plan.runtime_env,
        ),
        "gateway": subprocess.Popen(
            [
                "caddy",
                "run",
                "--config",
                "/etc/caddy/Caddyfile",
                "--adapter",
                "caddyfile",
            ],
            env=plan.gateway_env,
            preexec_fn=_drop_to(GATEWAY_UID, GATEWAY_GID),
        ),
    }
    print(
        "railway-start: processes started gateway=public auth=private app=private ouroboros=private"
    )
    return processes


def _get_json(url: str, *, timeout: float = 3) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read())
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise RailwayStartupError("private readiness request failed") from exc
    if not isinstance(value, dict):
        raise RailwayStartupError("private readiness response is invalid")
    return value


def _wait_until(label: str, check: Any, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except RailwayStartupError:
            pass
        time.sleep(1)
    raise RailwayStartupError(f"{label} did not become ready")


def _run_checked(
    label: str,
    command: list[str],
    *,
    env: dict[str, str],
    timeout: float,
    preexec_fn: Any = None,
) -> None:
    try:
        process = subprocess.run(
            command,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
            preexec_fn=preexec_fn,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RailwayStartupError(f"{label} could not run") from exc
    if process.returncode != 0:
        raise RailwayStartupError(f"{label} failed")


def _atomic_marker(path: pathlib.Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _skill_state() -> dict[str, Any]:
    manifest = _get_json("http://127.0.0.1:8765/api/extensions/communication_factory/manifest")
    extensions = _get_json("http://127.0.0.1:8765/api/extensions")
    row = next(
        (
            item
            for item in extensions.get("skills", [])
            if isinstance(item, dict) and item.get("name") == "communication_factory"
        ),
        {},
    )
    return {
        "content_hash": str(manifest.get("content_hash") or ""),
        "clean": row.get("review_status") == "clean" and not row.get("review_stale"),
        "enabled": bool(row.get("enabled")),
        "executable_review": bool(row.get("executable_review")),
    }


def _bootstrap(plan: LaunchPlan) -> None:
    _wait_until(
        "authentication",
        lambda: _get_json("http://127.0.0.1:8090/auth/health").get("status") == "ok",
        timeout=30,
    )
    _wait_until(
        "application",
        lambda: _get_json("http://127.0.0.1:8000/healthz").get("status") == "ok",
        timeout=90,
    )

    def runtime_ready() -> bool:
        state = _get_json("http://127.0.0.1:8765/api/state")
        return bool(
            state.get("supervisor_ready")
            and not state.get("supervisor_error")
            and int(state.get("workers_alive") or 0) > 0
        )

    _wait_until("Ouroboros", runtime_ready, timeout=180)
    admin = [sys.executable, "/opt/communication-factory/runtime_admin.py"]
    _run_checked("runtime refresh", [*admin, "refresh"], env=plan.runtime_env, timeout=45)
    skill = _skill_state()
    if not (skill["clean"] and skill["executable_review"]):
        if plan.runtime_env["AUTO_BOOTSTRAP_SKILL_REVIEW"] != "true":
            raise RailwayStartupError("skill review is required but automatic review is disabled")
        marker_path = plan.state_root / "ouroboros" / "bootstrap-review.json"
        attempt = {
            "schema_version": 1,
            "content_hash": skill["content_hash"],
            "model": MODEL,
            "retry_id": plan.runtime_env["AUTO_BOOTSTRAP_REVIEW_RETRY_ID"],
        }
        if marker_path.exists():
            try:
                previous = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RailwayStartupError("bootstrap review marker is invalid") from exc
            same_attempt = all(previous.get(key) == value for key, value in attempt.items())
            if same_attempt and previous.get("status") in {"started", "failed"}:
                raise RailwayStartupError(
                    "previous skill review failed; set a new AUTO_BOOTSTRAP_REVIEW_RETRY_ID"
                )
        _atomic_marker(
            marker_path,
            {**attempt, "status": "started", "started_at": datetime.now(UTC).isoformat()},
        )
        try:
            _run_checked(
                "one-time skill review",
                [*admin, "review-rebuttal"],
                env=plan.runtime_env,
                timeout=300,
            )
        except RailwayStartupError:
            _atomic_marker(
                marker_path,
                {**attempt, "status": "failed", "finished_at": datetime.now(UTC).isoformat()},
            )
            raise
        _atomic_marker(
            marker_path,
            {**attempt, "status": "passed", "finished_at": datetime.now(UTC).isoformat()},
        )
    skill = _skill_state()
    if not skill["enabled"]:
        _run_checked("skill enable", [*admin, "enable"], env=plan.runtime_env, timeout=45)
    _run_checked("runtime validation", [*admin, "snapshot"], env=plan.runtime_env, timeout=45)

    probe_env = dict(plan.runtime_env)
    probe_env.update(
        {
            "OUROBOROS_REPO_DIR": "/tmp/cf-probe-repo",
            "CONTRACT_IMAGE_ID": plan.runtime_identity,
            "CONTRACT_IDENTITY_KIND": "railway_deployment",
            "CONTRACT_LOCK_DIR": str(plan.state_root / "contracts"),
            "CONTRACT_PROJECTION_PATH": "/projection/communication_factory.ru.md",
            "CONTRACT_PROBE_DRIVE": "/tmp/cf-probe-drive",
        }
    )
    probe_drive = pathlib.Path("/tmp/cf-probe-drive")
    if probe_drive.exists():
        shutil.rmtree(probe_drive)
    probe_drive.mkdir(mode=0o700)
    os.chown(probe_drive, RUNTIME_UID, RUNTIME_GID)
    _run_checked(
        "contract probe",
        [sys.executable, "/opt/communication-factory/entrypoint.py", "contract-probe"],
        env=probe_env,
        timeout=150,
    )
    _run_checked(
        "application admission",
        ["/opt/app-venv/bin/python", "/opt/communication-factory/check_admission.py"],
        env=plan.app_env,
        timeout=45,
        preexec_fn=_drop_to(APP_UID, APP_GID, (CONTRACT_GID,)),
    )
    print("railway-bootstrap: READY provider=openrouter model=z-ai/glm-5.2")


def _stop_processes(processes: dict[str, subprocess.Popen[bytes]]) -> None:
    for process in reversed(tuple(processes.values())):
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 12
    for process in reversed(tuple(processes.values())):
        remaining = max(0.1, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    if os.name != "posix" or os.geteuid() != 0:
        print("railway-start: FAIL deployment launcher requires container root", file=sys.stderr)
        return 78
    try:
        plan = build_launch_plan()
        scrub_parent_environment()
        validate_secret_isolation(plan)
        processes = _spawn_processes(plan)
    except RailwayStartupError as exc:
        print(f"railway-start: FAIL {exc}", file=sys.stderr)
        return 78

    stop_requested = threading.Event()

    def request_stop(_: int, __: Any) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    def bootstrap_target() -> None:
        try:
            _bootstrap(plan)
        except RailwayStartupError as exc:
            print(f"railway-bootstrap: BLOCKED {exc}", file=sys.stderr)

    threading.Thread(target=bootstrap_target, name="cf-bootstrap", daemon=True).start()
    exit_code = 0
    try:
        while not stop_requested.wait(0.5):
            exited = next(
                (
                    (name, process)
                    for name, process in processes.items()
                    if process.poll() is not None
                ),
                None,
            )
            if exited is not None:
                name, process = exited
                print(
                    f"railway-start: process {name} exited code={process.returncode}",
                    file=sys.stderr,
                )
                exit_code = 1
                break
    finally:
        _stop_processes(processes)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
