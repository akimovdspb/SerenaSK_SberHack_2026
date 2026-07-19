from __future__ import annotations

import copy
import dataclasses
import json
import pathlib
import zipfile
from typing import Any

import pytest

from apps.api.app import live_evaluation_transport
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.workflow.store import WorkflowStore
from scripts import live_evaluation
from scripts.budget_control import NightBudget, RunRequest
from scripts.evidence import EvidenceError, validate_checksums, validate_live_report
from tests.factories import deterministic_live_operation_adapter


def _store(tmp_path: pathlib.Path) -> WorkflowStore:
    store = WorkflowStore(
        f"sqlite:///{tmp_path / 'factory.db'}",
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "app-artifacts",
    )
    store.initialize()
    return store


def _raw_basket(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    evaluation_id: str,
) -> dict[str, dict[str, Any]]:
    store = _store(tmp_path)
    monkeypatch.setattr(
        live_evaluation_transport,
        "run_operation",
        deterministic_live_operation_adapter(),
    )
    raw: dict[str, dict[str, Any]] = {
        "B02": live_evaluation_transport._standard_case(store, "B02", evaluation_id)
    }
    raw["B01"] = live_evaluation_transport._b01(store, evaluation_id)
    active_ids, active_version = store.active_rule_state()
    raw["B03"] = live_evaluation_transport._b03(
        store,
        evaluation_id,
        rule_version_id=active_ids[0],
        active_rules_version=active_version,
    )
    for case_id in (
        "B04",
        "B05",
        "B06",
        "B07",
        "B08",
        "B09",
        "B10",
        "B11",
        "B12",
        "B13",
        "B14",
    ):
        raw[case_id] = live_evaluation_transport._standard_case(store, case_id, evaluation_id)
    raw["B15"] = live_evaluation_transport._b15(store, evaluation_id)
    return raw


def _context(evaluation_id: str, *, max_tokens: int = 10_000) -> live_evaluation.PreflightContext:
    return live_evaluation.PreflightContext(
        request=RunRequest(
            run_id=evaluation_id,
            provider="openai",
            model="gpt-5.4-mini",
            max_tokens=max_tokens,
            max_cost_usd=1.0,
            projected_tokens=3_000 if max_tokens > 1_000 else max_tokens,
            projected_cost_usd=0.1,
            concurrency=1,
        ),
        commit="a" * 40,
        branch="codex/p0-autonomous",
        contract_hash="b" * 64,
        basket_hash="c" * 64,
        image_id="sha256:" + "d" * 64,
        runtime_report={"ok": True, "provider_calls": 0},
        readiness={"status": "PASS"},
    )


def _additional_context(
    tmp_path: pathlib.Path,
    evaluation_id: str,
) -> live_evaluation.PreflightContext:
    canonical = _context(evaluation_id)
    return dataclasses.replace(
        canonical,
        branch="codex/p0-glm-basket",
        night=NightBudget(
            night_id="p0-glm-continuation-20260715-01",
            authority_path=tmp_path / "HANDOFF_VPS_P0_GLM_BASKET.md",
            authority_sha256="e" * 64,
            max_tokens=100_000_000,
            max_cost_usd=200.0,
            phase="basket",
            phase_max_tokens=100_000_000,
            phase_max_cost_usd=200.0,
            incomplete_usage_policy=("owner_authorized_confirmed_plus_bounded_per_call_estimates"),
            additional_authority=True,
            baseline_ledger_rows=1,
            baseline_ledger_sha256="f" * 64,
            baseline_confirmed_tokens=100,
            baseline_confirmed_cost_usd=0.01,
            prompt_price_usd_per_million=0.8862,
            completion_price_usd_per_million=2.785,
            estimate_safety_multiplier=2.0,
            metadata_poll_max_seconds=600,
            max_directed_attempts_per_failure_class=6,
        ),
        request=dataclasses.replace(
            canonical.request,
            provider="openrouter",
            model="z-ai/glm-5.2",
            max_tokens=100_000,
            max_cost_usd=10.0,
            openrouter_enabled=True,
            profile_name="openrouter-glm-5.2-functional",
        ),
    )


