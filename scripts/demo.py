from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from scripts.smoke import _json_body, _request

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "runtime" / "demo" / "latest.json"
CANARY_ROOT = ROOT / "runtime" / "demo" / "canaries"
ACCESS_PATH = ROOT / "runtime" / "operator" / "access.txt"


class DemoError(RuntimeError):
    pass


def _run(
    arguments: list[str],
    *,
    timeout: int = 120,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["docker", "compose", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if process.returncode != 0:
        raise DemoError("project Compose demo command failed")
    return process


def _container_result(action: str) -> dict[str, Any]:
    process = _run(
        [
            "exec",
            "-T",
            "app",
            "python",
            "-m",
            "apps.api.app.demo_admin",
            action,
        ]
    )
    try:
        value = json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise DemoError("demo container returned no safe JSON") from exc
    if not isinstance(value, dict) or value.get("status") != "PASS":
        raise DemoError("demo container check failed")
    return {str(key): item for key, item in value.items()}


def _credentials(path: pathlib.Path = ACCESS_PATH) -> tuple[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    except OSError as exc:
        raise DemoError("local gateway access file is unavailable; run make init") from exc
    username = values.get("username", "")
    password = values.get("password", "")
    if not username or not password:
        raise DemoError("local gateway access file is incomplete")
    return username, password


def _gateway_port() -> int:
    line = _run(["port", "gateway", "8080"], timeout=30).stdout.strip()
    try:
        return int(line.rsplit(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise DemoError("gateway port is unavailable") from exc


def _git_commit() -> str:
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    commit = process.stdout.strip()
    if process.returncode != 0 or len(commit) != 40:
        raise DemoError("current Git commit is unavailable")
    return commit


def _load_contract() -> tuple[dict[str, Any], str]:
    path = ROOT / "runtime" / "contracts" / "communication_factory.lock.json"
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoError("runtime contract lock is unavailable") from exc
    if not isinstance(contract, dict):
        raise DemoError("runtime contract lock is malformed")
    return contract, hashlib.sha256(path.read_bytes()).hexdigest()


def _frozen_release(commit: str) -> tuple[dict[str, Any], pathlib.Path]:
    from scripts.evidence import validate_evidence_directory, validate_frozen_live_directory

    evaluation_id = str(os.environ.get("DEMO_EVALUATION_ID") or "").strip()
    live_root = ROOT / "runtime" / "evaluation" / "live"
    candidates = [
        path
        for path in sorted(live_root.iterdir() if live_root.is_dir() else [])
        if path.is_dir()
        and (path / "FROZEN.json").is_file()
        and (not evaluation_id or path.name == evaluation_id)
    ]
    if len(candidates) != 1:
        raise DemoError("demo requires exactly one selected frozen live evaluation")
    report = validate_frozen_live_directory(candidates[0])
    if report.get("app_commit") != commit or report.get("git_dirty") is not False:
        raise DemoError("demo frozen evaluation differs from the current clean commit")
    evidence_matches: list[pathlib.Path] = []
    evidence_root = ROOT / "artifacts" / "evidence"
    for path in sorted(evidence_root.iterdir() if evidence_root.is_dir() else []):
        if not path.is_dir():
            continue
        try:
            manifest = validate_evidence_directory(path)
        except RuntimeError:
            continue
        if manifest.get("evaluation_id") == report.get("evaluation_id"):
            evidence_matches.append(path)
    if len(evidence_matches) != 1:
        raise DemoError("demo requires one matching immutable implementation evidence directory")
    return report, evidence_matches[0]


def _canary(commit: str, contract_hash: str) -> dict[str, Any]:
    canary_id = str(os.environ.get("DEMO_CANARY_ID") or "").strip()
    candidates = [
        path
        for path in sorted(CANARY_ROOT.glob("*.json"))
        if not canary_id or path.stem == canary_id
    ]
    valid: list[dict[str, Any]] = []
    for path in candidates:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(row, dict)
            and row.get("status") == "PASS"
            and row.get("app_commit") == commit
            and row.get("runtime_contract_hash") == contract_hash
        ):
            valid.append({str(key): value for key, value in row.items()})
    if len(valid) != 1:
        raise DemoError("demo requires exactly one selected current live canary")
    canary = valid[0]
    try:
        generated = datetime.fromisoformat(str(canary["generated_at"]))
    except (KeyError, ValueError) as exc:
        raise DemoError("demo canary timestamp is invalid") from exc
    if generated.tzinfo is None or datetime.now(UTC) - generated.astimezone(UTC) > timedelta(
        hours=24
    ):
        raise DemoError("demo canary is older than 24 hours")
    run_id = str(canary.get("run_id") or "")
    report_path = ROOT / "runtime" / "live-campaigns" / run_id / "report.json"
    if (
        not report_path.is_file()
        or hashlib.sha256(report_path.read_bytes()).hexdigest() != canary.get("report_sha256")
        or canary.get("case_id") != "B04"
        or canary.get("mode") != "live_ouroboros"
        or canary.get("excluded_from_evaluation_metrics") is not True
    ):
        raise DemoError("demo canary evidence is missing or drifted")
    return canary


def check_demo(*, require_release: bool = True) -> dict[str, Any]:
    state = _container_result("check")
    username, password = _credentials()
    base_url = f"http://127.0.0.1:{_gateway_port()}"
    status, _, body, _ = _request(
        base_url,
        "/api/v1/health",
        username=username,
        password=password,
    )
    health = _json_body(body)
    if (
        status != 200
        or health.get("data_mode") != "synthetic_only"
        or health.get("external_send_enabled") is not False
    ):
        raise DemoError("authenticated demo health contract failed")
    status, _, body, _ = _request(
        base_url,
        "/api/v1/cases",
        username=username,
        password=password,
    )
    try:
        cases = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemoError("demo case response is malformed") from exc
    if (
        status != 200
        or not isinstance(cases, list)
        or [item.get("case_id") for item in cases if isinstance(item, dict)]
        != [f"B{ordinal:02d}" for ordinal in range(1, 16)]
    ):
        raise DemoError("gateway demo basket does not contain exact B01-B15")
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "action": "check",
        "catalog_case_count": state["catalog_case_count"],
        "observed_case_count": state["observed_case_count"],
        "provider_calls": 0,
        "synthetic": True,
        "no_send": True,
        "gateway_authenticated": True,
        "b01_ready": state["b01_ready"],
        "b03_ready": state["b03_ready"],
        "release_ready": False,
    }
    if not require_release:
        return report
    from scripts.live_evaluation import validate_strict_contract

    contract, contract_hash = _load_contract()
    try:
        validate_strict_contract(contract)
    except RuntimeError as exc:
        raise DemoError(str(exc)) from exc
    commit = _git_commit()
    frozen, evidence_root = _frozen_release(commit)
    canary = _canary(commit, contract_hash)
    status, _, body, _ = _request(
        base_url,
        "/api/v1/diagnostics",
        username=username,
        password=password,
    )
    diagnostics = _json_body(body)
    if (
        status != 200
        or diagnostics.get("admission_state") != "CLOSED"
        or diagnostics.get("discovered_tools")
        != ["mcp_factory__cf_context_get", "mcp_factory__cf_draft_save"]
    ):
        raise DemoError("demo runtime/MCP/skill diagnostics are not release-ready")
    report.update(
        {
            "git_commit": commit,
            "evaluation_id": frozen["evaluation_id"],
            "live_case_count": frozen["live_case_count"],
            "evidence_path": evidence_root.relative_to(ROOT).as_posix(),
            "runtime_contract_hash": contract_hash,
            "runtime_tag": (contract.get("runtime") or {}).get("tag"),
            "provider_tool_names": diagnostics["discovered_tools"],
            "canary_run_id": canary["run_id"],
            "canary_excluded_from_evaluation_metrics": True,
            "release_ready": True,
        }
    )
    return report


def reset_demo() -> dict[str, Any]:
    _run(["stop", "gateway", "app"], timeout=60)
    try:
        process = _run(
            [
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "python",
                "app",
                "-m",
                "apps.api.app.demo_admin",
                "reset",
            ],
            timeout=120,
        )
        try:
            reset = json.loads(process.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise DemoError("demo reset returned no safe JSON") from exc
        if not isinstance(reset, dict) or reset.get("status") != "PASS":
            raise DemoError("demo reset failed")
    finally:
        _run(
            ["up", "--detach", "--wait", "--wait-timeout", "90", "--no-build", "app", "gateway"],
            timeout=120,
        )
    checked = check_demo(require_release=False)
    if checked["observed_case_count"] != 0:
        raise DemoError("demo reset left mutable campaigns behind")
    return {**checked, "action": "reset", "removed_database_files": reset["removed_database_files"]}


def run_canary(environment: dict[str, str] | None = None) -> dict[str, Any]:
    effective = dict(environment if environment is not None else os.environ)
    if effective.get("ALLOW_DEMO_CANARY", "").casefold() != "true":
        raise DemoError("demo canary requires ALLOW_DEMO_CANARY=true")
    run_id = str(effective.get("DEMO_CANARY_ID") or "").strip()
    if not run_id or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
        for character in run_id
    ):
        raise DemoError("DEMO_CANARY_ID is invalid")
    for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        if effective.get(name):
            raise DemoError("host demo canary process must not receive provider credentials")
    destination = CANARY_ROOT / f"{run_id}.json"
    if destination.exists() or (ROOT / "runtime" / "live-campaigns" / run_id).exists():
        raise DemoError("demo canary ID was already used")
    contract, contract_hash = _load_contract()
    from scripts.live_evaluation import validate_strict_contract

    try:
        validate_strict_contract(contract)
    except RuntimeError as exc:
        raise DemoError(str(exc)) from exc
    commit = _git_commit()
    _frozen_release(commit)
    canary_environment = dict(effective)
    canary_environment.update(
        {
            "ALLOW_GATE2_LIVE": "true",
            "EVALUATION_ID": run_id,
        }
    )
    process = _run(
        ["make", "gate2-live-pilot"],
        timeout=180,
        environment=canary_environment,
    )
    if process.returncode != 0:
        raise DemoError("guarded demo canary failed")
    report_path = ROOT / "runtime" / "live-campaigns" / run_id / "report.json"
    try:
        live = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoError("demo canary report is missing") from exc
    package = live.get("package") if isinstance(live, dict) else None
    if (
        not isinstance(live, dict)
        or live.get("ok") is not True
        or not isinstance(package, dict)
        or package.get("mode") != "live_ouroboros"
        or not isinstance(package.get("quality_report"), dict)
        or package["quality_report"].get("approvable") is not True
    ):
        raise DemoError("demo canary is not a QA-green live B04 package")
    latency = live.get("latency_ms")
    if (
        not isinstance(latency, dict)
        or int(latency.get("user_visible_terminal") or 30_000) >= 30_000
    ):
        raise DemoError("demo canary exceeded the terminal latency limit")
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "action": "canary",
        "run_id": run_id,
        "case_id": "B04",
        "mode": "live_ouroboros",
        "app_commit": commit,
        "runtime_contract_hash": contract_hash,
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "user_visible_terminal_ms": int(latency["user_visible_terminal"]),
        "excluded_from_evaluation_metrics": True,
        "account_remaining": "unknown",
    }
    CANARY_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return result


def _write_report(report: dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, REPORT_PATH)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("reset", "check", "canary"))
    args = parser.parse_args(argv)
    try:
        report = (
            reset_demo()
            if args.action == "reset"
            else run_canary()
            if args.action == "canary"
            else check_demo()
        )
        _write_report(report)
    except (OSError, ValueError, DemoError, subprocess.SubprocessError) as exc:
        _write_report(
            {
                "schema_version": 1,
                "generated_at": datetime.now(UTC).isoformat(),
                "status": "FAIL",
                "action": args.action,
                "reason": str(exc),
                "provider_calls_started": 0 if args.action != "canary" else "unknown",
            }
        )
        print(f"demo: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        f"demo: PASS action={args.action} cases={report['catalog_case_count']} "
        f"observed={report['observed_case_count']} provider_calls=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
