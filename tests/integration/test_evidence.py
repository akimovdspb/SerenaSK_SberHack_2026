from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import subprocess
import zipfile
from datetime import UTC, datetime
from typing import Any

import pytest

from scripts import evidence as evidence_module
from scripts.evaluation import run_replay_evaluation
from scripts.evidence import (
    EvidenceError,
    _validate_review_packets,
    build_evidence,
    render_with_playwright,
    validate_evidence_directory,
    validate_live_report,
)
from scripts.package_submission import (
    SubmissionError,
    _fixture_documents,
    validate_operator_evidence_bindings,
)


def _json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _commit() -> str:
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return process.stdout.strip()


def _contract(path: pathlib.Path) -> None:
    _json(
        path,
        {
            "schema_version": 1,
            "runtime": {
                "tag": "v6.61.4",
                "commit": "a00d51dd414f794d830cacf7da760061e442fa88",
                "image_id": "sha256:" + "a" * 64,
            },
            "skill": {
                "activation_mode": "adapter_injected",
                "skill_content_hash": "b" * 64,
                "prompt_hash": "c" * 64,
            },
            "tools": {
                "inventory_hash": "d" * 64,
                "post_deny_schema_hash": "e" * 64,
                "post_deny_tool_names": [
                    "mcp_factory__cf_context_get",
                    "mcp_factory__cf_draft_save",
                ],
            },
            "mcp": {"settings_hash": "f" * 64},
        },
    )


def _frozen_report(contract_path: pathlib.Path) -> dict[str, Any]:
    report = run_replay_evaluation()
    commit = _commit()
    report.update(
        {
            "execution_kind": "live_evaluation",
            "frozen": True,
            "app_commit": commit,
            "git_dirty": False,
            "runtime_contract_hash": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            "status": "PASS",
            "live_case_count": 12,
            "release_targets_passed": True,
            "release_blockers": [],
            "provider_calls": 12,
            "rules_hash": "a" * 64,
            "primary_attempt": "test-live-evaluation",
            "repeats": [],
            "exclusions": [],
            "stability": {
                "crash_count": 0,
                "stuck_run_count": 0,
                "timeout_over_30s_count": 0,
                "unsupported_approved_claim_count": 0,
                "prompt_injection_success_count": 0,
                "blocker_approval_success_count": 0,
                "duplicate_paid_generation_count": 0,
            },
        }
    )
    for index, case in enumerate(report["cases"], start=1):
        if case["live_target"]:
            case["mode"] = "live_ouroboros"
            case["package"]["mode"] = "live_ouroboros"
            case["run"] = {
                "run_id": f"run_evidence_{index:02d}",
                "status": "COMPLETED",
                "mode": "live_ouroboros",
                "tool_receipts": [
                    "mcp_factory__cf_context_get",
                    "mcp_factory__cf_draft_save",
                ],
            }
            case["task"] = {"task_id": f"task_evidence_{index:02d}", "status": "completed"}
            case["context"] = {
                "context_version": case["package"]["context_version"],
                "source_manifest": [],
                "content_plan": {},
                "rules_version": "b" * 64,
                "active_rules": [],
            }
            case["metrics"] = {
                "usage_complete": True,
                "user_visible_terminal_ms": 1_000 + index,
                "full_worker_occupancy_ms": 1_200 + index,
                "provider_calls": 1,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cached_tokens": 0,
                "cost_usd": 0.001,
                "usage_by_category": {
                    "main_generation": {
                        "calls": 1,
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "cached_tokens": 0,
                        "cost_usd": 0.001,
                    }
                },
            }
            case["provider_call_ledger"] = case["metrics"]["usage_by_category"]
            case["safe_events"] = [{"type": "task_result", "status": "completed"}]
            case["mcp_calls"] = [
                {"tool": "mcp_factory__cf_context_get", "status": "completed"},
                {"tool": "mcp_factory__cf_draft_save", "status": "completed"},
            ]
        else:
            case["metrics"] = {
                "usage_complete": True,
                "user_visible_terminal_ms": 0,
                "full_worker_occupancy_ms": 0,
                "provider_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
                "cost_usd": 0.0,
                "usage_by_category": {},
            }
    report["learning"].update(
        {
            "clarification": {
                "question_ids": ["missing_cta_label", "missing_cta_url"],
                "llm_calls": 0,
            },
            "rule_tests": report["learning"]["rule_proposal"]["tests"],
            "package_approval": {
                "approval_id": "approval_test_evidence",
                "test_only": True,
                "package_id": report["learning"]["package_v2"]["package_id"],
                "package_hash": report["learning"]["package_v2"]["package_hash"],
            },
        }
    )
    return report


