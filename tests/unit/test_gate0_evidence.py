from __future__ import annotations

import hashlib
import json
import pathlib

from apps.api.app.live_probe_transport import LEDGER_CATEGORIES
from scripts.gate0_evidence import validate_gate0_evidence


def _write(path: pathlib.Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _fixture(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    runs = tmp_path / "runs"
    probes = tmp_path / "probes"
    contract = tmp_path / "contract.json"
    run_id = "gate0-live-02"
    ledger = {
        category: {
            "call_count": 1 if category in {"main_generation", "post_task_summary"} else 0,
            "providers": ["openai"] if category in {"main_generation", "post_task_summary"} else [],
        }
        for category in LEDGER_CATEGORIES
    }
    report: dict[str, object] = {
        "ok": True,
        "checks": {"one": True, "two": True},
        "timestamps": {
            "task_created": "1",
            "context_tool_completed": "2",
            "draft_saved": "3",
            "task_result_persisted": "4",
            "task_terminal": "5",
            "worker_released": "6",
        },
        "latency_ms": {"user_visible": 1000, "full_worker_occupancy": 2000},
        "provider_call_ledger": ledger,
        "tool_receipts": [
            "mcp_factory__cf_context_get",
            "mcp_factory__cf_draft_save",
        ],
        "task": {
            "status": "completed",
            "final_answer": json.dumps({"status": "SAVED", "draft_id": "draft_1"}),
        },
        "draft": {"draft_id": "draft_1", "draft_hash": "a" * 64},
        "runtime_image_id": f"sha256:{'b' * 64}",
        "activation": {"prompt_hash": "c" * 64},
    }
    report_path = probes / run_id / "report.json"
    _write(report_path, report)
    _write(
        runs / f"{run_id}.json",
        {
            "kind": "gate0_live_probe",
            "run_id": run_id,
            "status": "completed",
            "usage_complete": True,
            "finished_at": "2026-07-11T00:00:00+00:00",
            "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        },
    )
    _write(
        contract,
        {
            "runtime": {"image_id": f"sha256:{'b' * 64}"},
            "skill": {"prompt_hash": "c" * 64},
        },
    )
    return runs, probes, contract


def test_gate0_evidence_accepts_complete_bound_receipts(tmp_path: pathlib.Path) -> None:
    runs, probes, contract = _fixture(tmp_path)

    run_id, errors = validate_gate0_evidence(
        runs=runs,
        probes=probes,
        contract_lock_path=contract,
    )

    assert run_id == "gate0-live-02"
    assert errors == []


def test_gate0_evidence_rejects_tampered_report(tmp_path: pathlib.Path) -> None:
    runs, probes, contract = _fixture(tmp_path)
    report_path = probes / "gate0-live-02" / "report.json"
    report_path.write_text(report_path.read_text() + " ", encoding="utf-8")

    _, errors = validate_gate0_evidence(
        runs=runs,
        probes=probes,
        contract_lock_path=contract,
    )

    assert "live probe report hash differs from its run marker" in errors
