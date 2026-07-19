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

from scripts.smoke import (
    _assert_service_isolation,
    _compose,
    _run,
    _smoke_environment,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS_ROOT = ROOT / "runtime" / "playwright" / "results"
REPORT_PATH = ROOT / "runtime" / "playwright" / "e2e-latest.json"


class E2EError(RuntimeError):
    pass


def validate_playwright_results(root: pathlib.Path = RESULTS_ROOT) -> dict[str, int]:
    try:
        last_run = json.loads((root / ".last-run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise E2EError("Playwright result marker is unreadable") from exc
    screenshots = len(list(root.rglob("*.png")))
    traces = len(list(root.rglob("trace.zip")))
    if (
        not isinstance(last_run, dict)
        or last_run.get("status") != "passed"
        or last_run.get("failedTests") != []
        or screenshots < 8
        or traces != 5
    ):
        raise E2EError("Playwright matrix/golden artifact contract failed")
    return {"screenshot_count": screenshots, "trace_count": traces}


def run_e2e() -> dict[str, Any]:
    project = f"cf-e2e-{secrets.token_hex(6)}"
    username = f"e2e_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(24)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    environment = _smoke_environment(username, password_hash, secrets.token_urlsafe(48))
    for name in (
        "ALLOW_LIVE_EVAL",
        "ALLOW_LIVE_PROBE",
        "ALLOW_GATE2_LIVE",
        "ALLOW_BOOTSTRAP_REVIEW",
        "EVALUATION_ID",
    ):
        environment.pop(name, None)
    cleanup_failed = False
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
            raise E2EError("ephemeral Playwright gateway port is invalid") from exc
        shutil.rmtree(RESULTS_ROOT, ignore_errors=True)
        RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
        browser_environment = dict(environment)
        browser_environment.update(
            {
                "CF_UI_USERNAME": username,
                "CF_UI_PASSWORD": password,
                "PLAYWRIGHT_BASE_URL": f"http://127.0.0.1:{port}",
            }
        )
        process = subprocess.run(
            ["npm", "run", "e2e"],
            cwd=ROOT,
            env=browser_environment,
            timeout=300,
            check=False,
        )
        if process.returncode != 0:
            raise E2EError("repository-pinned Playwright suite failed")
        counts = validate_playwright_results()
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "PASS",
            "services": list(services),
            "scenario_count": 15,
            "golden_consecutive_count": 5,
            "narrow_viewport_count": 1,
            **counts,
            "provider_calls": 0,
            "ouroboros_started": False,
        }
    finally:
        cleanup = _compose(
            project,
            ["down", "--volumes", "--remove-orphans", "--timeout", "10"],
            environment=environment,
            timeout=60,
            check=False,
        )
        cleanup_failed = cleanup.returncode != 0
        password = ""
        password_hash = ""
        if cleanup_failed and sys.exc_info()[0] is None:
            raise E2EError("ephemeral Playwright Compose cleanup failed")


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
        report = run_e2e()
        _write_report(report)
    except (OSError, ValueError, E2EError, subprocess.SubprocessError) as exc:
        print(f"e2e: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "e2e: PASS scenarios=15 golden=5 narrow=1 provider_calls=0 "
        f"screenshots={report['screenshot_count']} traces={report['trace_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
