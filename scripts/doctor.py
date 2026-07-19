from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

from scripts.budget_control import DEFAULT_OPERATOR_LIMITS, load_operator_profile
from scripts.compose_contract import load_rendered_compose, validate_compose, validate_static_files
from scripts.live_evaluation import validate_strict_contract
from scripts.preflight import (
    ENV_PATH,
    KEY_PATH,
    validate_host_environment,
    validate_key_source,
    validate_local_environment,
    validate_no_repo_secret_copies,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "runtime" / "doctor" / "latest.json"
CONTRACT_PATH = ROOT / "runtime" / "contracts" / "communication_factory.lock.json"


class DoctorError(RuntimeError):
    pass


def _version(command: list[str]) -> str:
    process = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise DoctorError(f"required tool is unavailable: {command[0]}")
    line = (process.stdout or process.stderr).strip().splitlines()
    if not line:
        raise DoctorError(f"required tool returned no version: {command[0]}")
    return line[0][:200]


def redacted_contract_summary(contract: dict[str, Any]) -> dict[str, Any]:
    runtime = contract.get("runtime")
    skill = contract.get("skill")
    tools = contract.get("tools")
    runtime = runtime if isinstance(runtime, dict) else {}
    skill = skill if isinstance(skill, dict) else {}
    tools = tools if isinstance(tools, dict) else {}
    try:
        validate_strict_contract(contract)
    except RuntimeError:
        strict_ready = False
    else:
        strict_ready = True
    return {
        "present": bool(contract),
        "runtime_tag": runtime.get("tag"),
        "runtime_commit": runtime.get("commit"),
        "skill_ready": skill.get("ready") is True,
        "activation_mode": skill.get("activation_mode"),
        "provider_tool_names": tools.get("post_deny_tool_names", []),
        "strict_provider_tools_ready": strict_ready,
        "release_blocker": None if strict_ready else "CF-RP-001",
    }


def _services() -> dict[str, str]:
    process = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise DoctorError("Docker Compose service status is unavailable")
    result: dict[str, str] = {}
    for line in process.stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DoctorError("Docker Compose service status is malformed") from exc
        if isinstance(row, dict):
            result[str(row.get("Service") or "unknown")] = str(row.get("Status") or "")
    return result


def _database_status() -> dict[str, Any]:
    program = """
import json
from apps.api.app.sqlite_runtime import create_sqlite_aware_engine
engine = create_sqlite_aware_engine('sqlite:////data/factory.db')
with engine.connect() as connection:
    value = {
        'journal_mode': connection.exec_driver_sql('PRAGMA journal_mode').scalar_one(),
        'busy_timeout_ms': connection.exec_driver_sql('PRAGMA busy_timeout').scalar_one(),
        'foreign_keys': connection.exec_driver_sql('PRAGMA foreign_keys').scalar_one(),
        'synchronous': connection.exec_driver_sql('PRAGMA synchronous').scalar_one(),
        'integrity_check': connection.exec_driver_sql('PRAGMA integrity_check').scalar_one(),
        'foreign_key_error_count': len(
            connection.exec_driver_sql('PRAGMA foreign_key_check').all()
        ),
    }
print(json.dumps(value, sort_keys=True))
""".strip()
    process = subprocess.run(
        ["docker", "compose", "exec", "-T", "app", "python", "-c", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise DoctorError("database runtime status is malformed") from exc
    expected = {
        "journal_mode": "wal",
        "busy_timeout_ms": 5_000,
        "foreign_keys": 1,
        "synchronous": 1,
        "integrity_check": "ok",
        "foreign_key_error_count": 0,
    }
    if process.returncode != 0 or value != expected:
        raise DoctorError("database runtime policy/integrity check failed")
    return expected


def run_doctor() -> dict[str, Any]:
    validate_host_environment()
    validate_key_source(KEY_PATH)
    validate_local_environment(ENV_PATH)
    validate_no_repo_secret_copies()
    profile = load_operator_profile(DEFAULT_OPERATOR_LIMITS, model="gpt-5.4-mini")
    compose_errors = validate_compose(load_rendered_compose()) + validate_static_files()
    if compose_errors:
        raise DoctorError(compose_errors[0])
    try:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        contract = {}
    if not isinstance(contract, dict):
        contract = {}
    services = _services()
    required_services = {"app", "gateway", "ouroboros"}
    services_running = required_services.issubset(services) and all(
        "Up " in services[name] for name in required_services
    )
    if not services_running:
        raise DoctorError("app, gateway and Ouroboros must all be running")
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "toolchain": {
            "git": _version(["git", "--version"]),
            "docker": _version(["docker", "--version"]),
            "compose": _version(["docker", "compose", "version"]),
            "make": _version(["make", "--version"]),
            "uv": _version(["uv", "--version"]),
            "node": _version(["node", "--version"]),
            "npm": _version(["npm", "--version"]),
        },
        "services": services,
        "database": _database_status(),
        "contract": redacted_contract_summary(contract),
        "credential_source_present": True,
        "credential_source_mode": "0600",
        "host_provider_environment_present": False,
        "operator_profile_valid": True,
        "operator_model": profile.model,
        "account_remaining": "unknown",
        "secret_values_in_report": False,
    }


def _write(report: dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, REPORT_PATH)


def main() -> int:
    try:
        report = run_doctor()
        _write(report)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"doctor: FAIL: {exc}", file=sys.stderr)
        return 1
    blocker = report["contract"]["release_blocker"] or "none"
    print(
        "doctor: PASS services=3 database=wal/fk/ok secrets=external account_remaining=unknown "
        f"release_blocker={blocker}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
