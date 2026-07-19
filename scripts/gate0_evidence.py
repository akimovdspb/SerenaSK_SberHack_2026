from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from typing import Any

from apps.api.app.live_probe_transport import LEDGER_CATEGORIES

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS = ROOT / "runtime" / "budget" / "runs"
PROBES = ROOT / "runtime" / "live-probes"
CONTRACT_LOCK = ROOT / "runtime" / "contracts" / "communication_factory.lock.json"
EXPECTED_TOOLS = ["mcp_factory__cf_context_get", "mcp_factory__cf_draft_save"]
REQUIRED_TIMESTAMPS = {
    "task_created",
    "context_tool_completed",
    "draft_saved",
    "task_result_persisted",
    "task_terminal",
    "worker_released",
}


def _load_object(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return {str(key): item for key, item in value.items()}


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _field(document: dict[str, Any], key: str) -> dict[str, Any]:
    return _object(document.get(key))


def latest_completed_run(runs: pathlib.Path = RUNS) -> tuple[pathlib.Path, dict[str, Any]]:
    candidates: list[tuple[pathlib.Path, dict[str, Any]]] = []
    for path in runs.glob("*.json"):
        try:
            marker = _load_object(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if marker.get("kind") == "gate0_live_probe" and marker.get("status") == "completed":
            candidates.append((path, marker))
    if not candidates:
        raise ValueError("no completed Gate 0 live probe exists")
    return max(candidates, key=lambda item: str(item[1].get("finished_at") or ""))


def validate_gate0_evidence(
    *,
    runs: pathlib.Path = RUNS,
    probes: pathlib.Path = PROBES,
    contract_lock_path: pathlib.Path = CONTRACT_LOCK,
) -> tuple[str, list[str]]:
    errors: list[str] = []
    try:
        _, marker = latest_completed_run(runs)
        run_id = str(marker.get("run_id") or "")
        report_path = probes / run_id / "report.json"
        report = _load_object(report_path)
        contract_lock = _load_object(contract_lock_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "", [str(exc)]
    report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
    if marker.get("report_sha256") != report_hash:
        errors.append("live probe report hash differs from its run marker")
    if marker.get("usage_complete") is not True or report.get("ok") is not True:
        errors.append("live probe run is not complete and green")
    checks = _field(report, "checks")
    if not checks or not all(value is True for value in checks.values()):
        errors.append("live probe engineering checks are not all true")
    timestamps = _field(report, "timestamps")
    if set(timestamps) != REQUIRED_TIMESTAMPS or not all(timestamps.values()):
        errors.append("live probe lifecycle timestamps are incomplete")
    latency = _field(report, "latency_ms")
    if (
        int(latency.get("user_visible") or 30_000) >= 30_000
        or int(latency.get("full_worker_occupancy") or 30_000) >= 30_000
    ):
        errors.append("live probe latency reached the 30-second terminal limit")

    ledger = _field(report, "provider_call_ledger")
    if set(ledger) != set(LEDGER_CATEGORIES):
        errors.append("live probe provider-call ledger categories are incomplete")
    for category, raw in ledger.items():
        row = _object(raw)
        if int(row.get("call_count") or 0) > 0 and row.get("providers") != ["openai"]:
            errors.append(f"live probe provider route drifted in {category}")
    if int(_field(ledger, "main_generation").get("call_count") or 0) <= 0:
        errors.append("live probe has no main generation usage")
    if int(_field(ledger, "post_task_summary").get("call_count") or 0) <= 0:
        errors.append("live probe did not measure post-task summary usage")
    if list(report.get("tool_receipts") or []) != EXPECTED_TOOLS:
        errors.append("live probe does not contain the exact two logical tool receipts")

    task = _field(report, "task")
    draft = _field(report, "draft")
    try:
        final_answer = _object(json.loads(str(task.get("final_answer") or "")))
    except json.JSONDecodeError:
        final_answer = {}
    if (
        task.get("status") != "completed"
        or final_answer.get("status") != "SAVED"
        or final_answer.get("draft_id") != draft.get("draft_id")
        or not draft.get("draft_hash")
    ):
        errors.append("live probe final receipt does not bind the persisted draft")
    runtime = _field(contract_lock, "runtime")
    skill = _field(contract_lock, "skill")
    activation = _field(report, "activation")
    if report.get("runtime_image_id") != runtime.get("image_id"):
        errors.append("live probe runtime image differs from the current contract lock")
    if activation.get("prompt_hash") != skill.get("prompt_hash"):
        errors.append("live probe prompt hash differs from the current contract lock")

    retry_of = str(marker.get("retry_of") or "")
    if retry_of:
        try:
            previous = _load_object(runs / f"{retry_of}.json")
        except (OSError, ValueError, json.JSONDecodeError):
            previous = {}
        if previous.get("status") != "failed" or not (probes / retry_of / "report.json").is_file():
            errors.append("linked failed live probe evidence was not preserved")
    return run_id, errors


def main() -> int:
    run_id, errors = validate_gate0_evidence()
    if errors:
        for error in errors:
            print(f"gate0-evidence: FAIL: {error}", file=sys.stderr)
        return 1
    print(f"gate0-evidence: PASS run={run_id} tools=2 usage=complete latency=under-30s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
