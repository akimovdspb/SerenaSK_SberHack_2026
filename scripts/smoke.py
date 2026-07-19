from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import pathlib
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime
from typing import Any

import bcrypt

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "runtime" / "smoke" / "latest.json"
PROVIDER_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
)
EXPECTED_EXPORT_FILES = {
    "campaign.json",
    "brief.json",
    "run.json",
    "context-manifest.json",
    "fact-card.json",
    "rules-version.json",
    "sms/message.txt",
    "sms/metrics.json",
    "email/email.html",
    "email/email.txt",
    "email/content.json",
    "qa/findings.json",
    "qa/report.html",
    "feedback/feedback.json",
    "feedback/diff.json",
    "learning/rule-proposal.json",
    "trace/safe-events.jsonl",
    "trace/mcp-calls.jsonl",
    "trace/model-usage.json",
    "manifest.json",
    "README.txt",
}


class SmokeError(RuntimeError):
    pass


def _run(
    command: list[str],
    *,
    environment: dict[str, str],
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and process.returncode != 0:
        raise SmokeError(f"command failed without provider execution: {command[0]} {command[1]}")
    return process


def _compose(
    project: str,
    arguments: list[str],
    *,
    environment: dict[str, str],
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["docker", "compose", "--project-name", project, *arguments],
        environment=environment,
        timeout=timeout,
        check=check,
    )


def _request(
    base_url: str,
    path: str,
    *,
    username: str | None = None,
    password: str | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, str], bytes, float]:
    headers = {"Accept": "application/json"}
    if username is not None and password is not None:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            if payload is not None
            else None
        ),
        headers=headers,
        method=method,
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return (
                response.status,
                {key.casefold(): value for key, value in response.headers.items()},
                response.read(),
                time.monotonic() - started,
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            {key.casefold(): value for key, value in exc.headers.items()},
            exc.read(),
            time.monotonic() - started,
        )


