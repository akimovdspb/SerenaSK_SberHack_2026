from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from apps.api.app.ouroboros_client import (
    ManagedTaskTransportError,
    TaskTransportFailure,
)

TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
TRANSIENT_RUNTIME_REASONS = frozenset(
    {
        "deadline",
        "timeout",
        "provider_unavailable",
        "rate_limited",
        "service_unavailable",
        "upstream_timeout",
        "connection_reset",
    }
)
TRANSIENT_TRANSPORT_FAILURES = frozenset(
    {
        TaskTransportFailure.CONNECT_TIMEOUT,
        TaskTransportFailure.READ_TIMEOUT,
        TaskTransportFailure.WRITE_TIMEOUT,
        TaskTransportFailure.POOL_TIMEOUT,
        TaskTransportFailure.CONNECTION_RESET,
    }
)


class AttemptOutcome(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    CANCELLED = "CANCELLED"
    RELEASE_UNCONFIRMED = "RELEASE_UNCONFIRMED"


@dataclass(frozen=True)
class RetryAssessment:
    outcome: AttemptOutcome
    reason_code: str
    retry_allowed: bool
    retry_after_seconds: float | None = None
    failure_kind: str = ""


def normalized_task_reason(result: dict[str, Any]) -> str | None:
    """Read only explicit reason-code fields; never infer from prose or stack text."""

    candidates: list[Any] = [
        result.get("reason_code"),
        result.get("normalized_reason"),
        result.get("failure_code"),
    ]
    error = result.get("error")
    if isinstance(error, dict):
        candidates.extend((error.get("code"), error.get("reason_code")))
    for value in candidates:
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower().replace("-", "_")
        if normalized and normalized.replace("_", "").isalnum():
            return normalized
    return None


def assess_transport_failure(
    error: ManagedTaskTransportError,
    *,
    draft_present: bool,
    result_present: bool,
) -> RetryAssessment:
    if draft_present or result_present:
        return RetryAssessment(
            outcome=AttemptOutcome.PERMANENT_FAILURE,
            reason_code="RESULT_ALREADY_PERSISTED",
            retry_allowed=False,
            failure_kind=error.failure.value,
        )
    transient = error.failure in TRANSIENT_TRANSPORT_FAILURES or (
        error.failure is TaskTransportFailure.HTTP_STATUS
        and error.http_status in TRANSIENT_HTTP_STATUSES
    )
    status_suffix = f"_{error.http_status}" if error.http_status is not None else ""
    return RetryAssessment(
        outcome=(
            AttemptOutcome.TRANSIENT_FAILURE if transient else AttemptOutcome.PERMANENT_FAILURE
        ),
        reason_code=(
            f"TRANSIENT_TASK_TRANSPORT{status_suffix}"
            if transient
            else f"TASK_TRANSPORT_PERMANENT{status_suffix}"
        ),
        retry_allowed=transient,
        retry_after_seconds=error.retry_after_seconds if transient else None,
        failure_kind=error.failure.value,
    )


def assess_terminal_failure(
    *,
    result: dict[str, Any],
    draft_present: bool,
    result_present: bool,
    tool_sequence_valid: bool,
    usage_complete: bool,
    user_cancelled: bool,
    terminal_deadline_expired: bool,
) -> RetryAssessment:
    task_status = str(result.get("status") or "").strip().lower()
    reason = normalized_task_reason(result)
    if draft_present or result_present:
        return RetryAssessment(
            outcome=AttemptOutcome.PERMANENT_FAILURE,
            reason_code="RESULT_ALREADY_PERSISTED",
            retry_allowed=False,
            failure_kind=reason or task_status,
        )
    if user_cancelled:
        return RetryAssessment(
            outcome=AttemptOutcome.CANCELLED,
            reason_code="LIVE_TASK_CANCELLED",
            retry_allowed=False,
            failure_kind="user_cancelled",
        )
    if not tool_sequence_valid and task_status == "completed":
        return RetryAssessment(
            outcome=AttemptOutcome.PERMANENT_FAILURE,
            reason_code="TOOL_SEQUENCE_INVALID",
            retry_allowed=False,
            failure_kind="tool_sequence_invalid",
        )
    if task_status == "completed" and not usage_complete:
        return RetryAssessment(
            outcome=AttemptOutcome.PERMANENT_FAILURE,
            reason_code="PROVIDER_USAGE_INCOMPLETE",
            retry_allowed=False,
            failure_kind="usage_incomplete",
        )
    if terminal_deadline_expired:
        return RetryAssessment(
            outcome=AttemptOutcome.TRANSIENT_FAILURE,
            reason_code="TERMINAL_DEADLINE",
            retry_allowed=True,
            failure_kind="deadline",
        )
    if reason in TRANSIENT_RUNTIME_REASONS:
        return RetryAssessment(
            outcome=AttemptOutcome.TRANSIENT_FAILURE,
            reason_code=f"TRANSIENT_RUNTIME_{reason.upper()}",
            retry_allowed=True,
            failure_kind=reason,
        )
    if task_status == "cancelled":
        return RetryAssessment(
            outcome=AttemptOutcome.PERMANENT_FAILURE,
            reason_code="LIVE_TASK_CANCELLED",
            retry_allowed=False,
            failure_kind=reason or task_status,
        )
    return RetryAssessment(
        outcome=AttemptOutcome.PERMANENT_FAILURE,
        reason_code=(
            "LIVE_TASK_FAILED" if task_status != "completed" else "LIVE_DRAFT_NOT_PERSISTED"
        ),
        retry_allowed=False,
        failure_kind=reason or task_status,
    )
