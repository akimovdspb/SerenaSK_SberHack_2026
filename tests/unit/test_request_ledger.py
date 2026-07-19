from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from request_ledger import (
    PROMPT_ESTIMATION_METHOD,
    RequestLedgerLimitError,
    RequestLedgerStateError,
    activate_request,
    bind_task,
    finalize_failure,
    initialize_ledger,
    ledger_totals,
    observe_active_response,
    read_ledger,
    reconcile_exact,
    reserve_request,
    reset_active_request,
    retain_unknown,
    serialized_request_metrics,
)


def _new_ledger(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "accounting" / "request-ledger.json"
    values: dict[str, object] = {
        "goal_id": "campaign-authoring-quality-v3",
        "evaluation_id": "eval_quality_001",
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "input_price_per_token_usd": "0.00000091",
        "output_price_per_token_usd": "0.00000286",
        "price_source": "https://openrouter.ai/api/v1/models",
        "price_observed_at": "2026-07-17T00:00:00Z",
    }
    values.update(overrides)
    initialize_ledger(path, **values)  # type: ignore[arg-type]
    bind_task(
        path,
        task_id="task_001",
        case_id="DQ01",
        attempt_id="attempt_001",
        request_digest="a" * 64,
    )
    return path


def _reserve(path: Path, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "task_id": "task_001",
        "category": "main_generation",
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "provider_call_id": "provider_call_001",
        "estimated_prompt_tokens": 1_000,
        "configured_max_output_tokens": 2_000,
        "request_digest": "b" * 64,
    }
    values.update(overrides)
    return reserve_request(path, **values)  # type: ignore[arg-type]


def test_reservation_uses_actual_estimate_margin_and_pinned_prices(tmp_path: Path) -> None:
    path = _new_ledger(tmp_path)

    row = _reserve(path)

    assert row["reserved_prompt_tokens"] == 1_250
    assert row["reserved_output_tokens"] == 2_500
    assert row["reserved_total_tokens"] == 3_750
    expected_cost = Decimal(1_250) * Decimal("0.00000091") + Decimal(2_500) * Decimal("0.00000286")
    assert Decimal(str(row["reserved_cost_usd"])) == expected_cost
    assert ledger_totals(read_ledger(path)) == {
        "tokens": 3_750,
        "cost_usd": f"{expected_cost:.12f}",
        "exact_requests": 0,
        "retained_unknown_requests": 0,
        "inflight_requests": 1,
        "released_zero_request": 0,
        "physical_request_count": 1,
    }


def test_serialized_request_estimate_is_content_free_and_conservative() -> None:
    payload = {
        "messages": [{"role": "user", "content": "Русский synthetic request"}],
        "max_tokens": 16_384,
    }

    estimate, digest = serialized_request_metrics(payload)

    assert estimate == 44
    assert len(digest) == 64
    assert PROMPT_ESTIMATION_METHOD == "serialized_request_unicode_chars_div_2_v2"


def test_review_sized_request_fits_but_larger_request_still_fails_closed(
    tmp_path: Path,
) -> None:
    path = _new_ledger(tmp_path / "review")
    review_estimate, _ = serialized_request_metrics(
        {"messages": [{"role": "user", "content": "x" * 546_000}]}
    )

    row = _reserve(
        path,
        estimated_prompt_tokens=review_estimate,
        configured_max_output_tokens=16_384,
    )

    assert 350_000 < int(row["reserved_total_tokens"]) < 500_000

    oversized_path = _new_ledger(tmp_path / "oversized")
    oversized_estimate, _ = serialized_request_metrics(
        {"messages": [{"role": "user", "content": "x" * 800_000}]}
    )
    with pytest.raises(RequestLedgerLimitError, match="upper bound"):
        _reserve(
            oversized_path,
            estimated_prompt_tokens=oversized_estimate,
            configured_max_output_tokens=16_384,
        )


def test_exact_usage_replaces_reservation_and_is_idempotent(tmp_path: Path) -> None:
    path = _new_ledger(tmp_path)
    row = _reserve(path)

    exact = reconcile_exact(
        path,
        request_id=str(row["request_id"]),
        prompt_tokens=800,
        completion_tokens=500,
        cached_tokens=100,
        generation_id="gen-example123",
    )
    repeated = reconcile_exact(
        path,
        request_id=str(row["request_id"]),
        prompt_tokens=800,
        completion_tokens=500,
        cached_tokens=100,
        generation_id="gen-example123",
    )

    assert exact == repeated
    assert exact["status"] == "EXACT"
    assert exact["cost_source"] == "pinned_price_from_provider_tokens"
    assert ledger_totals(read_ledger(path))["tokens"] == 1_300


def test_only_one_physical_request_can_be_in_flight(tmp_path: Path) -> None:
    path = _new_ledger(tmp_path)
    _reserve(path)

    with pytest.raises(RequestLedgerStateError, match="already in flight"):
        _reserve(path, provider_call_id="provider_call_002")


def test_unknown_bound_is_retained_and_blocks_another_request_for_case(
    tmp_path: Path,
) -> None:
    path = _new_ledger(tmp_path)
    row = _reserve(path)
    retained = retain_unknown(
        path,
        request_id=str(row["request_id"]),
        failure_type="ReadTimeout",
    )

    assert retained["status"] == "RETAINED_UNKNOWN"
    with pytest.raises(RequestLedgerStateError, match="retained unknown"):
        _reserve(path, provider_call_id="provider_call_002")


def test_header_only_timeout_retains_bound_and_can_reconcile_late(tmp_path: Path) -> None:
    path = _new_ledger(tmp_path)
    row = _reserve(path)
    token = activate_request(path, str(row["request_id"]))
    try:
        observe_active_response(status_code=200, generation_id="gen-later1234")
    finally:
        reset_active_request(token)
    retained = finalize_failure(
        path,
        request_id=str(row["request_id"]),
        failure_type="ReadTimeout",
    )
    exact = reconcile_exact(
        path,
        request_id=str(row["request_id"]),
        prompt_tokens=900,
        completion_tokens=400,
        cost_usd="0.003",
        generation_id="gen-later1234",
        usage_source="openrouter_generation_endpoint",
    )

    assert retained["status"] == "RETAINED_UNKNOWN"
    assert exact["status"] == "EXACT"
    assert exact["cost_source"] == "provider_reported"


def test_confirmed_pre_generation_rejection_releases_reservation(tmp_path: Path) -> None:
    path = _new_ledger(tmp_path)
    row = _reserve(path)
    token = activate_request(path, str(row["request_id"]))
    try:
        observe_active_response(status_code=400, generation_id=None)
    finally:
        reset_active_request(token)

    released = finalize_failure(
        path,
        request_id=str(row["request_id"]),
        failure_type="BadRequestError",
    )

    assert released["status"] == "RELEASED_ZERO_REQUEST"
    assert ledger_totals(read_ledger(path))["tokens"] == 0
    retry = _reserve(path, provider_call_id="provider_call_002")
    assert retry["status"] == "RESERVED"


def test_unconfirmed_429_is_not_released(tmp_path: Path) -> None:
    path = _new_ledger(tmp_path)
    row = _reserve(path)
    token = activate_request(path, str(row["request_id"]))
    try:
        observe_active_response(status_code=429, generation_id=None)
    finally:
        reset_active_request(token)

    retained = finalize_failure(
        path,
        request_id=str(row["request_id"]),
        failure_type="RateLimitError",
    )

    assert retained["status"] == "RETAINED_UNKNOWN"


def test_per_request_and_run_caps_fail_before_reservation(tmp_path: Path) -> None:
    request_path = _new_ledger(tmp_path / "request", request_token_cap=2_000)
    with pytest.raises(RequestLedgerLimitError, match="upper bound"):
        _reserve(request_path)
    assert read_ledger(request_path)["requests"] == []

    run_path = _new_ledger(tmp_path / "run", run_token_cap=3_000)
    with pytest.raises(RequestLedgerLimitError, match="goal ledger cap"):
        _reserve(run_path)
    assert read_ledger(run_path)["requests"] == []


def test_task_binding_is_immutable_and_unbound_tasks_are_rejected(tmp_path: Path) -> None:
    path = _new_ledger(tmp_path)
    with pytest.raises(RequestLedgerStateError, match="different metadata"):
        bind_task(
            path,
            task_id="task_001",
            case_id="DQ03",
            attempt_id="attempt_002",
            request_digest="c" * 64,
        )

    with pytest.raises(RequestLedgerStateError, match="no durable case binding"):
        _reserve(path, task_id="task_unbound")


def test_lifecycle_default_binding_is_persisted_before_request(tmp_path: Path) -> None:
    path = _new_ledger(tmp_path)

    row = _reserve(
        path,
        task_id="skill_review_task",
        default_case_id="SCHEMA_PROBE",
        default_attempt_id="schema_attempt_001",
    )

    document = read_ledger(path)
    assert row["case_id"] == "SCHEMA_PROBE"
    assert document["task_bindings"]["skill_review_task"]["case_id"] == "SCHEMA_PROBE"
