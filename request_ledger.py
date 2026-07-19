"""Durable, request-level accounting for guarded OpenRouter qualification runs.

The ledger is intentionally independent from application databases and Ouroboros
state.  Every physical provider request receives a conservative reservation before
network I/O.  Exact usage replaces that reservation; indeterminate requests retain
their upper bound until generation metadata can reconcile them.
"""

from __future__ import annotations

import contextvars
import fcntl
import hashlib
import json
import math
import os
import tempfile
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

LEDGER_SCHEMA_VERSION = 1
PROMPT_ESTIMATION_METHOD = "serialized_request_unicode_chars_div_2_v2"
DEFAULT_MARGIN = Decimal("0.25")
DEFAULT_RUN_TOKEN_CAP = 50_000_000
DEFAULT_RUN_COST_CAP_USD = Decimal("150")
DEFAULT_REQUEST_TOKEN_CAP = 500_000
DEFAULT_REQUEST_COST_CAP_USD = Decimal("2")
CONFIRMED_PRE_GENERATION_STATUS_CODES = frozenset({400, 401, 403, 404, 409, 413, 422})
ACTIVE_STATUSES = frozenset({"RESERVED", "RETAINED_UNKNOWN"})
COUNTED_STATUSES = frozenset({"RESERVED", "RETAINED_UNKNOWN", "EXACT"})

_ACTIVE_REQUEST: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "communication_factory_active_physical_request",
    default=None,
)


class RequestLedgerError(RuntimeError):
    """Base error for a request-ledger invariant violation."""


class RequestLedgerLimitError(RequestLedgerError):
    """A request or aggregate reservation would exceed an authorized cap."""