def _configure_runtime_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[pathlib.Path, pathlib.Path]:
    runs = tmp_path / "runs"
    live = tmp_path / "live"
    readiness = tmp_path / "readiness.json"
    readiness.write_text('{"status":"PASS"}\n', encoding="utf-8")
    monkeypatch.setattr(live_evaluation, "RUN_STATE_DIR", runs)
    monkeypatch.setattr(live_evaluation, "LIVE_ROOT", live)
    monkeypatch.setattr(live_evaluation, "READINESS_PATH", readiness)
    return runs, live


def _executor(raw: dict[str, dict[str, Any]]) -> live_evaluation.CaseExecutor:
    rule = raw["B01"]["learning"]["rule_approval"]

    def execute(
        case_id: str,
        evaluation_id: str,
        rule_version_id: str,
        active_rules_version: str,
    ) -> tuple[int, dict[str, Any]]:
        if case_id == "B03":
            assert rule_version_id == rule["rule_version_id"]
            assert active_rules_version == rule["rules_version"]
        report = copy.deepcopy(raw[case_id])
        assert report["evaluation_id"] == evaluation_id
        return 0, report

    return execute


def _copy_export(_source: str, destination: pathlib.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("manifest.json", '{"synthetic":true,"no_send":true}')


@pytest.mark.integration
def test_full_runner_freezes_all_cases_sequentially_without_relabelling_validation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_id = "full-live-test-01"
    raw = _raw_basket(tmp_path, monkeypatch, evaluation_id)
    runs, live = _configure_runtime_paths(tmp_path, monkeypatch)
    usage_rows: list[dict[str, Any]] = []

    report = live_evaluation.run_live_evaluation(
        _context(evaluation_id),
        environment={},
        executor=_executor(raw),
        export_copier=_copy_export,
        rule_cleanup=lambda _rule, _version: {"ok": True},
        usage_appender=usage_rows.extend,
    )

    assert report["status"] == "PASS"
    assert report["release_targets_passed"] is True
    assert report["business_case_count"] == 15
    assert report["passed_case_count"] == 15
    assert report["live_case_count"] == 12
    assert report["mode_counts"] == {"live_ouroboros": 12, "validation_only": 3}
    assert report["case_execution_order"] == [item.case_id for item in live_evaluation.CASE_PLAN]
    assert len(usage_rows) == 12
    validate_live_report(report)

    source = live / evaluation_id
    assert (source / "FROZEN.json").is_file()
    assert not (source / "FAILED.json").exists()
    assert zipfile.is_zipfile(source / "demo-case" / "campaign-export.zip")
    validate_checksums(source)
    marker = json.loads((runs / f"{evaluation_id}.json").read_text())
    assert marker["status"] == "completed"
    assert marker["usage_complete"] is True
    assert marker["completed_case_count"] == 15


@pytest.mark.integration
def test_case_boundary_stop_preserves_failure_and_cleans_active_rule(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_id = "full-live-budget-stop-01"
    raw = _raw_basket(tmp_path, monkeypatch, evaluation_id)
    b02_metrics = raw["B02"]["metrics"]
    b02_usage = b02_metrics["usage_by_category"]["main_generation"]
    b02_usage["prompt_tokens"] = 10
    b02_usage["completion_tokens"] = 0
    b02_metrics["prompt_tokens"] = 10
    b02_metrics["completion_tokens"] = 0
    _, live = _configure_runtime_paths(tmp_path, monkeypatch)
    cleanup_calls: list[tuple[str, str]] = []

    def cleanup(rule_version_id: str, active_rules_version: str) -> dict[str, Any]:
        cleanup_calls.append((rule_version_id, active_rules_version))
        return {"ok": True, "status": "ROLLED_BACK", "provider_calls": 0}

    report = live_evaluation.run_live_evaluation(
        _context(evaluation_id, max_tokens=1_000),
        environment={},
        executor=_executor(raw),
        export_copier=_copy_export,
        rule_cleanup=cleanup,
        usage_appender=lambda _rows: None,
    )

    assert report["status"] == "FAIL"
    assert report["business_case_count"] == 2
    assert report["release_targets_passed"] is False
    assert report["usage"]["usage_complete"] is True
    assert any(item == "B01_LiveEvaluationError" for item in report["release_blockers"])
    assert len(cleanup_calls) == 1
    source = live / evaluation_id
    assert (source / "FAILED.json").is_file()
    assert not (source / "FROZEN.json").exists()
    assert (source / "rule-cleanup.json").is_file()


@pytest.mark.integration
def test_additional_authority_quarantines_orphan_and_continues_remaining_cases(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_id = "full-live-additional-orphan-01"
    raw = _raw_basket(tmp_path, monkeypatch, evaluation_id)
    for case in raw.values():
        for usage in (case.get("metrics") or {}).get("usage_by_category", {}).values():
            usage["providers"] = ["openrouter"]
            usage["models"] = ["z-ai/glm-5.2"]
    b01 = raw["B01"]
    b01["ok"] = False
    b01["functional_quality_passed"] = False
    b01["metrics"]["usage_complete"] = False
    orphan_operation = b01["operations"][-1]
    orphan_operation["ok"] = False
    orphan_operation["functional_quality_passed"] = False
    orphan_operation["checks"]["usage_complete"] = False
    orphan_operation["error_type"] = "TimeoutError"
    orphan_operation["provider_accounting"] = {
        "schema_version": 1,
        "orphan_requests": [
            {
                "provider_call_id": "cf_provider_orphan",
                "category": "main_generation",
                "generation_id": "gen-orphan-12345678",
                "status_code": 200,
                "estimated_prompt_tokens": 500,
                "configured_max_output_tokens": 100,
                "prompt_estimation_method": "utf8_request_bytes_upper_bound_v1",
            }
        ],
        "pre_generation_anomalies": [],
    }
    runs, live = _configure_runtime_paths(tmp_path, monkeypatch)
    context = _additional_context(tmp_path, evaluation_id)
    usage_rows: list[dict[str, Any]] = []

    report = live_evaluation.run_live_evaluation(
        context,
        environment={},
        executor=_executor(raw),
        export_copier=_copy_export,
        rule_cleanup=lambda _rule, _version: {"ok": True},
        usage_appender=usage_rows.extend,
        metadata_poller=lambda requests, max_seconds: {
            "schema_version": 1,
            "status": "incomplete",
            "poll_max_seconds": max_seconds,
            "elapsed_seconds": max_seconds,
            "requested_generation_ids": [requests[0]["generation_id"]],
            "resolved_generation_ids": [],
            "unresolved_generation_ids": [requests[0]["generation_id"]],
            "attempts": [],
            "results": [],
        },
    )

    assert report["status"] == "FAIL"
    assert report["business_case_count"] == 15
    assert report["quarantined_cases"] == ["B01"]
    assert report["usage"]["usage_complete"] is False
    assert report["usage"]["bounded_estimated_tokens"] == 600
    assert report["usage"]["estimate_is_provider_usage"] is False
    assert len(usage_rows) == 12
    marker = json.loads((runs / f"{evaluation_id}.json").read_text())
    assert marker["status"] == "failed"
    assert marker["accounting_disposition"] == "orphan_request_estimate"
    assert marker["bounded_request_estimates"][0]["estimated_tokens"] == 600
    assert marker["evidence_eligible"] is False
    source = live / evaluation_id
    assert (source / "cases" / "B15" / "outcome.json").is_file()
    assert (source / "generation-metadata-poll.json").is_file()
    assert (source / "accounting.json").is_file()


@pytest.mark.integration
def test_additional_authority_treats_zero_request_deadline_as_anomaly_and_continues(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_id = "full-live-additional-zero-request-01"
    raw = _raw_basket(tmp_path, monkeypatch, evaluation_id)
    for case in raw.values():
        for usage in (case.get("metrics") or {}).get("usage_by_category", {}).values():
            usage["providers"] = ["openrouter"]
            usage["models"] = ["z-ai/glm-5.2"]

    b04 = raw["B04"]
    operation = b04["operations"][0]
    b04["ok"] = False
    b04["functional_quality_passed"] = False
    b04["mode"] = "deterministic_template"
    operation["ok"] = False
    operation["functional_quality_passed"] = False
    operation["task"].update(
        {
            "status": "failed",
            "reason_code": "deadline",
            "total_rounds": 0,
        }
    )
    operation["run"].update(
        {
            "status": "COMPLETED_FALLBACK",
            "mode": "deterministic_template",
            "reason_code": "LIVE_TASK_FAILED",
        }
    )
    for row in operation["provider_call_ledger"].values():
        for field in (
            "call_count",
            "provider_request_count",
            "provider_request_completed_count",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "cache_write_tokens",
        ):
            row[field] = 0
        row["cost_usd"] = 0.0
        for field in (
            "provider_request_ids",
            "provider_request_completed_ids",
            "generation_ids",
            "terminal_generation_ids",
            "models",
            "providers",
            "timestamps",
        ):
            row[field] = []
    for row in operation["usage_by_category"].values():
        for field in (
            "calls",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "cache_write_tokens",
        ):
            row[field] = 0
        row["cost_usd"] = 0.0
        row["models"] = []
        row["providers"] = []
    operation["checks"].update(
        {
            "run_completed_live": False,
            "usage_complete": False,
            "provider_route_unchanged": False,
            "model_route_unchanged": False,
        }
    )
    operation["provider_accounting"] = {
        "schema_version": 1,
        "orphan_requests": [],
        "pre_generation_anomalies": [
            {
                "provider_call_id": "",
                "category": "main_generation",
                "status_code": 0,
                "error_type": "deadline",
                "generation_id_present": False,
                "reserved_tokens": 0,
                "reserved_cost_usd": 0.0,
                "source": "task_terminal_without_provider_request",
                "task_id": operation["task"]["task_id"],
            }
        ],
    }
    for field in (
        "provider_calls",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "cache_write_tokens",
    ):
        b04["metrics"][field] = 0
    b04["metrics"]["cost_usd"] = 0.0
    b04["metrics"]["usage_complete"] = False
    b04["metrics"]["usage_by_category"] = copy.deepcopy(operation["usage_by_category"])

    runs, live = _configure_runtime_paths(tmp_path, monkeypatch)
    context = _additional_context(tmp_path, evaluation_id)

    report = live_evaluation.run_live_evaluation(
        context,
        environment={},
        executor=_executor(raw),
        export_copier=_copy_export,
        rule_cleanup=lambda _rule, _version: {"ok": True},
        usage_appender=lambda _rows: None,
    )

    assert report["status"] == "FAIL"
    assert report["business_case_count"] == 15
    assert report["quarantined_cases"] == ["B04"]
    assert report["usage"]["usage_complete"] is True
    assert report["usage"]["bounded_estimated_tokens"] == 0
    assert report["accounting"]["accounting_disposition"] == "pre_generation_anomaly"
    assert (live / evaluation_id / "cases" / "B15" / "outcome.json").is_file()
    marker = json.loads((runs / f"{evaluation_id}.json").read_text())
    assert marker["bounded_estimated_tokens"] == 0
    assert marker["bounded_estimated_cost_usd"] == 0.0
    assert marker["pre_generation_anomalies"][0]["reserved_tokens"] == 0


@pytest.mark.integration
def test_glm_functional_pass_is_immutable_but_not_canonical_evidence(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_id = "full-live-glm-latency-gap-01"
    raw = _raw_basket(tmp_path, monkeypatch, evaluation_id)
    for case in raw.values():
        for operation in case.get("operations") or []:
            operation["latency_ms"]["user_visible_terminal"] = 31_000
        for usage in (case.get("metrics") or {}).get("usage_by_category", {}).values():
            usage["providers"] = ["openrouter"]
            usage["models"] = ["openrouter/z-ai/glm-5.2"]
    runs, live = _configure_runtime_paths(tmp_path, monkeypatch)
    canonical = _context(evaluation_id)
    glm_context = dataclasses.replace(
        canonical,
        branch="codex/p0-glm-basket",
        request=dataclasses.replace(
            canonical.request,
            provider="openrouter",
            model="z-ai/glm-5.2",
            openrouter_enabled=True,
            profile_name="openrouter-glm-5.2-functional",
        ),
    )

    report = live_evaluation.run_live_evaluation(
        glm_context,
        environment={},
        executor=_executor(raw),
        export_copier=_copy_export,
        rule_cleanup=lambda _rule, _version: {"ok": True},
        usage_appender=lambda _rows: None,
    )

    assert report["status"] == "FUNCTIONAL_PASS_WITH_LATENCY_GAP"
    assert report["functional_quality_passed"] is True
    assert report["canonical_latency_passed"] is False
    assert report["release_targets_passed"] is False
    assert report["latency"]["p95_ms"] == 31_000
    source = live / evaluation_id
    assert (source / "FUNCTIONAL_IMMUTABLE.json").is_file()
    assert not (source / "FROZEN.json").exists()
    with pytest.raises(EvidenceError, match=r"frozen|PASS|release"):
        validate_live_report(report)
    marker = json.loads((runs / f"{evaluation_id}.json").read_text())
    assert marker["status"] == "completed"
