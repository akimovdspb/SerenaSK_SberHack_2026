from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess

import pytest

from scripts import gate2_live
from scripts.budget_control import NightBudget, RunRequest


def _authorized_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "ALLOW_GATE2_LIVE": "true",
        "EVAL_PROVIDER_PROFILE": "openai-gpt-5.4-mini",
        "EVALUATION_ID": "gate2-b04-pilot-test",
        "EVAL_MAX_TOKENS": "120000",
        "EVAL_MAX_COST_USD": "0.10",
        "EVAL_PROJECTED_TOKENS": "100000",
        "EVAL_PROJECTED_COST_USD": "0.05",
        "EVAL_CONCURRENCY": "1",
        "PILOT_CASE_ID": "B07",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _additional_night(tmp_path: pathlib.Path) -> NightBudget:
    return NightBudget(
        night_id="p0-glm-continuation-20260715-01",
        authority_path=tmp_path / "HANDOFF_VPS_P0_GLM_BASKET.md",
        authority_sha256="e" * 64,
        max_tokens=100_000_000,
        max_cost_usd=200.0,
        phase="pilots",
        phase_max_tokens=100_000_000,
        phase_max_cost_usd=200.0,
        incomplete_usage_policy=("owner_authorized_confirmed_plus_bounded_per_call_estimates"),
        additional_authority=True,
        prompt_price_usd_per_million=0.8862,
        completion_price_usd_per_million=2.785,
        estimate_safety_multiplier=2.0,
        metadata_poll_max_seconds=600,
        max_directed_attempts_per_failure_class=6,
    )


def test_gate2_live_requires_explicit_opt_in_and_positive_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLOW_GATE2_LIVE", raising=False)

    with pytest.raises(gate2_live.Gate2LiveError, match="ALLOW_GATE2_LIVE"):
        gate2_live.requested_run()

    _authorized_environment(monkeypatch)
    request = gate2_live.requested_run()

    assert request.run_id == "gate2-b04-pilot-test"
    assert request.provider == "openai"
    assert request.model == "gpt-5.4-mini"
    assert request.concurrency == 1
    assert request.max_tokens == 120_000
    assert gate2_live.requested_case_id() == "B07"

    monkeypatch.setenv("PILOT_CASE_ID", "B01")
    with pytest.raises(gate2_live.Gate2LiveError, match="B04, B07 or B08"):
        gate2_live.requested_case_id()

    monkeypatch.setenv("EVAL_PROVIDER_PROFILE", "openrouter-glm-5.2-functional")
    monkeypatch.setenv("PILOT_CASE_ID", "B15")
    glm_request = gate2_live.requested_run()
    assert glm_request.provider == "openrouter"
    assert glm_request.model == "z-ai/glm-5.2"
    assert gate2_live.requested_case_id() == "B15"


