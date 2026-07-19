from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.export_basket03_mvp_report import (
    CODE_BASE_COMMIT,
    EVALUATION_ID,
    EXECUTED_APP_COMMIT,
    EXPECTED_CASES,
    ExportError,
    export_report,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(root: Path, *, inject_runtime_id: bool = False) -> Path:
    source = root / "source"
    cases = []
    total_tokens = 0
    total_cost = 0.0
    for index, case_id in enumerate(EXPECTED_CASES, start=1):
        failed = case_id in {"B05", "B08"}
        validation_only = case_id in {"B11", "B12", "B13"}
        prompt_tokens = 0 if validation_only else index * 10
        completion_tokens = 0 if validation_only else index
        total_tokens += prompt_tokens + completion_tokens
        total_cost += 0.0 if validation_only else index / 1000
        output = {
            "case_id": case_id,
            "passed": not failed,
            "mode": "validation_only" if validation_only else "live_ouroboros",
            "actual_initial": "READY",
            "actual_terminal": "FAILED" if failed else "APPROVABLE",
            "expected_initial": "READY",
            "expected_terminal": "APPROVABLE",
            "actual_channels": {
                "sms": "NOT_RUN" if validation_only else "GENERATED",
                "email": "NOT_RUN" if validation_only else "GENERATED",
            },
            "expected_channels": {"sms": "GENERATED", "email": "GENERATED"},
            "assertions": {"grounded": True},
            "input": {
                "case_id": case_id,
                "synthetic": True,
                "title": f"Case {case_id}",
                "campaign_id": f"campaign_{'a' * 32}",
                "brief": {
                    "channels": ["sms", "email"],
                    "name": "Synthetic",
                    "objective": "Проверка",
                    "notes": "Нет реальных данных",
                    "product_id": "synthetic_product",
                    "segment_id": "segment_test",
                    "tone": "деловой",
                    "synthetic": True,
                },
                "expected": {"status": "READY"},
            },
            "validation": {"status": "READY", "blockers": [], "questions": []},
            "package": None,
            "run": None,
            "metrics": {
                "provider_calls": 0 if validation_only else 1,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "cost_usd": 0.0 if validation_only else index / 1000,
                "user_visible_terminal_ms": index,
                "workflow_elapsed_ms": index,
                "full_worker_occupancy_ms": index,
                "usage_complete": True,
            },
        }
        if not validation_only:
            text = "Тест" if not inject_runtime_id or case_id != "B01" else f"task_{'b' * 32}"
            output["package"] = {
                "package_version": 1,
                "bundle": {
                    "sms": {"text": text, "cta_url": "https://example.test"},
                    "email": {"subject": "Тест", "plain_text": "Тест"},
                    "claim_evidence": [],
                },
                "quality_report": {
                    "approvable": True,
                    "deterministic_score": 100,
                    "checked_ids": ["QA01"],
                    "findings": [],
                },
            }
            output["run"] = {
                "status": "FAILED" if failed else "COMPLETED",
                "reason_code": "TOOL_SEQUENCE_INVALID" if failed else None,
                "tool_receipts": ["mcp_factory__cf_draft_save"] * (2 if failed else 1),
            }
        _write_json(source / "cases" / case_id / "outcome.json", output)
        cases.append({"case_id": case_id, "passed": not failed})

    report = {
        "evaluation_id": EVALUATION_ID,
        "app_commit": EXECUTED_APP_COMMIT,
        "status": "FAIL",
        "release_targets_passed": False,
        "business_case_count": 15,
        "passed_case_count": 13,
        "release_blockers": ["B05_FAILED", "B08_FAILED"],
        "provider_calls": 12,
        "provider_profile": "test",
        "execution_kind": "live",
        "generated_at": "2026-07-15T00:00:00Z",
        "no_send": True,
        "canonical_latency_passed": False,
        "functional_quality_passed": False,
        "latency": {"p50_ms": 5, "p95_ms": 10, "max_ms": 15},
        "usage": {
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 8),
            "usage_complete": True,
        },
        "cases": cases,
    }
    _write_json(source / "report.json", report)
    _write_json(source / "attempt.json", {"provider": "openrouter", "model": "z-ai/glm-5.2"})
    manifest_lines = []
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.name not in {"checksums.sha256", "FAILED.json"}:
            manifest_lines.append(f"{_sha256(path)}  {path.relative_to(source).as_posix()}")
    (source / "checksums.sha256").write_text("\n".join(manifest_lines) + "\n", encoding="ascii")
    _write_json(
        source / "FAILED.json",
        {
            "status": "FAILED",
            "report_sha256": _sha256(source / "report.json"),
            "checksums_sha256": _sha256(source / "checksums.sha256"),
        },
    )
    return source


def test_export_preserves_outputs_and_removes_runtime_ids(tmp_path: Path) -> None:
    source = _fixture(tmp_path)
    output = tmp_path / "report"

    report = export_report(source, output)

    assert report["result"]["failed_cases"] == ["B05", "B08"]
    assert report["source"]["recommended_code_base_commit"] == CODE_BASE_COMMIT
    assert (output / "cases" / "B01.json").is_file()
    assert (output / "summary.csv").read_text(encoding="utf-8-sig").count("\n") == 16
    exported = (output / "basket03-report.json").read_text(encoding="utf-8")
    assert "campaign_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in exported
    assert "FAILED_NON_EVIDENCE" in exported
    assert "B05" in (output / "report.html").read_text(encoding="utf-8")


def test_export_fails_closed_on_runtime_id_in_selected_output(tmp_path: Path) -> None:
    source = _fixture(tmp_path, inject_runtime_id=True)

    with pytest.raises(ExportError, match="forbidden runtime/secret-like token"):
        export_report(source, tmp_path / "report")