def _freeze_source(root: pathlib.Path, report: dict[str, Any]) -> None:
    root.mkdir(parents=True)
    _json(root / "report.json", report)
    export = root / "demo-case" / "campaign-export.zip"
    export.parent.mkdir()
    with zipfile.ZipFile(export, "w") as archive:
        archive.writestr("manifest.json", '{"synthetic":true,"no_send":true}')
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name in {"checksums.sha256", "FROZEN.json"}:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = path.relative_to(root).as_posix()
        rows.append(f"{digest}  {relative}\n")
    (root / "checksums.sha256").write_text("".join(rows), encoding="utf-8")
    _json(
        root / "FROZEN.json",
        {
            "schema_version": 1,
            "evaluation_id": report["evaluation_id"],
            "report_sha256": hashlib.sha256((root / "report.json").read_bytes()).hexdigest(),
            "checksums_sha256": hashlib.sha256(
                (root / "checksums.sha256").read_bytes()
            ).hexdigest(),
        },
    )


def _chaos(path: pathlib.Path) -> None:
    _json(
        path,
        {
            "schema_version": 1,
            "status": "PASS",
            "chaos_case_count": 5,
            "passed_case_count": 5,
            "provider_calls": 0,
            "normal_metrics_included": False,
            "cases": [
                {
                    "case_id": f"X{index:02d}",
                    "passed": True,
                    "under_30_seconds": True,
                    "duration_ms": index,
                }
                for index in range(1, 6)
            ],
        },
    )


def _security(path: pathlib.Path) -> None:
    _json(
        path,
        {
            "schema_version": 1,
            "status": "PASS",
            "secret_values_in_report": False,
            "finding_counts": {"tree": 0, "history": 0, "artifacts": 0},
        },
    )


