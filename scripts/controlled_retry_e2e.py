from __future__ import annotations

import json
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import bcrypt

from scripts.smoke import _assert_service_isolation, _compose, _smoke_environment

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS_ROOT = ROOT / "runtime" / "playwright" / "controlled-retry"
REPORT_PATH = ROOT / "runtime" / "playwright" / "controlled-retry-latest.json"
BIND_MOUNT_PATHS = (
    pathlib.Path("runtime/contracts"),
    pathlib.Path("artifacts/evidence"),
)


class ControlledRetryE2EError(RuntimeError):
    pass


def prepare_bind_mounts(root: pathlib.Path = ROOT) -> None:
    """Prevent Docker from creating project bind mounts as root."""

    for relative in BIND_MOUNT_PATHS:
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir() or path.stat().st_uid != os.getuid() or not os.access(path, os.W_OK):
            raise ControlledRetryE2EError(
                f"retry E2E bind mount is not owned and writable: {relative.as_posix()}"
            )


def _run_profile(*, fault_profile: str, expected: str) -> dict[str, Any]:
    project = f"cf-retry-e2e-{expected}-{secrets.token_hex(5)}"
    username = f"retry_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(24)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    environment = _smoke_environment(username, password_hash, secrets.token_urlsafe(48))
    environment.update(
        {
            "APP_ENV": "test",
            "DEFAULT_EXECUTION_MODE": "live_ouroboros",
            "CONTROLLED_PROVIDER_RETRY_ENABLED": "true",
            "CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE": fault_profile,
        }
    )
    for name in (
        "ALLOW_LIVE_EVAL",
        "ALLOW_LIVE_PROBE",
        "ALLOW_GATE2_LIVE",
        "EVALUATION_ID",
    ):
        environment.pop(name, None)
    output = RESULTS_ROOT / expected
    try:
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
            raise ControlledRetryE2EError("retry E2E gateway port is invalid") from exc
        shutil.rmtree(output, ignore_errors=True)
        output.mkdir(parents=True, exist_ok=True)
        browser_environment = dict(environment)
        browser_environment.update(
            {
                "CF_UI_USERNAME": username,
                "CF_UI_PASSWORD": password,
                "PLAYWRIGHT_BASE_URL": f"http://127.0.0.1:{port}",
                "PLAYWRIGHT_OUTPUT_DIR": str(output),
                "RETRY_E2E_EXPECTED": expected,
            }
        )
        process = subprocess.run(
            [
                "npm",
                "exec",
                "playwright",
                "test",
                "tests/e2e/controlled-provider-retry.spec.ts",
            ],
            cwd=ROOT,
            env=browser_environment,
            timeout=120,
            check=False,
        )
        if process.returncode != 0:
            raise ControlledRetryE2EError(f"retry E2E {expected} scenario failed")
        screenshots = len(list(output.rglob("*.png")))
        if screenshots != 1:
            raise ControlledRetryE2EError("retry E2E screenshot contract failed")
        return {
            "expected": expected,
            "fault_profile": fault_profile,
            "services": list(services),
            "screenshot_count": screenshots,
            "provider_calls": 0,
            "ouroboros_started": False,
        }
    finally:
        _compose(
            project,
            ["down", "--volumes", "--remove-orphans", "--timeout", "10"],
            environment=environment,
            timeout=60,
            check=False,
        )
        password = ""
        password_hash = ""


def run_controlled_retry_e2e() -> dict[str, Any]:
    prepare_bind_mounts()
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    scenarios = [
        _run_profile(fault_profile="transient_then_success", expected="success"),
        _run_profile(fault_profile="transient_twice", expected="failure"),
    ]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "scenario_count": len(scenarios),
        "provider_calls": 0,
        "scenarios": scenarios,
    }


def _write_report(report: dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, REPORT_PATH)


def main() -> int:
    try:
        report = run_controlled_retry_e2e()
        _write_report(report)
    except (OSError, ValueError, ControlledRetryE2EError, subprocess.SubprocessError) as exc:
        print(f"controlled-retry-e2e: FAIL: {exc}", file=sys.stderr)
        return 1
    print("controlled-retry-e2e: PASS scenarios=2 screenshots=2 provider_calls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