class RequestLedgerStateError(RequestLedgerError):
    """The durable ledger is missing, stale, or in an unsafe state."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _decimal(value: object, *, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RequestLedgerStateError(f"{label} must be a decimal number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise RequestLedgerStateError(f"{label} must be a finite non-negative number")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000000000001")), "f")


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise RequestLedgerStateError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RequestLedgerStateError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise RequestLedgerStateError(f"{label} must be a positive integer")
    return parsed


def _non_negative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise RequestLedgerStateError(f"{label} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RequestLedgerStateError(f"{label} must be a non-negative integer") from exc
    if parsed < 0:
        raise RequestLedgerStateError(f"{label} must be a non-negative integer")
    return parsed


def _ledger_path(path: str | os.PathLike[str]) -> Path:
    resolved = Path(path)
    if not resolved.name:
        raise RequestLedgerStateError("request ledger path must name a file")
    return resolved


def _lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    serialized = (
        json.dumps(
            document,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o660)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _read_unlocked(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RequestLedgerStateError("request ledger does not exist")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RequestLedgerStateError("request ledger is unreadable") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise RequestLedgerStateError("request ledger schema is unsupported")
    return {str(key): value for key, value in parsed.items()}


def _with_lock[T](
    path_value: str | os.PathLike[str],
    *,
    exclusive: bool,
    operation: Callable[[dict[str, Any]], T],
) -> T:
    path = _ledger_path(path_value)
    lock_path = _lock_path(path)
    try:
        lock_handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise RequestLedgerStateError("request ledger lock is unavailable") from exc
    with lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        document = _read_unlocked(path)
        result = operation(document)
        if exclusive:
            document["updated_at"] = _utc_now()
            _atomic_write(path, document)
        return result


def initialize_ledger(
    path_value: str | os.PathLike[str],
    *,
    goal_id: str,
    evaluation_id: str,
    provider: str,
    model: str,
    input_price_per_token_usd: Decimal | str,
    output_price_per_token_usd: Decimal | str,
    price_source: str,
    price_observed_at: str,
    run_token_cap: int = DEFAULT_RUN_TOKEN_CAP,
    run_cost_cap_usd: Decimal | str = DEFAULT_RUN_COST_CAP_USD,
    request_token_cap: int = DEFAULT_REQUEST_TOKEN_CAP,
    request_cost_cap_usd: Decimal | str = DEFAULT_REQUEST_COST_CAP_USD,
    margin: Decimal | str = DEFAULT_MARGIN,
) -> dict[str, Any]:
    """Create a new empty ledger; an existing target is never reused."""

    path = _ledger_path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o770)
    if path.exists():
        raise RequestLedgerStateError("request ledger target must be new and empty")
    input_price = _decimal(input_price_per_token_usd, label="input price")
    output_price = _decimal(output_price_per_token_usd, label="output price")
    cost_cap = _decimal(run_cost_cap_usd, label="run cost cap")
    per_request_cost_cap = _decimal(request_cost_cap_usd, label="request cost cap")
    parsed_margin = _decimal(margin, label="reservation margin")
    token_cap = _positive_int(run_token_cap, label="run token cap")
    per_request_token_cap = _positive_int(request_token_cap, label="request token cap")
    for label, value in {
        "goal_id": goal_id,
        "evaluation_id": evaluation_id,
        "provider": provider,
        "model": model,
        "price_source": price_source,
        "price_observed_at": price_observed_at,
    }.items():
        if not str(value).strip():
            raise RequestLedgerStateError(f"{label} must be non-empty")
    now = _utc_now()
    document: dict[str, Any] = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "ledger_id": f"cfledger_{uuid.uuid4().hex}",
        "goal_id": goal_id,
        "evaluation_id": evaluation_id,
        "created_at": now,
        "updated_at": now,
        "route": {
            "provider": provider,
            "model": model,
            "input_price_per_token_usd": _decimal_text(input_price),
            "output_price_per_token_usd": _decimal_text(output_price),
            "price_source": price_source,
            "price_observed_at": price_observed_at,
            "reservation_margin": _decimal_text(parsed_margin),
            "prompt_estimation_method": PROMPT_ESTIMATION_METHOD,
        },
        "caps": {
            "run_tokens": token_cap,
            "run_cost_usd": _decimal_text(cost_cap),
            "request_tokens": per_request_token_cap,
            "request_cost_usd": _decimal_text(per_request_cost_cap),
        },
        "task_bindings": {},
        "requests": [],
    }
    lock_path = _lock_path(path)
    try:
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o660)
    except FileExistsError as exc:
        raise RequestLedgerStateError("request ledger lock target already exists") from exc
    try:
        os.fchmod(lock_descriptor, 0o660)
        os.close(lock_descriptor)
        with lock_path.open("r+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            if path.exists():
                raise RequestLedgerStateError("request ledger target must be new and empty")
            _atomic_write(path, document)
    except BaseException:
        path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)
        raise
    return document


def read_ledger(path_value: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a consistent snapshot without mutating the ledger."""

    return _with_lock(
        path_value,
        exclusive=False,
        operation=lambda document: json.loads(json.dumps(document)),
    )


def bind_task(
    path_value: str | os.PathLike[str],
    *,
    task_id: str,
    case_id: str,
    attempt_id: str,
    request_digest: str,
) -> dict[str, Any]:
    """Bind a task identity before it can produce any provider request."""

    values = {
        "task_id": task_id,
        "case_id": case_id,
        "attempt_id": attempt_id,
        "request_digest": request_digest,
    }
    if any(not str(value).strip() for value in values.values()):
        raise RequestLedgerStateError("task binding fields must be non-empty")

    def operation(document: dict[str, Any]) -> dict[str, Any]:
        bindings = document.get("task_bindings")
        if not isinstance(bindings, dict):
            raise RequestLedgerStateError("request ledger task bindings are invalid")
        expected = {
            "task_id": task_id,
            "case_id": case_id,
            "attempt_id": attempt_id,
            "request_digest": request_digest,
            "evaluation_id": str(document["evaluation_id"]),
        }
        existing = bindings.get(task_id)
        if existing is not None:
            comparable = (
                {key: existing.get(key) for key in expected} if isinstance(existing, dict) else {}
            )
            if comparable != expected:
                raise RequestLedgerStateError("task id is already bound to different metadata")
            return dict(existing)
        binding = {**expected, "bound_at": _utc_now()}
        bindings[task_id] = binding
        return dict(binding)

    return _with_lock(path_value, exclusive=True, operation=operation)


