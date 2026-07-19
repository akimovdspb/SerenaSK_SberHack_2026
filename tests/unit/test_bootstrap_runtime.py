from __future__ import annotations

import json
import pathlib

import pytest

from scripts import bootstrap_runtime
from scripts.bootstrap_runtime import BootstrapError
from scripts.budget_control import RunRequest, read_usage_ledger


def _glm_review_request() -> RunRequest:
    return RunRequest(
        run_id="review-route-check-01",
        provider="openrouter",
        model="z-ai/glm-5.2",
        max_tokens=200_000,
        max_cost_usd=0.5,
        projected_tokens=200_000,
        projected_cost_usd=0.5,
        concurrency=1,
        openrouter_enabled=True,
        profile_name="openrouter-glm-5.2-functional",
    )


def test_runtime_review_route_must_match_requested_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap_runtime,
        "_runtime_review_route",
        lambda: ("openai", "gpt-5.4-mini"),
    )

    with pytest.raises(BootstrapError, match="route differs"):
        bootstrap_runtime._assert_runtime_review_route(_glm_review_request())


def test_append_review_usage_records_observed_route_exactly(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr(bootstrap_runtime, "DEFAULT_USAGE_LEDGER", ledger)
    review = {
        "usage": [
            {
                "model": "openai/gpt-5.4-mini",
                "prompt_tokens": 125_762,
                "completion_tokens": 773,
                "cost_usd": 0.0978,
            }
        ]
    }

    totals = bootstrap_runtime._append_review_usage(
        "review-route-drift-01",
        review,
        observed_provider="openai",
    )

    assert totals == (126_535, 0.0978, ("openai",), ("gpt-5.4-mini",))
    records = read_usage_ledger(ledger)
    assert len(records) == 1
    assert records[0].provider == "openai"
    assert records[0].model == "gpt-5.4-mini"
    assert records[0].total_tokens == 126_535


def test_recover_failed_review_accounting_preserves_failure_and_exact_usage(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = tmp_path / "runs"
    accounting = tmp_path / "reviews"
    ledger = tmp_path / "usage.jsonl"
    runs.mkdir()
    run_id = "review-route-drift-02"
    marker_path = runs / f"{run_id}.json"
    original_finished_at = "2026-07-15T13:27:58.560733+00:00"
    marker_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "kind": "skill_review",
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "provider_profile": "openrouter-glm-5.2-functional",
                "max_tokens": 200_000,
                "max_cost_usd": 0.5,
                "projected_tokens": 200_000,
                "projected_cost_usd": 0.5,
                "status": "failed",
                "usage_complete": False,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "started_at": "2026-07-15T13:27:41.382760+00:00",
                "finished_at": original_finished_at,
            }
        ),
        encoding="utf-8",
    )
    persisted = {
        "status": "clean",
        "content_hash": "d" * 64,
        "reviewed_at": "2026-07-15T13:27:57.993787+00:00",
        "usage": [
            {
                "model": "openai/gpt-5.4-mini",
                "prompt_tokens": 125_762,
                "completion_tokens": 773,
                "cost_usd": 0.0978,
            }
        ],
    }
    monkeypatch.setattr(bootstrap_runtime, "RUN_STATE_DIR", runs)
    monkeypatch.setattr(bootstrap_runtime, "REVIEW_ACCOUNTING_ROOT", accounting)
    monkeypatch.setattr(bootstrap_runtime, "DEFAULT_USAGE_LEDGER", ledger)
    monkeypatch.setattr(
        bootstrap_runtime,
        "_persisted_review_snapshot",
        lambda: (persisted, "openai"),
    )

    recovered = bootstrap_runtime.recover_failed_review_accounting(run_id)

    assert recovered["tokens"] == 126_535
    assert recovered["cost_usd"] == pytest.approx(0.0978)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["status"] == "failed"
    assert marker["usage_complete"] is True
    assert marker["evidence_eligible"] is False
    assert marker["finished_at"] == original_finished_at
    assert marker["observed_providers"] == ["openai"]
    assert marker["observed_models"] == ["gpt-5.4-mini"]
    assert marker["provider_drift_detected"] is True
    assert marker["model_drift_detected"] is True
    assert marker["failure_classes"] == [
        "provider.route_drift",
        "provider.model_drift",
    ]
    artifact = accounting / f"{run_id}.json"
    assert marker["accounting_artifact_sha256"] == recovered["accounting_artifact_sha256"]
    assert artifact.is_file()
    records = read_usage_ledger(ledger)
    assert [(row.provider, row.model, row.total_tokens) for row in records] == [
        ("openai", "gpt-5.4-mini", 126_535)
    ]

    with pytest.raises(BootstrapError, match="eligible"):
        bootstrap_runtime.recover_failed_review_accounting(run_id)


def test_recovery_rejects_unbound_persisted_review_before_ledger_mutation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    ledger = tmp_path / "usage.jsonl"
    run_id = "review-route-drift-03"
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "kind": "skill_review",
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "provider_profile": "openrouter-glm-5.2-functional",
                "max_tokens": 200_000,
                "max_cost_usd": 0.5,
                "projected_tokens": 200_000,
                "projected_cost_usd": 0.5,
                "status": "failed",
                "usage_complete": False,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "started_at": "2026-07-15T13:27:41+00:00",
                "finished_at": "2026-07-15T13:27:58+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap_runtime, "RUN_STATE_DIR", runs)
    monkeypatch.setattr(bootstrap_runtime, "REVIEW_ACCOUNTING_ROOT", tmp_path / "reviews")
    monkeypatch.setattr(bootstrap_runtime, "DEFAULT_USAGE_LEDGER", ledger)
    monkeypatch.setattr(
        bootstrap_runtime,
        "_persisted_review_snapshot",
        lambda: (
            {
                "status": "clean",
                "content_hash": "e" * 64,
                "reviewed_at": "2026-07-15T13:28:01+00:00",
                "usage": [
                    {
                        "model": "openai/gpt-5.4-mini",
                        "prompt_tokens": 10,
                        "completion_tokens": 1,
                        "cost_usd": 0.01,
                    }
                ],
            },
            "openai",
        ),
    )

    with pytest.raises(BootstrapError, match="run window"):
        bootstrap_runtime.recover_failed_review_accounting(run_id)
    assert not ledger.exists()