def test_gate2_live_reservation_is_unique_empty_and_marks_account_unknown(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_state = tmp_path / "runs"
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(gate2_live, "RUN_STATE_DIR", run_state)
    monkeypatch.setattr(gate2_live, "EVIDENCE_ROOT", evidence_root)
    request = RunRequest(
        run_id="gate2-b04-pilot-test",
        provider="openai",
        model="gpt-5.4-mini",
        max_tokens=120_000,
        max_cost_usd=0.1,
        projected_tokens=100_000,
        projected_cost_usd=0.05,
        concurrency=1,
    )

    marker, evidence_dir = gate2_live.reserve_run(
        request,
        f"sha256:{'a' * 64}",
        "B07",
        "c" * 40,
    )
    payload = json.loads(marker.read_text(encoding="utf-8"))

    assert evidence_dir.is_dir()
    assert not list(evidence_dir.iterdir())
    assert payload["kind"] == "gate2_live_campaign"
    assert payload["case_id"] == "B07"
    assert payload["app_commit"] == "c" * 40
    assert payload["account_remaining"] == "unknown"
    assert payload["status"] == "running"
    with pytest.raises(gate2_live.Gate2LiveError, match="already used"):
        gate2_live.reserve_run(request, f"sha256:{'a' * 64}", "B07", "c" * 40)


def test_failed_run_recovery_records_observed_usage_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_id = "gate2-b04-failed-test"
    internal_run_id = "run_failed_test"
    run_state = tmp_path / "runs"
    evidence_root = tmp_path / "evidence"
    run_state.mkdir()
    evidence_dir = evidence_root / evaluation_id
    evidence_dir.mkdir(parents=True)
    marker = run_state / f"{evaluation_id}.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": evaluation_id,
                "kind": "gate2_live_campaign",
                "case_id": "B04",
                "status": "failed",
            }
        ),
        encoding="utf-8",
    )
    ledger = {
        "main_generation": {
            "call_count": 2,
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "cost_usd": 0.01,
            "models": ["openai/gpt-5.4-mini"],
            "providers": ["openai"],
            "timestamps": ["now"],
        },
        "safety": {
            "call_count": 1,
            "prompt_tokens": 200,
            "completion_tokens": 20,
            "cost_usd": 0.002,
            "models": ["openai/gpt-5.4-mini"],
            "providers": ["openai"],
            "timestamps": ["now"],
        },
    }
    report = {
        "schema_version": 1,
        "evaluation_id": evaluation_id,
        "ok": False,
        "run": {"run_id": internal_run_id},
        "provider_call_ledger": ledger,
        "checks": {"usage_complete": False},
    }
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(gate2_live, "RUN_STATE_DIR", run_state)
    monkeypatch.setattr(gate2_live, "EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(gate2_live, "read_usage_ledger", lambda _: [])
    monkeypatch.setattr(
        gate2_live,
        "execute_recovery",
        lambda *_: (subprocess.CompletedProcess([], 1, "", ""), report),
    )
    monkeypatch.setattr(gate2_live, "append_usage", recorded.extend)

    result = gate2_live.recover_failed_run(evaluation_id, internal_run_id)
    state = json.loads(marker.read_text(encoding="utf-8"))

    assert result["total_tokens"] == 1320
    assert result["total_cost_usd"] == 0.012
    assert len(recorded) == 2
    assert state["status"] == "failed"
    assert state["usage_recovered"] is True
    assert (evidence_dir / "postmortem.json").is_file()
    with pytest.raises(gate2_live.Gate2LiveError, match="postmortem already exists"):
        gate2_live.recover_failed_run(evaluation_id, internal_run_id)


def test_gate2_retry_reservation_requires_a_preserved_failed_attempt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_state = tmp_path / "runs"
    evidence_root = tmp_path / "evidence"
    run_state.mkdir()
    previous_id = "gate2-b04-attempt-01"
    (run_state / f"{previous_id}.json").write_text(
        json.dumps(
            {
                "kind": "gate2_live_campaign",
                "run_id": previous_id,
                "case_id": "B04",
                "status": "failed",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate2_live, "RUN_STATE_DIR", run_state)
    monkeypatch.setattr(gate2_live, "EVIDENCE_ROOT", evidence_root)
    monkeypatch.setenv("PREVIOUS_EVALUATION_ID", previous_id)
    monkeypatch.setenv(
        "EVALUATION_RETRY_REASON", "Schema contract clarified after preserved failure."
    )
    request = RunRequest(
        run_id="gate2-b04-attempt-02",
        provider="openai",
        model="gpt-5.4-mini",
        max_tokens=120_000,
        max_cost_usd=0.1,
        projected_tokens=100_000,
        projected_cost_usd=0.05,
        concurrency=1,
    )

    marker, _ = gate2_live.reserve_run(
        request,
        f"sha256:{'a' * 64}",
        "B04",
        "c" * 40,
    )
    payload = json.loads(marker.read_text(encoding="utf-8"))

    assert payload["retry_of"] == previous_id
    assert "Schema contract clarified" in payload["retry_reason"]


def test_gate2_retry_linkage_is_checked_before_reservation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_state = tmp_path / "runs"
    run_state.mkdir()
    monkeypatch.setattr(gate2_live, "RUN_STATE_DIR", run_state)
    monkeypatch.setenv("PREVIOUS_EVALUATION_ID", "gate2-b04-missing-attempt")
    monkeypatch.setenv("EVALUATION_RETRY_REASON", "A deterministic defect was fixed.")

    with pytest.raises(gate2_live.Gate2LiveError, match="previous Gate 2 run is unavailable"):
        gate2_live.validate_retry_linkage("B04")

    monkeypatch.delenv("EVALUATION_RETRY_REASON")
    with pytest.raises(gate2_live.Gate2LiveError, match="requires previous run id and reason"):
        gate2_live.validate_retry_linkage("B04")


def test_incomplete_pilot_orphan_uses_only_bounded_request_estimate(
    tmp_path: pathlib.Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    generation_id = "gen-orphan-12345678"
    report = {
        "ok": False,
        "run": {"reason_code": "TOOL_SEQUENCE_INVALID"},
        "operations": [
            {
                "provider_accounting": {
                    "orphan_requests": [
                        {
                            "provider_call_id": "cf_provider_orphan",
                            "category": "post_task_summary",
                            "generation_id": generation_id,
                            "status_code": 200,
                            "estimated_prompt_tokens": 500,
                            "configured_max_output_tokens": 100,
                            "prompt_estimation_method": "utf8_request_bytes_upper_bound_v1",
                        }
                    ],
                    "pre_generation_anomalies": [],
                }
            }
        ],
    }

    result = gate2_live._account_incomplete_usage(
        run_id="gate2-b15-orphan-test",
        case_id="B15",
        report=report,
        evidence_dir=evidence_dir,
        night=_additional_night(tmp_path),
        expected_model="z-ai/glm-5.2",
        known_tokens=1_000,
        known_cost_usd=0.01,
        metadata_poller=lambda _requests, max_seconds: {
            "schema_version": 1,
            "status": "incomplete",
            "poll_max_seconds": max_seconds,
            "elapsed_seconds": max_seconds,
            "requested_generation_ids": [generation_id],
            "resolved_generation_ids": [],
            "unresolved_generation_ids": [generation_id],
            "attempts": [],
            "results": [],
        },
        usage_appender=lambda _rows: pytest.fail("estimate must not mutate provider ledger"),
    )

    assert result["usage_complete"] is False
    assert result["estimated_tokens"] == 600
    assert result["marker_fields"]["accounting_disposition"] == "orphan_request_estimate"
    assert result["marker_fields"]["provider_usage_unknown"] is True
    assert result["marker_fields"]["failure_classes"] == [
        "case.b15.tool_sequence_invalid",
        "provider.orphan_generation",
    ]
    accounting = json.loads((evidence_dir / "accounting.json").read_text())
    assert accounting["provider_ledger_mutated_by_estimate"] is False
    assert accounting["bounded_request_estimates"][0]["estimated_tokens"] == 600


def test_failed_pilot_reconciliation_recovers_exact_orphan_usage_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "gate2-b15-reconcile-test"
    run_state = tmp_path / "runs"
    evidence_root = tmp_path / "evidence"
    evidence_dir = evidence_root / run_id
    run_state.mkdir()
    evidence_dir.mkdir(parents=True)
    report = {
        "evaluation_id": run_id,
        "case_id": "B15",
        "ok": False,
        "run": {"reason_code": "TOOL_SEQUENCE_INVALID"},
        "operations": [
            {
                "provider_accounting": {
                    "orphan_requests": [
                        {
                            "provider_call_id": "cf_provider_orphan",
                            "category": "post_task_summary",
                            "generation_id": "gen-recovered-12345678",
                            "status_code": 200,
                            "estimated_prompt_tokens": 500,
                            "configured_max_output_tokens": 100,
                            "prompt_estimation_method": "utf8_request_bytes_upper_bound_v1",
                        }
                    ],
                    "pre_generation_anomalies": [],
                }
            }
        ],
    }
    report_path = evidence_dir / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_before = report_path.read_bytes()
    night = _additional_night(tmp_path)
    marker = run_state / f"{run_id}.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "kind": "gate2_live_campaign",
                "case_id": "B15",
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "status": "failed",
                "usage_complete": False,
                "total_tokens": 110,
                "total_cost_usd": 0.01,
                "report_sha256": hashlib.sha256(report_before).hexdigest(),
                "night_id": night.night_id,
                "night_authority_sha256": night.authority_sha256,
            }
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "ts": "2026-07-15T00:00:00+00:00",
                "run_id": run_id,
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "category": "main_generation",
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "cost_usd": 0.01,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    appended: list[dict[str, object]] = []
    monkeypatch.setattr(gate2_live, "RUN_STATE_DIR", run_state)
    monkeypatch.setattr(gate2_live, "EVIDENCE_ROOT", evidence_root)

    result = gate2_live.reconcile_failed_accounting(
        run_id,
        night,
        ledger_path=ledger,
        metadata_poller=lambda _requests, max_seconds: {
            "schema_version": 1,
            "status": "complete",
            "poll_max_seconds": max_seconds,
            "elapsed_seconds": 1.5,
            "requested_generation_ids": ["gen-recovered-12345678"],
            "resolved_generation_ids": ["gen-recovered-12345678"],
            "unresolved_generation_ids": [],
            "attempts": [],
            "results": [
                {
                    "generation_id": "gen-recovered-12345678",
                    "found": True,
                    "status_code": 200,
                    "data": {
                        "model": "z-ai/glm-5.2-20260616",
                        "native_tokens_prompt": 20,
                        "native_tokens_completion": 5,
                        "total_cost": 0.002,
                    },
                }
            ],
        },
        usage_appender=appended.extend,
    )

    state = json.loads(marker.read_text())
    assert result["usage_complete"] is True
    assert result["total_tokens"] == 135
    assert result["total_cost_usd"] == 0.012
    assert result["bounded_estimated_tokens"] == 0
    assert len(appended) == 1
    assert state["status"] == "failed"
    assert state["usage_complete"] is True
    assert state["evidence_eligible"] is False
    assert state["accounting_disposition"] == "metadata_recovered"
    assert state["failure_classes"] == [
        "case.b15.tool_sequence_invalid",
        "provider.orphan_generation",
    ]
    assert report_path.read_bytes() == report_before
    with pytest.raises(gate2_live.Gate2LiveError, match="already exists"):
        gate2_live.reconcile_failed_accounting(run_id, night, ledger_path=ledger)