def serialized_request_metrics(payload: Mapping[str, Any]) -> tuple[int, str]:
    """Return a conservative content-free token estimate and request digest.

    Ouroboros' generic estimator uses four Unicode characters per token. This
    goal uses two characters per token, then the ledger applies a separate 25%
    reservation margin. The estimate covers the exact serialized physical
    request kwargs without persisting any prompt or response content.
    """

    serialized_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: f"<{type(value).__name__}>",
    )
    serialized = serialized_text.encode("utf-8")
    token_estimate = max(1, math.ceil(len(serialized_text) / 2))
    return token_estimate, hashlib.sha256(serialized).hexdigest()


def _requests(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = document.get("requests")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RequestLedgerStateError("request ledger rows are invalid")
    return rows


def _route(document: Mapping[str, Any]) -> dict[str, Any]:
    route = document.get("route")
    if not isinstance(route, dict):
        raise RequestLedgerStateError("request ledger route is invalid")
    return route


def _caps(document: Mapping[str, Any]) -> dict[str, Any]:
    caps = document.get("caps")
    if not isinstance(caps, dict):
        raise RequestLedgerStateError("request ledger caps are invalid")
    return caps


def _row_tokens(row: Mapping[str, Any]) -> int:
    if row.get("status") == "EXACT":
        return int(row.get("exact_total_tokens") or 0)
    if row.get("status") in ACTIVE_STATUSES:
        return int(row.get("reserved_total_tokens") or 0)
    return 0


def _row_cost(row: Mapping[str, Any]) -> Decimal:
    if row.get("status") == "EXACT":
        return _decimal(row.get("exact_cost_usd") or "0", label="exact request cost")
    if row.get("status") in ACTIVE_STATUSES:
        return _decimal(row.get("reserved_cost_usd") or "0", label="reserved request cost")
    return Decimal(0)


def ledger_totals(document: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize cap-consumed exact, unknown, and in-flight accounting."""

    rows = _requests(document)
    counted = [row for row in rows if row.get("status") in COUNTED_STATUSES]
    return {
        "tokens": sum(_row_tokens(row) for row in counted),
        "cost_usd": _decimal_text(sum((_row_cost(row) for row in counted), Decimal(0))),
        "exact_requests": sum(row.get("status") == "EXACT" for row in rows),
        "retained_unknown_requests": sum(row.get("status") == "RETAINED_UNKNOWN" for row in rows),
        "inflight_requests": sum(row.get("status") == "RESERVED" for row in rows),
        "released_zero_request": sum(row.get("status") == "RELEASED_ZERO_REQUEST" for row in rows),
        "physical_request_count": len(rows),
    }


def reserve_request(
    path_value: str | os.PathLike[str],
    *,
    task_id: str,
    category: str,
    provider: str,
    model: str,
    provider_call_id: str,
    estimated_prompt_tokens: int,
    configured_max_output_tokens: int,
    request_digest: str,
    default_case_id: str | None = None,
    default_attempt_id: str | None = None,
) -> dict[str, Any]:
    """Persist a conservative reservation immediately before physical network I/O."""

    prompt_estimate = _positive_int(
        estimated_prompt_tokens,
        label="estimated prompt tokens",
    )
    output_ceiling = _positive_int(
        configured_max_output_tokens,
        label="configured output ceiling",
    )
    required = {
        "task_id": task_id,
        "category": category,
        "provider": provider,
        "model": model,
        "provider_call_id": provider_call_id,
        "request_digest": request_digest,
    }
    if any(not str(value).strip() for value in required.values()):
        raise RequestLedgerStateError("physical request identity fields must be non-empty")

    def operation(document: dict[str, Any]) -> dict[str, Any]:
        route = _route(document)
        if provider != route.get("provider") or model != route.get("model"):
            raise RequestLedgerStateError("physical request route differs from ledger route")
        rows = _requests(document)
        if any(row.get("status") == "RESERVED" for row in rows):
            raise RequestLedgerStateError("another physical request is already in flight")
        bindings = document.get("task_bindings")
        if not isinstance(bindings, dict):
            raise RequestLedgerStateError("request ledger task bindings are invalid")
        binding = bindings.get(task_id)
        if binding is None:
            if not default_case_id or not default_attempt_id:
                raise RequestLedgerStateError("provider task has no durable case binding")
            binding = {
                "task_id": task_id,
                "case_id": default_case_id,
                "attempt_id": default_attempt_id,
                "request_digest": "runtime_lifecycle_request",
                "evaluation_id": str(document["evaluation_id"]),
                "bound_at": _utc_now(),
            }
            bindings[task_id] = binding
        if not isinstance(binding, dict):
            raise RequestLedgerStateError("provider task binding is invalid")
        case_id = str(binding.get("case_id") or "")
        if any(
            row.get("case_id") == case_id and row.get("status") == "RETAINED_UNKNOWN"
            for row in rows
        ):
            raise RequestLedgerStateError("case already has a retained unknown physical request")
        margin_multiplier = Decimal(1) + _decimal(
            route.get("reservation_margin"),
            label="reservation margin",
        )
        reserved_prompt = math.ceil(Decimal(prompt_estimate) * margin_multiplier)
        reserved_output = math.ceil(Decimal(output_ceiling) * margin_multiplier)
        reserved_tokens = reserved_prompt + reserved_output
        input_price = _decimal(
            route.get("input_price_per_token_usd"),
            label="input price",
        )
        output_price = _decimal(
            route.get("output_price_per_token_usd"),
            label="output price",
        )
        reserved_cost = (
            Decimal(reserved_prompt) * input_price + Decimal(reserved_output) * output_price
        )
        caps = _caps(document)
        request_token_cap = _positive_int(caps.get("request_tokens"), label="request token cap")
        request_cost_cap = _decimal(caps.get("request_cost_usd"), label="request cost cap")
        if reserved_tokens > request_token_cap or reserved_cost > request_cost_cap:
            raise RequestLedgerLimitError("physical request reservation exceeds its upper bound")
        totals = ledger_totals(document)
        projected_tokens = int(totals["tokens"]) + reserved_tokens
        projected_cost = _decimal(totals["cost_usd"], label="ledger cost") + reserved_cost
        run_token_cap = _positive_int(caps.get("run_tokens"), label="run token cap")
        run_cost_cap = _decimal(caps.get("run_cost_usd"), label="run cost cap")
        if projected_tokens > run_token_cap or projected_cost > run_cost_cap:
            raise RequestLedgerLimitError("physical request would exceed the goal ledger cap")
        now = _utc_now()
        row: dict[str, Any] = {
            "request_id": f"cfreq_{uuid.uuid4().hex}",
            "ordinal": len(rows) + 1,
            "evaluation_id": str(document["evaluation_id"]),
            "attempt_id": str(binding.get("attempt_id") or ""),
            "case_id": case_id,
            "task_id": task_id,
            "category": category,
            "provider": provider,
            "model": model,
            "provider_call_id": provider_call_id,
            "request_digest": request_digest,
            "estimated_prompt_tokens": prompt_estimate,
            "configured_max_output_tokens": output_ceiling,
            "prompt_estimation_method": PROMPT_ESTIMATION_METHOD,
            "reserved_prompt_tokens": reserved_prompt,
            "reserved_output_tokens": reserved_output,
            "reserved_total_tokens": reserved_tokens,
            "reserved_cost_usd": _decimal_text(reserved_cost),
            "status": "RESERVED",
            "reserved_at": now,
            "response_status_code": None,
            "generation_id": None,
        }
        rows.append(row)
        return dict(row)

    return _with_lock(path_value, exclusive=True, operation=operation)


def _find_row(document: Mapping[str, Any], request_id: str) -> dict[str, Any]:
    for row in _requests(document):
        if row.get("request_id") == request_id:
            return row
    raise RequestLedgerStateError("physical request is absent from the ledger")


def observe_response(
    path_value: str | os.PathLike[str],
    *,
    request_id: str,
    status_code: int,
    generation_id: str | None,
) -> dict[str, Any]:
    """Attach safe response correlation metadata as soon as headers arrive."""

    parsed_status = _positive_int(status_code, label="response status code")

    def operation(document: dict[str, Any]) -> dict[str, Any]:
        row = _find_row(document, request_id)
        existing_generation = row.get("generation_id")
        if existing_generation and generation_id and existing_generation != generation_id:
            raise RequestLedgerStateError("physical request observed multiple generation ids")
        row["response_status_code"] = parsed_status
        if generation_id:
            row["generation_id"] = generation_id
        row["response_observed_at"] = _utc_now()
        return dict(row)

    return _with_lock(path_value, exclusive=True, operation=operation)


def reconcile_exact(
    path_value: str | os.PathLike[str],
    *,
    request_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    cost_usd: Decimal | str | None = None,
    generation_id: str | None = None,
    usage_source: str = "provider_response",
) -> dict[str, Any]:
    """Replace a reservation or retained bound with exact provider usage."""

    prompt = _non_negative_int(prompt_tokens, label="prompt tokens")
    completion = _non_negative_int(completion_tokens, label="completion tokens")
    cached = _non_negative_int(cached_tokens, label="cached tokens")
    if prompt + completion <= 0 or cached > prompt:
        raise RequestLedgerStateError("exact provider usage is malformed")
    supplied_cost = (
        _decimal(cost_usd, label="exact provider cost") if cost_usd is not None else None
    )

    def operation(document: dict[str, Any]) -> dict[str, Any]:
        row = _find_row(document, request_id)
        status = str(row.get("status") or "")
        if status == "RELEASED_ZERO_REQUEST":
            raise RequestLedgerStateError("released zero-request row cannot receive exact usage")
        if status == "EXACT":
            expected = (prompt, completion, cached)
            actual = (
                int(row.get("exact_prompt_tokens") or 0),
                int(row.get("exact_completion_tokens") or 0),
                int(row.get("exact_cached_tokens") or 0),
            )
            if expected != actual:
                raise RequestLedgerStateError("exact usage reconciliation is not idempotent")
            if generation_id and row.get("generation_id") not in (None, generation_id):
                raise RequestLedgerStateError("generation id conflicts with reconciled usage")
            return dict(row)
        if status not in ACTIVE_STATUSES:
            raise RequestLedgerStateError("physical request is not reconcilable")
        route = _route(document)
        if supplied_cost is None:
            input_price = _decimal(
                route.get("input_price_per_token_usd"),
                label="input price",
            )
            output_price = _decimal(
                route.get("output_price_per_token_usd"),
                label="output price",
            )
            exact_cost = Decimal(prompt) * input_price + Decimal(completion) * output_price
            cost_source = "pinned_price_from_provider_tokens"
        else:
            exact_cost = supplied_cost
            cost_source = "provider_reported"
        existing_generation = row.get("generation_id")
        if existing_generation and generation_id and existing_generation != generation_id:
            raise RequestLedgerStateError("generation id conflicts with provider response")
        row.update(
            {
                "status": "EXACT",
                "exact_prompt_tokens": prompt,
                "exact_completion_tokens": completion,
                "exact_cached_tokens": cached,
                "exact_total_tokens": prompt + completion,
                "exact_cost_usd": _decimal_text(exact_cost),
                "cost_source": cost_source,
                "usage_source": usage_source,
                "generation_id": generation_id or existing_generation,
                "reconciled_at": _utc_now(),
            }
        )
        caps = _caps(document)
        request_token_cap = _positive_int(caps.get("request_tokens"), label="request token cap")
        request_cost_cap = _decimal(caps.get("request_cost_usd"), label="request cost cap")
        if prompt + completion > request_token_cap or exact_cost > request_cost_cap:
            row["limit_breach"] = "request_cap_exceeded_by_exact_usage"
        totals = ledger_totals(document)
        run_token_cap = _positive_int(caps.get("run_tokens"), label="run token cap")
        run_cost_cap = _decimal(caps.get("run_cost_usd"), label="run cost cap")
        if (
            int(totals["tokens"]) > run_token_cap
            or _decimal(totals["cost_usd"], label="ledger cost") > run_cost_cap
        ):
            row["limit_breach"] = "run_cap_exceeded_by_exact_usage"
        return dict(row)

    return _with_lock(path_value, exclusive=True, operation=operation)


def retain_unknown(
    path_value: str | os.PathLike[str],
    *,
    request_id: str,
    failure_type: str,
) -> dict[str, Any]:
    """Retain the conservative reservation when provider usage is indeterminate."""

    def operation(document: dict[str, Any]) -> dict[str, Any]:
        row = _find_row(document, request_id)
        if row.get("status") == "RETAINED_UNKNOWN":
            return dict(row)
        if row.get("status") == "EXACT":
            return dict(row)
        if row.get("status") != "RESERVED":
            raise RequestLedgerStateError("physical request cannot retain an unknown bound")
        if any(
            other is not row
            and other.get("case_id") == row.get("case_id")
            and other.get("status") == "RETAINED_UNKNOWN"
            for other in _requests(document)
        ):
            raise RequestLedgerStateError("case already has a retained unknown request")
        row.update(
            {
                "status": "RETAINED_UNKNOWN",
                "failure_type": failure_type or "UnknownProviderFailure",
                "retained_at": _utc_now(),
            }
        )
        return dict(row)

    return _with_lock(path_value, exclusive=True, operation=operation)


def release_confirmed_zero_request(
    path_value: str | os.PathLike[str],
    *,
    request_id: str,
    failure_type: str,
) -> dict[str, Any]:
    """Release only an HTTP rejection known to precede provider generation."""

    def operation(document: dict[str, Any]) -> dict[str, Any]:
        row = _find_row(document, request_id)
        if row.get("status") == "RELEASED_ZERO_REQUEST":
            return dict(row)
        if row.get("status") != "RESERVED":
            raise RequestLedgerStateError("physical request reservation is not releasable")
        if row.get("generation_id") or row.get("response_status_code") not in (
            CONFIRMED_PRE_GENERATION_STATUS_CODES
        ):
            raise RequestLedgerStateError("zero provider request is not durably confirmed")
        row.update(
            {
                "status": "RELEASED_ZERO_REQUEST",
                "failure_type": failure_type or "PreGenerationRejection",
                "released_at": _utc_now(),
                "release_reason": "confirmed_pre_generation_http_rejection",
            }
        )
        return dict(row)

    return _with_lock(path_value, exclusive=True, operation=operation)


def finalize_failure(
    path_value: str | os.PathLike[str],
    *,
    request_id: str,
    failure_type: str,
) -> dict[str, Any]:
    """Release a proven pre-generation rejection; otherwise retain the full bound."""

    snapshot = read_ledger(path_value)
    row = _find_row(snapshot, request_id)
    if not row.get("generation_id") and row.get("response_status_code") in (
        CONFIRMED_PRE_GENERATION_STATUS_CODES
    ):
        return release_confirmed_zero_request(
            path_value,
            request_id=request_id,
            failure_type=failure_type,
        )
    return retain_unknown(path_value, request_id=request_id, failure_type=failure_type)


def activate_request(
    path_value: str | os.PathLike[str],
    request_id: str,
) -> contextvars.Token[tuple[str, str] | None]:
    """Expose the current physical request to the synchronous response hook."""

    return _ACTIVE_REQUEST.set((str(_ledger_path(path_value)), request_id))


def reset_active_request(token: contextvars.Token[tuple[str, str] | None]) -> None:
    _ACTIVE_REQUEST.reset(token)


def observe_active_response(*, status_code: int, generation_id: str | None) -> None:
    active = _ACTIVE_REQUEST.get()
    if active is None:
        return
    ledger_path, request_id = active
    observe_response(
        ledger_path,
        request_id=request_id,
        status_code=status_code,
        generation_id=generation_id,
    )


def request_by_generation(
    path_value: str | os.PathLike[str],
    generation_id: str,
) -> dict[str, Any] | None:
    document = read_ledger(path_value)
    matches = [row for row in _requests(document) if row.get("generation_id") == generation_id]
    if len(matches) > 1:
        raise RequestLedgerStateError("generation id is not unique in the request ledger")
    return dict(matches[0]) if matches else None