def _json_body(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError("API response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SmokeError("API response is not a JSON object")
    return value


def validate_export(content: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            if names != EXPECTED_EXPORT_FILES:
                raise SmokeError("export file inventory does not match the canonical contract")
            if "SYNTHETIC · NO SEND" not in archive.read("README.txt").decode("utf-8"):
                raise SmokeError("export no-send notice is missing")
            manifest = _json_body(archive.read("manifest.json"))
            if manifest.get("synthetic") is not True or manifest.get("no_send") is not True:
                raise SmokeError("export manifest is not synthetic/no-send")
            raw_checksums = manifest.get("files")
            if not isinstance(raw_checksums, dict):
                raise SmokeError("export checksums are missing")
            checksums = {str(name): str(digest) for name, digest in raw_checksums.items()}
            if set(checksums) != EXPECTED_EXPORT_FILES - {"manifest.json"}:
                raise SmokeError("export checksum inventory is incomplete")
            for name, expected in checksums.items():
                if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                    raise SmokeError("export checksum mismatch")
            usage = _json_body(archive.read("trace/model-usage.json"))
            if usage.get("provider_calls") != 0:
                raise SmokeError("deterministic smoke export reports provider calls")
            return manifest
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise SmokeError("export archive is malformed") from exc


def _assert_service_isolation(
    project: str,
    *,
    environment: dict[str, str],
) -> tuple[str, ...]:
    services = tuple(
        sorted(
            item
            for item in _compose(
                project,
                ["ps", "--all", "--services"],
                environment=environment,
            ).stdout.splitlines()
            if item
        )
    )
    if services != ("app", "gateway"):
        raise SmokeError("ephemeral smoke started a non-allowlisted service")
    for service in services:
        container_id = _compose(
            project,
            ["ps", "--quiet", service],
            environment=environment,
        ).stdout.strip()
        if not container_id:
            raise SmokeError("ephemeral smoke service has no running container")
        inspected = _run(
            ["docker", "inspect", "--format", "{{json .Config.Env}}", container_id],
            environment=environment,
        ).stdout
        try:
            container_environment = json.loads(inspected)
        except json.JSONDecodeError as exc:
            raise SmokeError("container environment inspection is malformed") from exc
        names = {
            str(item).split("=", 1)[0]
            for item in container_environment
            if isinstance(item, str) and "=" in item
        }
        if names.intersection(PROVIDER_ENV_NAMES):
            raise SmokeError("provider environment reached a no-provider smoke service")
    return services


def _smoke_environment(username: str, password_hash: str, mcp_token: str) -> dict[str, str]:
    environment = dict(os.environ)
    for name in PROVIDER_ENV_NAMES:
        environment.pop(name, None)
    environment.update(
        {
            "APP_ACCESS_USERNAME": username,
            "APP_ACCESS_PASSWORD_HASH": password_hash,
            "MCP_SHARED_TOKEN": mcp_token,
            "GATEWAY_HOST_BIND": "127.0.0.1",
            "GATEWAY_HOST_PORT": "0",
            "APP_ENV": "smoke",
            "DATABASE_URL": "sqlite:////data/factory.db",
            "ARTIFACTS_DIR": "/data/artifacts",
            "LOCAL_UID": str(os.getuid()),
            "LOCAL_GID": str(os.getgid()),
        }
    )
    return environment


def _key(ordinal: int) -> str:
    return f"compose-smoke-idempotency-{ordinal:04d}"


def run_smoke() -> dict[str, Any]:
    started_at = datetime.now(UTC)
    started = time.monotonic()
    project = f"cf-smoke-{secrets.token_hex(6)}"
    username = f"smoke_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(24)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    environment = _smoke_environment(username, password_hash, secrets.token_urlsafe(48))
    cleanup_error = False
    try:
        for image in ("communication-factory/app:local", "communication-factory/gateway:local"):
            _run(["docker", "image", "inspect", image], environment=environment)
        _compose(
            project,
            [
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "90",
                "--no-build",
                "gateway",
                "app",
            ],
            environment=environment,
            timeout=120,
        )
        services = _assert_service_isolation(project, environment=environment)
        port_line = _compose(
            project,
            ["port", "gateway", "8080"],
            environment=environment,
        ).stdout.strip()
        try:
            port = int(port_line.rsplit(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise SmokeError("ephemeral gateway port could not be resolved") from exc
        base_url = f"http://127.0.0.1:{port}"

        unauthorized, _, _, _ = _request(base_url, "/api/v1/health")
        if unauthorized != 401:
            raise SmokeError("gateway did not require authentication")

        status, headers, body, health_latency = _request(
            base_url,
            "/api/v1/health",
            username=username,
            password=password,
        )
        health = _json_body(body)
        if status != 200 or health != {
            "status": "ok",
            "environment": "smoke",
            "data_mode": "synthetic_only",
            "external_send_enabled": False,
        }:
            raise SmokeError("authenticated health contract failed")
        if "default-src 'self'" not in headers.get("content-security-policy", ""):
            raise SmokeError("gateway security headers are incomplete")
        if headers.get("x-content-type-options") != "nosniff":
            raise SmokeError("gateway content type protection is missing")

        status, _, body, _ = _request(
            base_url,
            "/api/v1/ready",
            username=username,
            password=password,
        )
        readiness = _json_body(body)
        if (
            status != 200
            or readiness.get("synthetic_case_count") != 15
            or readiness.get("external_send_enabled") is not False
        ):
            raise SmokeError("readiness contract failed")

        status, _, body, _ = _request(
            base_url,
            "/api/v1/campaigns",
            username=username,
            password=password,
            method="POST",
            payload={"case_id": "B04"},
            idempotency_key=_key(1),
        )
        campaign = _json_body(body)
        campaign_id = str(campaign.get("campaign_id") or "")
        if status != 201 or not campaign_id:
            raise SmokeError("B04 campaign creation failed")

        status, _, body, _ = _request(
            base_url,
            f"/api/v1/campaigns/{campaign_id}/validate",
            username=username,
            password=password,
            method="POST",
            idempotency_key=_key(2),
        )
        validated = _json_body(body)
        if status != 200 or validated.get("state") != "READY":
            raise SmokeError("B04 did not reach READY")

        status, _, body, _ = _request(
            base_url,
            f"/api/v1/campaigns/{campaign_id}/runs",
            username=username,
            password=password,
            method="POST",
            payload={"mode": "deterministic_template"},
            idempotency_key=_key(3),
        )
        package = _json_body(body)
        package_id = str(package.get("package_id") or "")
        quality = package.get("quality_report")
        if (
            status != 201
            or not package_id
            or not isinstance(quality, dict)
            or quality.get("approvable") is not True
            or len(quality.get("checked_ids", [])) != 22
            or package.get("mode") != "deterministic_template"
        ):
            raise SmokeError("deterministic B04 package contract failed")

        status, _, _, _ = _request(
            base_url,
            f"/api/v1/packages/{package_id}/export",
            username=username,
            password=password,
            method="POST",
            idempotency_key=_key(4),
        )
        if status != 409:
            raise SmokeError("export was not blocked before human approval")

        status, _, body, _ = _request(
            base_url,
            f"/api/v1/packages/{package_id}/approve",
            username=username,
            password=password,
            method="POST",
            payload={
                "package_hash": package.get("package_hash"),
                "decision": "APPROVED",
                "acknowledged_warning_ids": [],
                "test_only": True,
            },
            idempotency_key=_key(5),
        )
        approval = _json_body(body)
        if (
            status != 200
            or approval.get("actor_role") != "human"
            or approval.get("test_only") is not True
        ):
            raise SmokeError("test-only human approval contract failed")

        export_started = time.monotonic()
        status, _, body, _ = _request(
            base_url,
            f"/api/v1/packages/{package_id}/export",
            username=username,
            password=password,
            method="POST",
            idempotency_key=_key(6),
        )
        export_latency = time.monotonic() - export_started
        exported = _json_body(body)
        export_id = str(exported.get("export_id") or "")
        if status != 201 or not export_id:
            raise SmokeError("approved package export failed")
        status, _, archive, _ = _request(
            base_url,
            f"/api/v1/exports/{export_id}/download",
            username=username,
            password=password,
        )
        if status != 200:
            raise SmokeError("export download failed")
        manifest = validate_export(archive)
        if manifest.get("package_id") != package_id:
            raise SmokeError("export package identity mismatch")

        _compose(project, ["restart", "app"], environment=environment, timeout=60)
        _compose(
            project,
            ["up", "--detach", "--wait", "--wait-timeout", "60", "--no-build", "gateway", "app"],
            environment=environment,
            timeout=90,
        )
        status, _, body, _ = _request(
            base_url,
            f"/api/v1/campaigns/{campaign_id}",
            username=username,
            password=password,
        )
        persisted = _json_body(body)
        if status != 200 or persisted.get("state") != "EXPORTED":
            raise SmokeError("completed state did not survive app restart")
        status, _, body, _ = _request(
            base_url,
            f"/api/v1/exports/{export_id}",
            username=username,
            password=password,
        )
        if status != 200 or _json_body(body).get("archive_sha256") != exported.get(
            "archive_sha256"
        ):
            raise SmokeError("export metadata did not survive app restart")

        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "started_at": started_at.isoformat(),
            "status": "PASS",
            "mode": "deterministic_template",
            "case_id": "B04",
            "services": list(services),
            "provider_calls": 0,
            "ouroboros_started": False,
            "external_send_enabled": False,
            "gateway_auth_required": True,
            "test_only_approval": True,
            "restart_persistence": True,
            "qa_check_count": 22,
            "export_file_count": len(EXPECTED_EXPORT_FILES),
            "export_sha256": hashlib.sha256(archive).hexdigest(),
            "health_latency_ms": round(health_latency * 1000, 3),
            "export_latency_ms": round(export_latency * 1000, 3),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    finally:
        cleanup = _compose(
            project,
            ["down", "--volumes", "--remove-orphans", "--timeout", "10"],
            environment=environment,
            timeout=60,
            check=False,
        )
        cleanup_error = cleanup.returncode != 0
        password = ""
        password_hash = ""
        if cleanup_error and sys.exc_info()[0] is None:
            raise SmokeError("ephemeral Compose cleanup failed")


def _write_report(report: dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(REPORT_PATH)


def main() -> int:
    try:
        report = run_smoke()
        _write_report(report)
    except (OSError, ValueError, SmokeError, subprocess.SubprocessError) as exc:
        print(f"smoke: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "smoke: PASS services=app,gateway case=B04 provider_calls=0 "
        f"restart=true export_files={report['export_file_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