def _browser(root: pathlib.Path) -> None:
    for index in range(1, 6):
        case_root = root / f"golden-flow-{index}"
        case_root.mkdir(parents=True)
        (case_root / f"golden-{index}.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        with zipfile.ZipFile(case_root / "trace.zip", "w") as archive:
            archive.writestr("trace.json", "{}")


def _renderer(html_path: pathlib.Path, pdf_path: pathlib.Path, jpg_path: pathlib.Path) -> None:
    assert "<main" in html_path.read_text(encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.4\nfixture\n%%EOF\n")
    jpg_path.write_bytes(b"\xff\xd8fixture\xff\xd9")


def _build(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    contract_path = tmp_path / "communication_factory.lock.json"
    _contract(contract_path)
    monkeypatch.setattr(evidence_module, "CONTRACT_LOCK_PATH", contract_path)
    source = tmp_path / "live" / "test-live"
    _freeze_source(source, _frozen_report(contract_path))
    chaos = tmp_path / "chaos.json"
    security = tmp_path / "security.json"
    browser = tmp_path / "browser"
    _chaos(chaos)
    _security(security)
    _browser(browser)
    return build_evidence(
        source_root=source,
        chaos_path=chaos,
        security_path=security,
        browser_root=browser,
        output_root=tmp_path / "evidence",
        renderer=_renderer,
        require_clean_git=False,
        built_at=datetime(2026, 7, 12, tzinfo=UTC),
    )


def test_evidence_builder_creates_all_offline_formats_and_blank_review_packets(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _build(tmp_path, monkeypatch)

    manifest = validate_evidence_directory(destination)
    assert manifest["evidence_kind"] == "implementation"
    assert manifest["human_gate_status"] == "WAITING_FOR_OPERATOR"
    assert manifest["human_packet_count"] == 6
    assert manifest["browser_evidence"] == {
        "golden_screenshots": 5,
        "golden_traces": 5,
        "auth_headers_redacted": 0,
    }
    assert (destination / "report.pdf").read_bytes().startswith(b"%PDF-")
    assert (destination / "report.jpg").read_bytes().startswith(b"\xff\xd8")
    with (destination / "business-results.csv").open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 15
    assert len((destination / "business-results.jsonl").read_text().splitlines()) == 15
    qualitative = json.loads((destination / "qualitative-review.json").read_text())
    assert qualitative["preselected_case_ids"] == ["B01", "B02", "B03", "B04", "B07", "B08"]
    for packet in qualitative["packets"]:
        packet_path = destination / packet["packet_ref"]
        assert packet["packet_sha256"] == hashlib.sha256(packet_path.read_bytes()).hexdigest()
        form = json.loads((destination / packet["form_ref"]).read_text())
        assert form["reviewer_role"] is None
        assert form["reviewer_id"] is None
        assert form["completed_at"] is None
        assert form["comments"] is None
        assert set(form["scores"].values()) == {None}


def test_operator_records_bind_to_frozen_packets_and_approval_targets(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _build(tmp_path, monkeypatch)
    manifest = validate_evidence_directory(destination)
    qualitative = json.loads((destination / "qualitative-review.json").read_text())
    documents = _fixture_documents()
    packet_hashes = {row["case_id"]: row["packet_sha256"] for row in qualitative["packets"]}
    for review in documents["qualitative-reviews.json"]["reviews"]:
        review["packet_sha256"] = packet_hashes[review["case_id"]]
    targets = manifest["submission_approval_targets"]
    for label in ("rule", "package"):
        documents["approvals.json"][label].update(targets[label])

    result = validate_operator_evidence_bindings(
        documents,
        destination,
        expected_evaluation_id=str(manifest["evaluation_id"]),
    )

    assert result["packet_binding_count"] == 6
    assert result["approval_binding_count"] == 2

    documents["qualitative-reviews.json"]["reviews"][0]["packet_sha256"] = "f" * 64
    with pytest.raises(SubmissionError, match="frozen packet"):
        validate_operator_evidence_bindings(
            documents,
            destination,
            expected_evaluation_id=str(manifest["evaluation_id"]),
        )


def test_evidence_rejects_replay_and_detects_tampering(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(EvidenceError, match="only a live evaluation"):
        validate_live_report(run_replay_evaluation())

    destination = _build(tmp_path, monkeypatch)
    metrics = destination / "metrics.json"
    metrics.write_text(metrics.read_text() + " ", encoding="utf-8")
    with pytest.raises(EvidenceError, match="checksum inventory"):
        validate_evidence_directory(destination)


def test_review_packet_index_rejects_case_or_path_drift(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _build(tmp_path, monkeypatch)
    qualitative_path = destination / "qualitative-review.json"
    qualitative = json.loads(qualitative_path.read_text())
    qualitative["packets"][0]["case_id"] = "B15"
    _json(qualitative_path, qualitative)

    with pytest.raises(EvidenceError, match="malformed or fabricated"):
        _validate_review_packets(destination)


@pytest.mark.integration
def test_playwright_renderer_creates_offline_pdf_and_jpeg(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "report.html"
    source.write_text(
        "<!doctype html><html><meta charset='utf-8'><body><main>Evidence</main></body></html>",
        encoding="utf-8",
    )
    pdf = tmp_path / "report.pdf"
    jpg = tmp_path / "report.jpg"

    render_with_playwright(source, pdf, jpg)

    assert pdf.read_bytes().startswith(b"%PDF-")
    assert jpg.read_bytes().startswith(b"\xff\xd8")
    assert jpg.read_bytes().endswith(b"\xff\xd9")
