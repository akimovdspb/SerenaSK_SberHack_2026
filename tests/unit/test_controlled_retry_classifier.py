from __future__ import annotations

import pytest

from apps.api.app.ouroboros_client import (
    ManagedTaskTransportError,
    TaskTransportFailure,
)
from apps.api.app.workflow.retry import (
    AttemptOutcome,
    assess_terminal_failure,
    assess_transport_failure,
)


@pytest.mark.parametrize(
    "failure",
    [
        TaskTransportFailure.CONNECT_TIMEOUT,
        TaskTransportFailure.READ_TIMEOUT,
        TaskTransportFailure.WRITE_TIMEOUT,
        TaskTransportFailure.POOL_TIMEOUT,
        TaskTransportFailure.CONNECTION_RESET,
    ],
)
def test_typed_timeout_and_reset_classes_are_retryable(
    failure: TaskTransportFailure,
) -> None:
    assessment = assess_transport_failure(
        ManagedTaskTransportError(
            "safe test transport failure",
            phase="submit",
            failure=failure,
        ),
        draft_present=False,
        result_present=False,
    )

    assert assessment.outcome is AttemptOutcome.TRANSIENT_FAILURE
    assert assessment.retry_allowed is True
    assert assessment.failure_kind == failure.value


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_exact_transient_http_allowlist_is_retryable(status: int) -> None:
    assessment = assess_transport_failure(
        ManagedTaskTransportError(
            "safe test HTTP failure",
            phase="submit",
            failure=TaskTransportFailure.HTTP_STATUS,
            http_status=status,
            retry_after_seconds=0.75,
        ),
        draft_present=False,
        result_present=False,
    )

    assert assessment.retry_allowed is True
    assert assessment.retry_after_seconds == 0.75


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_other_client_statuses_are_permanent(status: int) -> None:
    assessment = assess_transport_failure(
        ManagedTaskTransportError(
            "safe test HTTP failure",
            phase="submit",
            failure=TaskTransportFailure.HTTP_STATUS,
            http_status=status,
        ),
        draft_present=False,
        result_present=False,
    )

    assert assessment.outcome is AttemptOutcome.PERMANENT_FAILURE
    assert assessment.retry_allowed is False


@pytest.mark.parametrize(
    "reason",
    [
        "deadline",
        "timeout",
        "provider_unavailable",
        "rate_limited",
        "service_unavailable",
        "upstream_timeout",
        "connection_reset",
    ],
)
def test_explicit_normalized_runtime_reasons_are_retryable(reason: str) -> None:
    assessment = assess_terminal_failure(
        result={"status": "failed", "reason_code": reason},
        draft_present=False,
        result_present=False,
        tool_sequence_valid=True,
        usage_complete=False,
        user_cancelled=False,
        terminal_deadline_expired=False,
    )

    assert assessment.retry_allowed is True
    assert assessment.failure_kind == reason


def test_free_text_is_never_used_as_a_transient_reason() -> None:
    assessment = assess_terminal_failure(
        result={"status": "failed", "error": "provider unavailable after timeout"},
        draft_present=False,
        result_present=False,
        tool_sequence_valid=True,
        usage_complete=False,
        user_cancelled=False,
        terminal_deadline_expired=False,
    )

    assert assessment.outcome is AttemptOutcome.PERMANENT_FAILURE
    assert assessment.retry_allowed is False


def test_persisted_result_blocks_retry_even_for_a_transient_transport() -> None:
    assessment = assess_transport_failure(
        ManagedTaskTransportError(
            "safe test timeout",
            phase="submit",
            failure=TaskTransportFailure.READ_TIMEOUT,
        ),
        draft_present=True,
        result_present=True,
    )

    assert assessment.reason_code == "RESULT_ALREADY_PERSISTED"
    assert assessment.retry_allowed is False
