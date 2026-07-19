from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib

import pytest
import yaml

from scripts.budget_control import (
    BudgetPolicyError,
    RunRequest,
    UsageRecord,
    assert_provider_unchanged,
    bounded_request_estimate,
    case_boundary_allows_next,
    load_operator_profile,
    night_marker_fields,
    read_usage_ledger,
    requested_night_budget,
    validate_night_headroom,
    validate_paid_run_budget,
    validate_run_request,
)


def _operator_file(tmp_path: pathlib.Path, *, model: str = "gpt-5.4-mini") -> pathlib.Path:
    path = tmp_path / "operator.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "models": {
                    model: {
                        "operator_demo_reserve_tokens": 100,
                        "project_planning_cap_tokens_per_utc_day": 2_000,
                        "reported_account_allowance_tokens_per_day": 10_000,
                        "warning_at_project_tokens": 1_500,
                    }
                },
                "rules": {
                    "account_wide_remaining_quota_known": False,
                    "project_counters_are_account_ground_truth": False,
                    "gpt_5_4_comparator_enabled_by_default": False,
                    "openrouter_enabled_by_default": False,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _request(**overrides: object) -> RunRequest:
    values: dict[str, object] = {
        "run_id": "run-1",
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "max_tokens": 1_000,
        "max_cost_usd": 1.0,
        "projected_tokens": 500,
        "projected_cost_usd": 0.25,
    }
    values.update(overrides)
    return RunRequest(**values)  # type: ignore[arg-type]


def test_missing_operator_file_and_nonpositive_caps_fail_closed(
    tmp_path: pathlib.Path,
) -> None:
    with pytest.raises(BudgetPolicyError, match="unavailable"):
        load_operator_profile(tmp_path / "missing.yaml")

    profile = load_operator_profile(_operator_file(tmp_path))
    with pytest.raises(BudgetPolicyError, match="token cap"):
        validate_run_request(
            _request(max_tokens=0),
            profile=profile,
            observed_project_tokens=0,
        )


def test_run_id_rejects_path_traversal(tmp_path: pathlib.Path) -> None:
    profile = load_operator_profile(_operator_file(tmp_path))
    with pytest.raises(BudgetPolicyError, match="safe filename"):
        validate_run_request(
            _request(run_id="../reused"),
            profile=profile,
            observed_project_tokens=0,
        )


def test_projection_and_project_headroom_are_enforced(tmp_path: pathlib.Path) -> None:
    profile = load_operator_profile(_operator_file(tmp_path))
    with pytest.raises(BudgetPolicyError, match="projected usage"):
        validate_run_request(
            _request(projected_tokens=1_001),
            profile=profile,
            observed_project_tokens=0,
        )
    with pytest.raises(BudgetPolicyError, match="headroom"):
        validate_run_request(
            _request(max_tokens=500, projected_tokens=400),
            profile=profile,
            observed_project_tokens=1_600,
        )


def test_case_boundary_stops_on_missing_usage_or_insufficient_headroom() -> None:
    request = _request(max_tokens=1_000, max_cost_usd=1.0)
    usage = [
        UsageRecord(
            ts=dt.datetime.now(dt.UTC),
            run_id=request.run_id,
            provider="openai",
            model="gpt-5.4-mini",
            category="main_generation",
            prompt_tokens=700,
            completion_tokens=200,
            cost_usd=0.8,
        )
    ]

    assert not case_boundary_allows_next(
        request,
        recorded_run_usage=usage,
        next_case_projected_tokens=50,
        next_case_projected_cost_usd=0.05,
        usage_complete=False,
    )
    assert not case_boundary_allows_next(
        request,
        recorded_run_usage=usage,
        next_case_projected_tokens=200,
        next_case_projected_cost_usd=0.25,
        usage_complete=True,
    )


def test_malformed_usage_is_unknown_and_stops_expansion(tmp_path: pathlib.Path) -> None:
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text('{"ts":"broken"}\n', encoding="utf-8")

    with pytest.raises(BudgetPolicyError, match="identity fields"):
        read_usage_ledger(ledger)


def test_comparator_requires_explicit_opt_in_and_matching_id(tmp_path: pathlib.Path) -> None:
    profile = load_operator_profile(_operator_file(tmp_path, model="gpt-5.4"), model="gpt-5.4")
    with pytest.raises(BudgetPolicyError, match="comparator"):
        validate_run_request(
            _request(model="gpt-5.4"),
            profile=profile,
            observed_project_tokens=0,
        )

    validate_run_request(
        _request(
            model="gpt-5.4",
            allow_gpt54_comparator=True,
            comparator_run_id="run-1",
        ),
        profile=profile,
        observed_project_tokens=0,
    )


def test_provider_switching_and_openrouter_fallback_are_forbidden(
    tmp_path: pathlib.Path,
) -> None:
    profile = load_operator_profile(_operator_file(tmp_path))
    with pytest.raises(BudgetPolicyError, match="selected profile"):
        validate_run_request(
            _request(provider="openrouter", openrouter_enabled=True),
            profile=profile,
            observed_project_tokens=0,
        )
    with pytest.raises(BudgetPolicyError, match="provider switching"):
        assert_provider_unchanged("openai", "openrouter")


def _night_handoff(
    tmp_path: pathlib.Path,
    *,
    resume: bool = False,
    continuation: bool = False,
    second_continuation: bool = False,
    third_continuation: bool = False,
    parent_authority_sha256: str = "a" * 64,
    continuation_parent_authority_sha256: str = "b" * 64,
    second_continuation_parent_authority_sha256: str = "c" * 64,
    third_continuation_parent_authority_sha256: str = "d" * 64,
) -> pathlib.Path:
    path = tmp_path / "HANDOFF_VPS_P0_GLM_BASKET.md"
    text = """
- aggregate maximum cost: $50.00;
- aggregate maximum tokens: 15 000 000;
- smoke и до двух повторов вместе: максимум $3 и 1 000 000 tokens;
- все pilots и повторы вместе: максимум $12 и 4 000 000 tokens;
""".strip()
    if resume:
        text += f"""

- `resume_session_id=p0-glm-resume-20260715-01`
- `resume_parent_night_id=glm-night-20260714`
- `resume_parent_authority_sha256={parent_authority_sha256}`
- `resume_model_drift_policy=failed_accounted_nonblocking`
"""
    if continuation:
        continuation_policy = (
            "owner_authorized_incomplete_usage_quarantined_by_full_cap_reservation"
        )
        text += f"""

- `continuation_session_id=p0-glm-resume-20260715-02`
- `continuation_parent_night_id=p0-glm-resume-20260715-01`
- `continuation_parent_authority_sha256={continuation_parent_authority_sha256}`
- `continuation_ancestor_night_id=glm-night-20260714`
- `continuation_ancestor_authority_sha256={parent_authority_sha256}`
- `continuation_incomplete_usage_policy={continuation_policy}`
- `continuation_quarantined_run_id=p0-glm-resume-smoke-b01-20260715-04`
- `continuation_quarantined_run_max_tokens=190000`
- `continuation_quarantined_run_max_cost_usd=0.80`
"""
    if second_continuation:
        continuation_policy = (
            "owner_authorized_incomplete_usage_quarantined_by_full_cap_reservation"
        )
        text += f"""

- `continuation_v2_session_id=p0-glm-resume-20260715-03`
- `continuation_v2_parent_night_id=p0-glm-resume-20260715-02`
- `continuation_v2_parent_authority_sha256={second_continuation_parent_authority_sha256}`
- `continuation_v2_ancestor_night_id=p0-glm-resume-20260715-01`
- `continuation_v2_ancestor_authority_sha256={continuation_parent_authority_sha256}`
- `continuation_v2_root_ancestor_night_id=glm-night-20260714`
- `continuation_v2_root_ancestor_authority_sha256={parent_authority_sha256}`
- `continuation_v2_incomplete_usage_policy={continuation_policy}`
- `continuation_v2_quarantined_run_id=p0-glm-resume-smoke-b01-20260715-05`
- `continuation_v2_quarantined_run_max_tokens=250000`
- `continuation_v2_quarantined_run_max_cost_usd=0.80`
"""
    if third_continuation:
        continuation_policy = (
            "owner_authorized_incomplete_usage_quarantined_by_full_cap_reservation"
        )
        text += f"""

- `continuation_v3_session_id=p0-glm-resume-20260715-04`
- `continuation_v3_parent_night_id=p0-glm-resume-20260715-03`
- `continuation_v3_parent_authority_sha256={third_continuation_parent_authority_sha256}`
- `continuation_v3_ancestor_night_id=p0-glm-resume-20260715-02`
- `continuation_v3_ancestor_authority_sha256={second_continuation_parent_authority_sha256}`
- `continuation_v3_second_ancestor_night_id=p0-glm-resume-20260715-01`
- `continuation_v3_second_ancestor_authority_sha256={continuation_parent_authority_sha256}`
- `continuation_v3_root_ancestor_night_id=glm-night-20260714`
- `continuation_v3_root_ancestor_authority_sha256={parent_authority_sha256}`
- `continuation_v3_incomplete_usage_policy={continuation_policy}`
- `continuation_v3_quarantined_run_id=p0-glm-resume-basket-20260715-01`
- `continuation_v3_quarantined_run_max_tokens=9500000`
- `continuation_v3_quarantined_run_max_cost_usd=38.0`
"""
    path.write_text(text, encoding="utf-8")
    return path


def _night_environment(handoff: pathlib.Path, *, phase: str = "smoke") -> dict[str, str]:
    return {
        "GLM_NIGHT_ID": "glm-night-20260714",
        "GLM_NIGHT_AUTHORITY_PATH": str(handoff),
        "GLM_NIGHT_AUTHORITY_SHA256": hashlib.sha256(handoff.read_bytes()).hexdigest(),
        "GLM_NIGHT_MAX_TOKENS": "15000000",
        "GLM_NIGHT_MAX_COST_USD": "50",
        "GLM_NIGHT_PHASE": phase,
    }


def _additional_authority(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path, dict[str, str]]:
    handoff = _night_handoff(
        tmp_path,
        resume=True,
        continuation=True,
        second_continuation=True,
        third_continuation=True,
    )
    ledger = tmp_path / "usage.jsonl"
    baseline = {
        "ts": "2026-07-15T00:00:00+00:00",
        "run_id": "historical-exact",
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "category": "main_generation",
        "prompt_tokens": 90,
        "completion_tokens": 10,
        "cost_usd": 0.01,
    }
    ledger.write_text(json.dumps(baseline, sort_keys=True) + "\n", encoding="utf-8")
    handoff.write_text(
        handoff.read_text(encoding="utf-8")
        + f"""

- `continuation_v4_session_id=p0-glm-continuation-20260715-01`
- `continuation_v4_parent_night_id=p0-glm-resume-20260715-04`
- `continuation_v4_parent_authority_sha256={"e" * 64}`
- `continuation_v4_accounting_policy=owner_authorized_confirmed_plus_bounded_per_call_estimates`
- `continuation_v4_additional_max_tokens=100000000`
- `continuation_v4_additional_max_cost_usd=200.00`
- `continuation_v4_baseline_ledger_rows=1`
- `continuation_v4_baseline_ledger_sha256={hashlib.sha256(ledger.read_bytes()).hexdigest()}`
- `continuation_v4_baseline_confirmed_tokens=100`
- `continuation_v4_baseline_confirmed_cost_usd=0.01`
- `continuation_v4_prompt_price_usd_per_million=0.8862`
- `continuation_v4_completion_price_usd_per_million=2.785`
- `continuation_v4_estimate_safety_multiplier=2`
- `continuation_v4_metadata_poll_max_seconds=600`
- `continuation_v4_max_directed_attempts_per_failure_class=6`
- `continuation_v4_release_historical_full_cap_reservations=true`
""",
        encoding="utf-8",
    )
    environment = _night_environment(handoff, phase="basket")
    environment.update(
        {
            "GLM_NIGHT_ID": "p0-glm-continuation-20260715-01",
            "GLM_NIGHT_MAX_TOKENS": "100000000",
            "GLM_NIGHT_MAX_COST_USD": "200",
        }
    )
    return handoff, ledger, environment


def _glm_request(**overrides: object) -> RunRequest:
    values: dict[str, object] = {
        "run_id": "glm-smoke-01",
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "max_tokens": 100_000,
        "max_cost_usd": 1.0,
        "projected_tokens": 50_000,
        "projected_cost_usd": 0.5,
        "openrouter_enabled": True,
        "profile_name": "openrouter-glm-5.2-functional",
    }
    values.update(overrides)
    return RunRequest(**values)  # type: ignore[arg-type]


def test_glm_requires_hash_bound_run_scoped_authority(tmp_path: pathlib.Path) -> None:
    handoff = _night_handoff(tmp_path)
    environment = _night_environment(handoff)
    environment["GLM_NIGHT_AUTHORITY_SHA256"] = "0" * 64
    with pytest.raises(BudgetPolicyError, match="hash does not match"):
        requested_night_budget(environment, root=tmp_path)

    budget = requested_night_budget(_night_environment(handoff), root=tmp_path)
    request = _glm_request()
    validate_run_request(
        request,
        profile=None,
        observed_project_tokens=0,
        run_scoped_authority=True,
    )
    validate_night_headroom(
        budget,
        request,
        run_state_dir=tmp_path / "runs",
        ledger_path=tmp_path / "usage.jsonl",
        run_kind="gate0_live_probe",
    )


def test_smoke_limit_counts_failed_attempts_not_green_identity_refreshes(
    tmp_path: pathlib.Path,
) -> None:
    handoff = _night_handoff(tmp_path)
    budget = requested_night_budget(_night_environment(handoff), root=tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    now = dt.datetime.now(dt.UTC).isoformat()
    rows: list[str] = []
    for ordinal in range(1, 4):
        run_id = f"glm-smoke-green-{ordinal}"
        marker = {
            "run_id": run_id,
            "kind": "gate0_live_probe",
            "night_id": budget.night_id,
            "night_phase": "smoke",
            "night_authority_sha256": budget.authority_sha256,
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "status": "completed",
            "usage_complete": True,
        }
        (runs / f"{run_id}.json").write_text(json.dumps(marker), encoding="utf-8")
        rows.append(
            json.dumps(
                {
                    "ts": now,
                    "run_id": run_id,
                    "provider": "openrouter",
                    "model": "z-ai/glm-5.2",
                    "category": "main_generation",
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "cost_usd": 0.01,
                }
            )
        )
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")

    validate_night_headroom(
        budget,
        _glm_request(),
        run_state_dir=runs,
        ledger_path=ledger,
        run_kind="gate0_live_probe",
    )


def test_smoke_limit_stops_after_three_failed_attempts(tmp_path: pathlib.Path) -> None:
    handoff = _night_handoff(tmp_path)
    budget = requested_night_budget(_night_environment(handoff), root=tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    now = dt.datetime.now(dt.UTC).isoformat()
    rows: list[str] = []
    for ordinal in range(1, 4):
        run_id = f"glm-smoke-failed-{ordinal}"
        marker = {
            "run_id": run_id,
            "kind": "gate0_live_probe",
            "night_id": budget.night_id,
            "night_phase": "smoke",
            "night_authority_sha256": budget.authority_sha256,
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "status": "failed",
            "usage_complete": True,
        }
        (runs / f"{run_id}.json").write_text(json.dumps(marker), encoding="utf-8")
        rows.append(
            json.dumps(
                {
                    "ts": now,
                    "run_id": run_id,
                    "provider": "openrouter",
                    "model": "z-ai/glm-5.2",
                    "category": "main_generation",
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "cost_usd": 0.01,
                }
            )
        )
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")

    with pytest.raises(BudgetPolicyError, match="failed smoke attempt limit"):
        validate_night_headroom(
            budget,
            _glm_request(),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="gate0_live_probe",
        )


def test_night_guard_stops_on_missing_usage_and_phase_cap(tmp_path: pathlib.Path) -> None:
    handoff = _night_handoff(tmp_path)
    budget = requested_night_budget(_night_environment(handoff), root=tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    marker = {
        "run_id": "glm-review-01",
        "kind": "skill_review",
        "night_id": budget.night_id,
        "night_phase": "smoke",
        "night_authority_sha256": budget.authority_sha256,
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "status": "completed",
        "usage_complete": False,
    }
    (runs / "glm-review-01.json").write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(BudgetPolicyError, match="missing usage"):
        validate_night_headroom(
            budget,
            _glm_request(),
            run_state_dir=runs,
            ledger_path=tmp_path / "usage.jsonl",
            run_kind="gate0_live_probe",
        )

    marker["usage_complete"] = True
    (runs / "glm-review-01.json").write_text(json.dumps(marker), encoding="utf-8")
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "ts": dt.datetime.now(dt.UTC).isoformat(),
                "run_id": "glm-review-01",
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "category": "skill_review",
                "prompt_tokens": 900_000,
                "completion_tokens": 0,
                "cost_usd": 2.5,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(BudgetPolicyError, match="phase headroom"):
        validate_night_headroom(
            budget,
            _glm_request(max_tokens=200_000, max_cost_usd=1.0),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="gate0_live_probe",
        )


def test_night_guard_stops_after_accounted_model_drift(tmp_path: pathlib.Path) -> None:
    handoff = _night_handoff(tmp_path)
    budget = requested_night_budget(_night_environment(handoff), root=tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    run_id = "glm-smoke-drifted"
    marker = {
        "run_id": run_id,
        "kind": "gate0_live_probe",
        "night_id": budget.night_id,
        "night_phase": "smoke",
        "night_authority_sha256": budget.authority_sha256,
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "status": "failed",
        "usage_complete": True,
    }
    (runs / f"{run_id}.json").write_text(json.dumps(marker), encoding="utf-8")
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "ts": dt.datetime.now(dt.UTC).isoformat(),
                "run_id": run_id,
                "provider": "openrouter",
                "model": "google/gemini-3.5-flash",
                "category": "post_task_summary",
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "cost_usd": 0.01,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(BudgetPolicyError, match="provider/model drifted"):
        validate_night_headroom(
            budget,
            _glm_request(),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="gate0_live_probe",
        )


def test_resume_session_counts_parent_usage_and_allows_failed_accounted_drift(
    tmp_path: pathlib.Path,
) -> None:
    parent_authority = "a" * 64
    handoff = _night_handoff(
        tmp_path,
        resume=True,
        parent_authority_sha256=parent_authority,
    )
    environment = _night_environment(handoff)
    environment["GLM_NIGHT_ID"] = "p0-glm-resume-20260715-01"
    budget = requested_night_budget(environment, root=tmp_path)
    assert budget.parent_night_id == "glm-night-20260714"
    assert budget.allow_accounted_model_drift is True
    assert night_marker_fields(budget)["night_parent_id"] == "glm-night-20260714"

    runs = tmp_path / "runs"
    runs.mkdir()
    review_marker = {
        "run_id": "glm-review-parent",
        "kind": "skill_review",
        "night_id": budget.parent_night_id,
        "night_phase": "smoke",
        "night_authority_sha256": parent_authority,
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "status": "completed",
        "usage_complete": True,
    }
    drift_marker = {
        "run_id": "glm-smoke-parent",
        "kind": "gate0_live_probe",
        "night_id": budget.parent_night_id,
        "night_phase": "smoke",
        "night_authority_sha256": parent_authority,
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "status": "failed",
        "usage_complete": True,
        "usage_recovered": True,
        "usage_recovery_sha256": "b" * 64,
        "model_drift_detected": True,
        "observed_models": ["google/gemini-3.5-flash", "z-ai/glm-5.2"],
    }
    (runs / "glm-review-parent.json").write_text(json.dumps(review_marker), encoding="utf-8")
    (runs / "glm-smoke-parent.json").write_text(json.dumps(drift_marker), encoding="utf-8")
    now = dt.datetime.now(dt.UTC).isoformat()
    rows = [
        {
            "ts": now,
            "run_id": "glm-review-parent",
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "category": "skill_review",
            "prompt_tokens": 125_058,
            "completion_tokens": 1_317,
            "cost_usd": 0.180876,
        },
        {
            "ts": now,
            "run_id": "glm-smoke-parent",
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "category": "main_generation",
            "prompt_tokens": 109_349,
            "completion_tokens": 3_079,
            "cost_usd": 0.03894185,
        },
        {
            "ts": now,
            "run_id": "glm-smoke-parent",
            "provider": "openrouter",
            "model": "google/gemini-3.5-flash",
            "category": "post_task_summary",
            "prompt_tokens": 804,
            "completion_tokens": 1_342,
            "cost_usd": 0.013284,
        },
        {
            "ts": now,
            "run_id": "glm-smoke-parent",
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "category": "safety",
            "prompt_tokens": 5_692,
            "completion_tokens": 108,
            "cost_usd": 0.00301819,
        },
    ]
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    validate_night_headroom(
        budget,
        _glm_request(max_tokens=300_000, max_cost_usd=1.0),
        run_state_dir=runs,
        ledger_path=ledger,
        run_kind="gate0_live_probe",
    )
    with pytest.raises(BudgetPolicyError, match="phase headroom"):
        validate_night_headroom(
            budget,
            _glm_request(max_tokens=800_000, max_cost_usd=1.0),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="gate0_live_probe",
        )


def test_resume_session_rejects_unqualified_model_drift(tmp_path: pathlib.Path) -> None:
    parent_authority = "a" * 64
    handoff = _night_handoff(
        tmp_path,
        resume=True,
        parent_authority_sha256=parent_authority,
    )
    environment = _night_environment(handoff)
    environment["GLM_NIGHT_ID"] = "p0-glm-resume-20260715-01"
    budget = requested_night_budget(environment, root=tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    marker = {
        "run_id": "glm-drift-unqualified",
        "kind": "gate0_live_probe",
        "night_id": budget.night_id,
        "night_phase": "smoke",
        "night_authority_sha256": budget.authority_sha256,
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "status": "completed",
        "usage_complete": True,
        "usage_recovered": True,
        "usage_recovery_sha256": "b" * 64,
        "model_drift_detected": True,
        "observed_models": ["google/gemini-3.5-flash"],
    }
    (runs / "glm-drift-unqualified.json").write_text(json.dumps(marker), encoding="utf-8")
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "ts": dt.datetime.now(dt.UTC).isoformat(),
                "run_id": marker["run_id"],
                "provider": "openrouter",
                "model": "google/gemini-3.5-flash",
                "category": "post_task_summary",
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "cost_usd": 0.01,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(BudgetPolicyError, match="provider/model drifted"):
        validate_night_headroom(
            budget,
            _glm_request(),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="gate0_live_probe",
        )


def test_continuation_quarantines_exact_incomplete_run_by_full_cap_reservation(
    tmp_path: pathlib.Path,
) -> None:
    parent_authority = "a" * 64
    continuation_parent_authority = "b" * 64
    handoff = _night_handoff(
        tmp_path,
        resume=True,
        continuation=True,
        parent_authority_sha256=parent_authority,
        continuation_parent_authority_sha256=continuation_parent_authority,
    )
    environment = _night_environment(handoff, phase="pilots")
    environment.update(
        {
            "GLM_NIGHT_ID": "p0-glm-resume-20260715-02",
            "GLM_NIGHT_MAX_TOKENS": "250000",
            "GLM_NIGHT_MAX_COST_USD": "1.5",
        }
    )
    budget = requested_night_budget(environment, root=tmp_path)
    assert budget.parent_night_id == "p0-glm-resume-20260715-01"
    assert budget.ancestor_nights == (("glm-night-20260714", parent_authority),)
    assert budget.quarantined_run_id == "p0-glm-resume-smoke-b01-20260715-04"
    assert budget.quarantined_run_max_tokens == 190_000
    assert budget.quarantined_run_max_cost_usd == 0.8

    runs = tmp_path / "runs"
    runs.mkdir()
    marker = {
        "run_id": budget.quarantined_run_id,
        "kind": "gate0_live_probe",
        "night_id": budget.parent_night_id,
        "night_phase": "pilots",
        "night_authority_sha256": continuation_parent_authority,
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "status": "failed",
        "usage_complete": False,
        "provider_usage_unknown": True,
        "known_tokens": 36_085,
        "known_cost_usd": 0.03213566,
        "max_tokens": 190_000,
        "max_cost_usd": 0.8,
        "accounting_postmortem_sha256": "c" * 64,
        "report_sha256": "d" * 64,
    }
    (runs / f"{budget.quarantined_run_id}.json").write_text(json.dumps(marker), encoding="utf-8")
    ledger = tmp_path / "usage.jsonl"
    ledger_rows = [
        {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "run_id": budget.quarantined_run_id,
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "category": "main_generation",
            "prompt_tokens": 33_238,
            "completion_tokens": 426,
            "cost_usd": 0.03093246,
        },
        {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "run_id": budget.quarantined_run_id,
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "category": "safety",
            "prompt_tokens": 2_359,
            "completion_tokens": 62,
            "cost_usd": 0.0012032,
        },
    ]
    original_ledger = "".join(json.dumps(row) + "\n" for row in ledger_rows)
    ledger.write_text(original_ledger, encoding="utf-8")

    # 190k reservation + 50k request fits 250k. Counting the known 36,085 rows again would fail.
    validate_night_headroom(
        budget,
        _glm_request(
            max_tokens=50_000,
            max_cost_usd=0.5,
            projected_tokens=40_000,
            projected_cost_usd=0.4,
        ),
        run_state_dir=runs,
        ledger_path=ledger,
        run_kind="gate0_live_probe",
    )
    assert ledger.read_text(encoding="utf-8") == original_ledger

    with pytest.raises(BudgetPolicyError, match="aggregate headroom"):
        validate_night_headroom(
            budget,
            _glm_request(
                max_tokens=61_000,
                max_cost_usd=0.5,
                projected_tokens=40_000,
                projected_cost_usd=0.4,
            ),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="gate0_live_probe",
        )

    other = {**marker, "run_id": "other-incomplete", "known_tokens": 1}
    (runs / "other-incomplete.json").write_text(json.dumps(other), encoding="utf-8")
    with pytest.raises(BudgetPolicyError, match="missing usage"):
        validate_night_headroom(
            budget,
            _glm_request(max_tokens=50_000, max_cost_usd=0.5),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="gate0_live_probe",
        )


def test_second_continuation_reserves_two_incomplete_runs_without_double_counting(
    tmp_path: pathlib.Path,
) -> None:
    root_authority = "a" * 64
    first_continuation_parent_authority = "b" * 64
    second_continuation_parent_authority = "c" * 64
    handoff = _night_handoff(
        tmp_path,
        resume=True,
        continuation=True,
        second_continuation=True,
        parent_authority_sha256=root_authority,
        continuation_parent_authority_sha256=first_continuation_parent_authority,
        second_continuation_parent_authority_sha256=(second_continuation_parent_authority),
    )
    environment = _night_environment(handoff, phase="pilots")
    environment.update(
        {
            "GLM_NIGHT_ID": "p0-glm-resume-20260715-03",
            "GLM_NIGHT_MAX_TOKENS": "500000",
            "GLM_NIGHT_MAX_COST_USD": "2.0",
        }
    )
    budget = requested_night_budget(environment, root=tmp_path)
    assert budget.parent_night_id == "p0-glm-resume-20260715-02"
    assert budget.ancestor_nights == (
        ("p0-glm-resume-20260715-01", first_continuation_parent_authority),
        ("glm-night-20260714", root_authority),
    )
    assert budget.additional_quarantined_runs == (
        ("p0-glm-resume-smoke-b01-20260715-05", 250_000, 0.8),
    )
    assert len(night_marker_fields(budget)["night_quarantined_runs"]) == 2

    runs = tmp_path / "runs"
    runs.mkdir()
    reservations = (
        (
            budget.quarantined_run_id,
            "p0-glm-resume-20260715-01",
            first_continuation_parent_authority,
            190_000,
            0.8,
            36_085,
            0.03213566,
        ),
        (
            budget.additional_quarantined_runs[0][0],
            budget.parent_night_id,
            second_continuation_parent_authority,
            250_000,
            0.8,
            36_000,
            0.03187092,
        ),
    )
    now = dt.datetime.now(dt.UTC).isoformat()
    ledger_rows: list[dict[str, object]] = []
    for run_id, night_id, authority, cap_tokens, cap_cost, known_tokens, known_cost in reservations:
        marker = {
            "run_id": run_id,
            "kind": "gate0_live_probe",
            "night_id": night_id,
            "night_phase": "pilots",
            "night_authority_sha256": authority,
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "status": "failed",
            "usage_complete": False,
            "provider_usage_unknown": True,
            "known_tokens": known_tokens,
            "known_cost_usd": known_cost,
            "max_tokens": cap_tokens,
            "max_cost_usd": cap_cost,
            "accounting_postmortem_sha256": "d" * 64,
            "report_sha256": "e" * 64,
        }
        (runs / f"{run_id}.json").write_text(json.dumps(marker), encoding="utf-8")
        prompt_tokens = known_tokens - 1
        ledger_rows.append(
            {
                "ts": now,
                "run_id": run_id,
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "category": "main_generation",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 1,
                "cost_usd": known_cost,
            }
        )
    ledger = tmp_path / "usage.jsonl"
    original_ledger = "".join(json.dumps(row) + "\n" for row in ledger_rows)
    ledger.write_text(original_ledger, encoding="utf-8")

    validate_night_headroom(
        budget,
        _glm_request(
            max_tokens=50_000,
            max_cost_usd=0.3,
            projected_tokens=40_000,
            projected_cost_usd=0.2,
        ),
        run_state_dir=runs,
        ledger_path=ledger,
        run_kind="gate0_live_probe",
    )
    assert ledger.read_text(encoding="utf-8") == original_ledger

    with pytest.raises(BudgetPolicyError, match="aggregate headroom"):
        validate_night_headroom(
            budget,
            _glm_request(
                max_tokens=61_000,
                max_cost_usd=0.3,
                projected_tokens=40_000,
                projected_cost_usd=0.2,
            ),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="gate0_live_probe",
        )


def test_third_continuation_reserves_failed_basket_and_admits_bounded_retry(
    tmp_path: pathlib.Path,
) -> None:
    authorities = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
    handoff = _night_handoff(
        tmp_path,
        resume=True,
        continuation=True,
        second_continuation=True,
        third_continuation=True,
        parent_authority_sha256=authorities[0],
        continuation_parent_authority_sha256=authorities[1],
        second_continuation_parent_authority_sha256=authorities[2],
        third_continuation_parent_authority_sha256=authorities[3],
    )
    environment = _night_environment(handoff, phase="basket")
    environment["GLM_NIGHT_ID"] = "p0-glm-resume-20260715-04"
    budget = requested_night_budget(environment, root=tmp_path)
    assert budget.parent_night_id == "p0-glm-resume-20260715-03"
    assert budget.parent_authority_sha256 == authorities[3]
    assert budget.ancestor_nights == (
        ("p0-glm-resume-20260715-02", authorities[2]),
        ("p0-glm-resume-20260715-01", authorities[1]),
        ("glm-night-20260714", authorities[0]),
    )
    assert budget.additional_quarantined_runs == (
        ("p0-glm-resume-smoke-b01-20260715-05", 250_000, 0.8),
        ("p0-glm-resume-basket-20260715-01", 9_500_000, 38.0),
    )

    runs = tmp_path / "runs"
    runs.mkdir()
    reservations = (
        (
            budget.quarantined_run_id,
            "p0-glm-resume-20260715-01",
            authorities[1],
            "gate0_live_probe",
            190_000,
            0.8,
            36_085,
            0.03213566,
        ),
        (
            budget.additional_quarantined_runs[0][0],
            "p0-glm-resume-20260715-02",
            authorities[2],
            "gate0_live_probe",
            250_000,
            0.8,
            36_000,
            0.03187092,
        ),
        (
            budget.additional_quarantined_runs[1][0],
            budget.parent_night_id,
            authorities[3],
            "full_live_evaluation",
            9_500_000,
            38.0,
            35_869,
            0.03381991,
        ),
    )
    ledger_rows: list[dict[str, object]] = []
    now = dt.datetime.now(dt.UTC).isoformat()
    for (
        run_id,
        night_id,
        authority,
        kind,
        cap_tokens,
        cap_cost,
        known_tokens,
        known_cost,
    ) in reservations:
        marker = {
            "run_id": run_id,
            "kind": kind,
            "night_id": night_id,
            "night_phase": "basket" if kind == "full_live_evaluation" else "pilots",
            "night_authority_sha256": authority,
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "status": "failed",
            "usage_complete": False,
            "provider_usage_unknown": True,
            "known_tokens": known_tokens,
            "known_cost_usd": known_cost,
            "max_tokens": cap_tokens,
            "max_cost_usd": cap_cost,
            "accounting_postmortem_sha256": "e" * 64,
            "report_sha256": "f" * 64,
        }
        (runs / f"{run_id}.json").write_text(json.dumps(marker), encoding="utf-8")
        ledger_rows.append(
            {
                "ts": now,
                "run_id": run_id,
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "category": "main_generation",
                "prompt_tokens": known_tokens - 1,
                "completion_tokens": 1,
                "cost_usd": known_cost,
            }
        )
    ledger = tmp_path / "usage.jsonl"
    original_ledger = "".join(json.dumps(row) + "\n" for row in ledger_rows)
    ledger.write_text(original_ledger, encoding="utf-8")

    validate_night_headroom(
        budget,
        _glm_request(
            max_tokens=2_540_000,
            max_cost_usd=8.6,
            projected_tokens=2_532_690,
            projected_cost_usd=1.11231468,
        ),
        run_state_dir=runs,
        ledger_path=ledger,
        run_kind="full_live_evaluation",
    )
    assert ledger.read_text(encoding="utf-8") == original_ledger

    with pytest.raises(BudgetPolicyError, match="aggregate headroom"):
        validate_night_headroom(
            budget,
            _glm_request(
                max_tokens=5_100_000,
                max_cost_usd=9.0,
                projected_tokens=4_000_000,
                projected_cost_usd=2.0,
            ),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="full_live_evaluation",
        )


def test_additional_authority_starts_after_hash_bound_ledger_prefix(
    tmp_path: pathlib.Path,
) -> None:
    _handoff, ledger, environment = _additional_authority(tmp_path)
    budget = requested_night_budget(environment, root=tmp_path)

    assert budget.additional_authority is True
    assert budget.max_tokens == 100_000_000
    assert budget.max_cost_usd == 200.0
    assert budget.baseline_confirmed_tokens == 100
    assert budget.quarantined_run_id == ""
    validate_night_headroom(
        budget,
        _glm_request(
            max_tokens=100_000_000,
            max_cost_usd=200.0,
            projected_tokens=100_000_000,
            projected_cost_usd=200.0,
        ),
        run_state_dir=tmp_path / "runs",
        ledger_path=ledger,
        run_kind="full_live_evaluation",
    )

    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace('"prompt_tokens": 90', '"prompt_tokens": 91'),
        encoding="utf-8",
    )
    with pytest.raises(BudgetPolicyError, match="baseline prefix drifted"):
        validate_night_headroom(
            budget,
            _glm_request(),
            run_state_dir=tmp_path / "runs",
            ledger_path=ledger,
            run_kind="gate0_live_probe",
        )


def test_additional_authority_records_no_id_anomaly_without_reservation(
    tmp_path: pathlib.Path,
) -> None:
    _handoff, ledger, environment = _additional_authority(tmp_path)
    budget = requested_night_budget(environment, root=tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    marker = {
        **night_marker_fields(budget),
        "run_id": "additional-anomaly-01",
        "kind": "gate0_live_probe",
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "status": "failed",
        "usage_complete": False,
        "provider_usage_unknown": True,
        "evidence_eligible": False,
        "known_tokens": 0,
        "known_cost_usd": 0.0,
        "accounting_artifact_sha256": "a" * 64,
        "accounting_disposition": "pre_generation_anomaly",
        "bounded_request_estimates": [],
        "pre_generation_anomalies": [
            {
                "provider_call_id": "cf_provider_1",
                "generation_id_present": False,
                "status_code": 429,
                "reserved_tokens": 0,
                "reserved_cost_usd": 0.0,
            }
        ],
        "failure_classes": ["provider.http_429"],
    }
    (runs / "additional-anomaly-01.json").write_text(json.dumps(marker), encoding="utf-8")

    validate_night_headroom(
        budget,
        _glm_request(
            max_tokens=100_000_000,
            max_cost_usd=200.0,
            projected_tokens=100_000_000,
            projected_cost_usd=200.0,
        ),
        run_state_dir=runs,
        ledger_path=ledger,
        run_kind="gate0_live_probe",
    )


def test_additional_authority_charges_only_validated_orphan_request_estimate(
    tmp_path: pathlib.Path,
) -> None:
    _handoff, ledger, environment = _additional_authority(tmp_path)
    budget = requested_night_budget(environment, root=tmp_path)
    tokens, cost = bounded_request_estimate(
        budget,
        estimated_prompt_tokens=100,
        configured_max_output_tokens=200,
    )
    assert tokens == 300
    assert cost == pytest.approx(0.00129124)
    runs = tmp_path / "runs"
    runs.mkdir()
    estimate = {
        "generation_id": "gen-orphan-12345678",
        "provider_call_id": "cf_provider_orphan",
        "category": "main_generation",
        "estimated_prompt_tokens": 100,
        "configured_max_output_tokens": 200,
        "estimated_tokens": tokens,
        "estimated_cost_usd": cost,
        "prompt_price_usd_per_million": 0.8862,
        "completion_price_usd_per_million": 2.785,
        "safety_multiplier": 2.0,
        "prompt_estimation_method": "utf8_request_bytes_upper_bound_v1",
    }
    marker = {
        **night_marker_fields(budget),
        "run_id": "additional-orphan-01",
        "kind": "full_live_evaluation",
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "status": "failed",
        "usage_complete": False,
        "provider_usage_unknown": True,
        "evidence_eligible": False,
        "known_tokens": 0,
        "known_cost_usd": 0.0,
        "accounting_artifact_sha256": "b" * 64,
        "accounting_disposition": "orphan_request_estimate",
        "bounded_request_estimates": [estimate],
        "pre_generation_anomalies": [],
        "metadata_poll_sha256": "c" * 64,
        "metadata_poll_elapsed_seconds": 600,
        "failure_classes": ["provider.orphan_generation"],
    }
    (runs / "additional-orphan-01.json").write_text(json.dumps(marker), encoding="utf-8")

    validate_night_headroom(
        budget,
        _glm_request(
            max_tokens=99_999_700,
            max_cost_usd=199.0,
            projected_tokens=99_999_700,
            projected_cost_usd=199.0,
        ),
        run_state_dir=runs,
        ledger_path=ledger,
        run_kind="full_live_evaluation",
    )
    with pytest.raises(BudgetPolicyError, match="additional authority headroom"):
        validate_night_headroom(
            budget,
            _glm_request(
                max_tokens=99_999_701,
                max_cost_usd=199.0,
                projected_tokens=99_999_701,
                projected_cost_usd=199.0,
            ),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="full_live_evaluation",
        )


def test_additional_authority_allows_exact_quarantined_route_drift(
    tmp_path: pathlib.Path,
) -> None:
    _handoff, ledger, environment = _additional_authority(tmp_path)
    budget = requested_night_budget(environment, root=tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    run_id = "additional-review-route-drift-01"
    marker = {
        **night_marker_fields(budget),
        "run_id": run_id,
        "kind": "skill_review",
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "status": "failed",
        "usage_complete": True,
        "provider_usage_unknown": False,
        "evidence_eligible": False,
        "known_tokens": 126_535,
        "known_cost_usd": 0.0978,
        "total_tokens": 126_535,
        "total_cost_usd": 0.0978,
        "observed_providers": ["openai"],
        "observed_models": ["gpt-5.4-mini"],
        "provider_drift_detected": True,
        "model_drift_detected": True,
        "accounting_artifact_sha256": "a" * 64,
        "failure_classes": ["provider.route_drift", "provider.model_drift"],
    }
    marker_path = runs / f"{run_id}.json"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "ts": "2026-07-15T13:27:58+00:00",
                    "run_id": run_id,
                    "provider": "openai",
                    "model": "gpt-5.4-mini",
                    "category": "skill_review",
                    "prompt_tokens": 125_762,
                    "completion_tokens": 773,
                    "cost_usd": 0.0978,
                },
                sort_keys=True,
            )
            + "\n"
        )

    validate_night_headroom(
        budget,
        _glm_request(),
        run_state_dir=runs,
        ledger_path=ledger,
        run_kind="gate0_live_probe",
    )

    marker["observed_providers"] = ["openrouter"]
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(BudgetPolicyError, match="drift is not quarantined"):
        validate_night_headroom(
            budget,
            _glm_request(),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="gate0_live_probe",
        )


def test_additional_authority_bounds_directed_attempts_per_failure_class(
    tmp_path: pathlib.Path,
) -> None:
    _handoff, ledger, environment = _additional_authority(tmp_path)
    budget = requested_night_budget(environment, root=tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    for ordinal in range(6):
        marker = {
            **night_marker_fields(budget),
            "run_id": f"additional-anomaly-{ordinal}",
            "kind": "gate0_live_probe",
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "status": "failed",
            "usage_complete": False,
            "provider_usage_unknown": True,
            "evidence_eligible": False,
            "known_tokens": 0,
            "known_cost_usd": 0.0,
            "accounting_artifact_sha256": "d" * 64,
            "accounting_disposition": "pre_generation_anomaly",
            "bounded_request_estimates": [],
            "pre_generation_anomalies": [
                {
                    "generation_id_present": False,
                    "reserved_tokens": 0,
                    "reserved_cost_usd": 0.0,
                }
            ],
            "failure_classes": ["provider.http_429"],
        }
        (runs / f"additional-anomaly-{ordinal}.json").write_text(
            json.dumps(marker), encoding="utf-8"
        )

    with pytest.raises(BudgetPolicyError, match="directed attempt limit"):
        validate_night_headroom(
            budget,
            _glm_request(),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="gate0_live_probe",
            failure_class="provider.http_429",
        )
    validate_night_headroom(
        budget,
        _glm_request(),
        run_state_dir=runs,
        ledger_path=ledger,
        run_kind="gate0_live_probe",
        failure_class="provider.timeout",
    )


def test_paid_run_budget_reads_failure_class_from_process_environment(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _handoff, ledger, environment = _additional_authority(tmp_path)
    budget = requested_night_budget(environment, root=tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    for ordinal in range(6):
        marker = {
            **night_marker_fields(budget),
            "run_id": f"process-env-anomaly-{ordinal}",
            "kind": "gate0_live_probe",
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "status": "failed",
            "usage_complete": False,
            "provider_usage_unknown": True,
            "evidence_eligible": False,
            "known_tokens": 0,
            "known_cost_usd": 0.0,
            "accounting_artifact_sha256": "e" * 64,
            "accounting_disposition": "pre_generation_anomaly",
            "bounded_request_estimates": [],
            "pre_generation_anomalies": [
                {
                    "generation_id_present": False,
                    "reserved_tokens": 0,
                    "reserved_cost_usd": 0.0,
                }
            ],
            "failure_classes": ["provider.http_429"],
        }
        (runs / f"process-env-anomaly-{ordinal}.json").write_text(
            json.dumps(marker), encoding="utf-8"
        )

    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GLM_FAILURE_CLASS", "provider.http_429")
    monkeypatch.setattr(
        "scripts.budget_control.requested_night_budget",
        lambda source: requested_night_budget(source, root=tmp_path),
    )

    with pytest.raises(BudgetPolicyError, match="directed attempt limit"):
        validate_paid_run_budget(
            _glm_request(),
            run_state_dir=runs,
            ledger_path=ledger,
            run_kind="gate0_live_probe",
        )
