from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
from collections.abc import Iterable
from typing import Any

import yaml

from provider_profiles import (
    CANONICAL_PROFILE_NAME,
    GLM_FUNCTIONAL_PROFILE_NAME,
    ProviderProfileError,
    normalize_provider_model,
    provider_profile,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OPERATOR_LIMITS = pathlib.Path(
    "/home/dmitry/secrets/communication-factory/operator-limits.yaml"
)
DEFAULT_USAGE_LEDGER = ROOT / "runtime" / "budget" / "usage.jsonl"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class BudgetPolicyError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class OperatorProfile:
    model: str
    planning_cap_tokens: int
    demo_reserve_tokens: int
    warning_at_project_tokens: int
    reported_account_allowance_tokens: int

    @property
    def available_project_tokens(self) -> int:
        return max(0, self.planning_cap_tokens - self.demo_reserve_tokens)


@dataclasses.dataclass(frozen=True)
class UsageRecord:
    ts: dt.datetime
    run_id: str
    provider: str
    model: str
    category: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclasses.dataclass(frozen=True)
class RunRequest:
    run_id: str
    provider: str
    model: str
    max_tokens: int
    max_cost_usd: float
    projected_tokens: int
    projected_cost_usd: float
    concurrency: int = 1
    allow_gpt54_comparator: bool = False
    comparator_run_id: str = ""
    openrouter_enabled: bool = False
    profile_name: str = CANONICAL_PROFILE_NAME


@dataclasses.dataclass(frozen=True)
class NightBudget:
    night_id: str
    authority_path: pathlib.Path
    authority_sha256: str
    max_tokens: int
    max_cost_usd: float
    phase: str
    phase_max_tokens: int
    phase_max_cost_usd: float
    parent_night_id: str = ""
    parent_authority_sha256: str = ""
    ancestor_nights: tuple[tuple[str, str], ...] = ()
    allow_accounted_model_drift: bool = False
    incomplete_usage_policy: str = ""
    quarantined_run_id: str = ""
    quarantined_run_max_tokens: int = 0
    quarantined_run_max_cost_usd: float = 0.0
    additional_quarantined_runs: tuple[tuple[str, int, float], ...] = ()
    additional_authority: bool = False
    baseline_ledger_rows: int = 0
    baseline_ledger_sha256: str = ""
    baseline_confirmed_tokens: int = 0
    baseline_confirmed_cost_usd: float = 0.0
    prompt_price_usd_per_million: float = 0.0
    completion_price_usd_per_million: float = 0.0
    estimate_safety_multiplier: float = 0.0
    metadata_poll_max_seconds: int = 0
    max_directed_attempts_per_failure_class: int = 0


@dataclasses.dataclass(frozen=True)
class AdditionalAuthority:
    session_id: str
    parent_night_id: str
    parent_authority_sha256: str
    policy: str
    max_tokens: int
    max_cost_usd: float
    baseline_ledger_rows: int
    baseline_ledger_sha256: str
    baseline_confirmed_tokens: int
    baseline_confirmed_cost_usd: float
    prompt_price_usd_per_million: float
    completion_price_usd_per_million: float
    estimate_safety_multiplier: float
    metadata_poll_max_seconds: int
    max_directed_attempts_per_failure_class: int


def normalize_model(model: str) -> str:
    return normalize_provider_model(model)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise BudgetPolicyError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BudgetPolicyError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise BudgetPolicyError(f"{field} must be a positive integer")
    return parsed


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise BudgetPolicyError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BudgetPolicyError(f"{field} must be a non-negative integer") from exc
    if parsed < 0:
        raise BudgetPolicyError(f"{field} must be a non-negative integer")
    return parsed


def _positive_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BudgetPolicyError(f"{field} must be positive") from exc
    if parsed <= 0:
        raise BudgetPolicyError(f"{field} must be positive")
    return parsed


def _nonnegative_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BudgetPolicyError(f"{field} must be non-negative") from exc
    if parsed < 0:
        raise BudgetPolicyError(f"{field} must be non-negative")
    return parsed


def load_operator_profile(
    path: pathlib.Path = DEFAULT_OPERATOR_LIMITS,
    *,
    model: str = "gpt-5.4-mini",
) -> OperatorProfile:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BudgetPolicyError("external operator limits file is unavailable") from exc
    except yaml.YAMLError as exc:
        raise BudgetPolicyError("external operator limits file is invalid YAML") from exc
    if not isinstance(document, dict) or int(document.get("schema_version") or 0) != 1:
        raise BudgetPolicyError("external operator limits schema is unsupported")
    rules = document.get("rules")
    if not isinstance(rules, dict):
        raise BudgetPolicyError("external operator limits rules are missing")
    required_false = (
        "account_wide_remaining_quota_known",
        "project_counters_are_account_ground_truth",
        "gpt_5_4_comparator_enabled_by_default",
        "openrouter_enabled_by_default",
    )
    if any(rules.get(key) is not False for key in required_false):
        raise BudgetPolicyError("external operator rules do not preserve fail-closed assumptions")
    normalized_model = normalize_model(model)
    models = document.get("models")
    entry = models.get(normalized_model) if isinstance(models, dict) else None
    if not isinstance(entry, dict):
        raise BudgetPolicyError("requested model is absent from external operator limits")
    profile = OperatorProfile(
        model=normalized_model,
        planning_cap_tokens=_positive_int(
            entry.get("project_planning_cap_tokens_per_utc_day"),
            "project planning cap",
        ),
        demo_reserve_tokens=_nonnegative_int(
            entry.get("operator_demo_reserve_tokens"),
            "operator demo reserve",
        ),
        warning_at_project_tokens=_positive_int(
            entry.get("warning_at_project_tokens"),
            "project warning threshold",
        ),
        reported_account_allowance_tokens=_positive_int(
            entry.get("reported_account_allowance_tokens_per_day"),
            "reported account allowance",
        ),
    )
    if profile.demo_reserve_tokens >= profile.planning_cap_tokens:
        raise BudgetPolicyError("operator demo reserve leaves no project planning headroom")
    if profile.warning_at_project_tokens > profile.planning_cap_tokens:
        raise BudgetPolicyError("project warning threshold exceeds the planning cap")
    return profile


def _parse_timestamp(value: Any) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BudgetPolicyError("usage ledger timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def parse_usage_record(payload: Any) -> UsageRecord:
    if not isinstance(payload, dict):
        raise BudgetPolicyError("usage ledger row must be an object")
    provider = str(payload.get("provider") or "").strip()
    model = normalize_model(str(payload.get("model") or ""))
    run_id = str(payload.get("run_id") or "").strip()
    category = str(payload.get("category") or "").strip()
    if not all((provider, model, run_id, category)):
        raise BudgetPolicyError("usage ledger identity fields are incomplete")
    cost_usd = _nonnegative_float(payload.get("cost_usd"), "usage ledger cost")
    return UsageRecord(
        ts=_parse_timestamp(payload.get("ts")),
        run_id=run_id,
        provider=provider,
        model=model,
        category=category,
        prompt_tokens=_nonnegative_int(payload.get("prompt_tokens"), "prompt tokens"),
        completion_tokens=_nonnegative_int(payload.get("completion_tokens"), "completion tokens"),
        cost_usd=cost_usd,
    )


def read_usage_ledger(path: pathlib.Path = DEFAULT_USAGE_LEDGER) -> list[UsageRecord]:
    if not path.exists():
        return []
    records: list[UsageRecord] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BudgetPolicyError("usage ledger is unreadable") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BudgetPolicyError("usage ledger contains malformed JSON") from exc
        records.append(parse_usage_record(payload))
    return records


def observed_tokens_for_utc_day(
    records: Iterable[UsageRecord],
    *,
    model: str,
    day: dt.date | None = None,
) -> int:
    effective_day = day or dt.datetime.now(dt.UTC).date()
    normalized = normalize_model(model)
    return sum(
        record.total_tokens
        for record in records
        if record.model == normalized and record.ts.date() == effective_day
    )


def validate_run_request(
    request: RunRequest,
    *,
    profile: OperatorProfile | None,
    observed_project_tokens: int,
    run_scoped_authority: bool = False,
) -> None:
    if not RUN_ID_PATTERN.fullmatch(request.run_id.strip()):
        raise BudgetPolicyError("run id must use only safe filename characters")
    if request.concurrency != 1:
        raise BudgetPolicyError("live execution must be sequential")
    max_tokens = _positive_int(request.max_tokens, "run token cap")
    max_cost = _positive_float(request.max_cost_usd, "run dollar cap")
    projection_tokens = _positive_int(request.projected_tokens, "projected tokens")
    projection_cost = _positive_float(request.projected_cost_usd, "projected cost")
    provider = str(request.provider or "").strip().lower()
    try:
        selected = provider_profile(request.profile_name)
    except ProviderProfileError as exc:
        raise BudgetPolicyError(str(exc)) from exc
    selected_model = normalize_model(request.model)
    comparator_profile = (
        selected.name == CANONICAL_PROFILE_NAME
        and provider == "openai"
        and selected_model == "gpt-5.4"
    )
    if provider != selected.ledger_provider or (
        selected_model != selected.normalized_model and not comparator_profile
    ):
        raise BudgetPolicyError("run provider/model does not match the selected profile")
    if selected.name == GLM_FUNCTIONAL_PROFILE_NAME:
        if not request.openrouter_enabled or not run_scoped_authority:
            raise BudgetPolicyError("OpenRouter requires the active run-scoped authorization")
    elif provider != "openai":
        raise BudgetPolicyError("provider switching is disabled")
    model = normalize_model(request.model)
    if profile is None:
        if not run_scoped_authority:
            raise BudgetPolicyError("validated operator or run-scoped authority is required")
    else:
        if model != profile.model:
            raise BudgetPolicyError("run model does not match the validated operator profile")
    if model == "gpt-5.4" and (
        not request.allow_gpt54_comparator
        or not request.comparator_run_id.strip()
        or request.comparator_run_id.strip() != request.run_id.strip()
    ):
        raise BudgetPolicyError("gpt-5.4 requires explicit comparator opt-in and run id")
    if projection_tokens > max_tokens or projection_cost > max_cost:
        raise BudgetPolicyError("projected usage exceeds the supplied run cap")
    if profile is not None:
        remaining = profile.available_project_tokens - max(0, observed_project_tokens)
        if max_tokens > remaining or projection_tokens > remaining:
            raise BudgetPolicyError("operator project planning headroom is insufficient")


def _authorized_handoff_caps(path: pathlib.Path) -> tuple[int, float, int, float, int, float]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BudgetPolicyError("night authorization handoff is unavailable") from exc

    def token_cap(pattern: str, label: str) -> int:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match is None:
            raise BudgetPolicyError(f"night authorization is missing {label}")
        return _positive_int(match.group(1).replace(" ", ""), label)

    def dollar_cap(pattern: str, label: str) -> float:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match is None:
            raise BudgetPolicyError(f"night authorization is missing {label}")
        return _positive_float(match.group(1), label)

    aggregate_cost = dollar_cap(
        r"aggregate maximum cost:\s*\$([0-9]+(?:\.[0-9]+)?)",
        "aggregate dollar cap",
    )
    aggregate_tokens = token_cap(
        r"aggregate maximum tokens:\s*([0-9][0-9 ]*)\s*;",
        "aggregate token cap",
    )
    smoke_cost = dollar_cap(
        r"smoke[^\n]*максимум\s*\$([0-9]+(?:\.[0-9]+)?)",
        "smoke dollar cap",
    )
    smoke_tokens = token_cap(
        r"smoke[^\n]*максимум\s*\$[0-9]+(?:\.[0-9]+)?\s*и\s*([0-9][0-9 ]*)\s*tokens",
        "smoke token cap",
    )
    pilot_cost = dollar_cap(
        r"pilots[^\n]*максимум\s*\$([0-9]+(?:\.[0-9]+)?)",
        "pilot dollar cap",
    )
    pilot_tokens = token_cap(
        r"pilots[^\n]*максимум\s*\$[0-9]+(?:\.[0-9]+)?\s*и\s*([0-9][0-9 ]*)\s*tokens",
        "pilot token cap",
    )
    return (
        aggregate_tokens,
        aggregate_cost,
        smoke_tokens,
        smoke_cost,
        pilot_tokens,
        pilot_cost,
    )


def _authorized_resume_session(path: pathlib.Path) -> tuple[str, str, str, bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BudgetPolicyError("night authorization handoff is unavailable") from exc
    patterns = {
        "session": r"resume_session_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "parent": r"resume_parent_night_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "parent_hash": r"resume_parent_authority_sha256=([0-9a-f]{64})",
        "drift_policy": r"resume_model_drift_policy=([a-z_]+)",
    }
    values: dict[str, str] = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, text)
        if match is not None:
            values[name] = match.group(1)
    if not values:
        return "", "", "", False
    if set(values) != set(patterns):
        raise BudgetPolicyError("night resume authorization is incomplete")
    if values["session"] == values["parent"]:
        raise BudgetPolicyError("night resume session must differ from its parent")
    if values["drift_policy"] != "failed_accounted_nonblocking":
        raise BudgetPolicyError("night resume model-drift policy is unsupported")
    return values["session"], values["parent"], values["parent_hash"], True


def _authorized_continuation_session(
    path: pathlib.Path,
) -> tuple[str, str, str, str, str, str, str, int, float] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BudgetPolicyError("night authorization handoff is unavailable") from exc
    patterns = {
        "session": r"continuation_session_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "parent": r"continuation_parent_night_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "parent_hash": r"continuation_parent_authority_sha256=([0-9a-f]{64})",
        "ancestor": r"continuation_ancestor_night_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "ancestor_hash": r"continuation_ancestor_authority_sha256=([0-9a-f]{64})",
        "policy": r"continuation_incomplete_usage_policy=([a-z_]+)",
        "run_id": r"continuation_quarantined_run_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "tokens": r"continuation_quarantined_run_max_tokens=([0-9]+)",
        "cost": r"continuation_quarantined_run_max_cost_usd=([0-9]+(?:\.[0-9]+)?)",
    }
    values: dict[str, str] = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, text)
        if match is not None:
            values[name] = match.group(1)
    if not values:
        return None
    if set(values) != set(patterns):
        raise BudgetPolicyError("night continuation authorization is incomplete")
    if len({values["session"], values["parent"], values["ancestor"]}) != 3:
        raise BudgetPolicyError("night continuation lineage IDs must be distinct")
    expected_policy = "owner_authorized_incomplete_usage_quarantined_by_full_cap_reservation"
    if values["policy"] != expected_policy:
        raise BudgetPolicyError("night continuation incomplete-usage policy is unsupported")
    return (
        values["session"],
        values["parent"],
        values["parent_hash"],
        values["ancestor"],
        values["ancestor_hash"],
        values["policy"],
        values["run_id"],
        _positive_int(values["tokens"], "quarantined run token reservation"),
        _positive_float(values["cost"], "quarantined run dollar reservation"),
    )


def _authorized_second_continuation_session(
    path: pathlib.Path,
) -> tuple[str, str, str, str, str, str, str, str, str, int, float] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BudgetPolicyError("night authorization handoff is unavailable") from exc
    patterns = {
        "session": r"continuation_v2_session_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "parent": r"continuation_v2_parent_night_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "parent_hash": r"continuation_v2_parent_authority_sha256=([0-9a-f]{64})",
        "ancestor": r"continuation_v2_ancestor_night_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "ancestor_hash": r"continuation_v2_ancestor_authority_sha256=([0-9a-f]{64})",
        "root_ancestor": (
            r"continuation_v2_root_ancestor_night_id="
            r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})"
        ),
        "root_ancestor_hash": r"continuation_v2_root_ancestor_authority_sha256=([0-9a-f]{64})",
        "policy": r"continuation_v2_incomplete_usage_policy=([a-z_]+)",
        "run_id": r"continuation_v2_quarantined_run_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "tokens": r"continuation_v2_quarantined_run_max_tokens=([0-9]+)",
        "cost": r"continuation_v2_quarantined_run_max_cost_usd=([0-9]+(?:\.[0-9]+)?)",
    }
    values: dict[str, str] = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, text)
        if match is not None:
            values[name] = match.group(1)
    if not values:
        return None
    if set(values) != set(patterns):
        raise BudgetPolicyError("second night continuation authorization is incomplete")
    lineage_ids = {
        values["session"],
        values["parent"],
        values["ancestor"],
        values["root_ancestor"],
    }
    if len(lineage_ids) != 4:
        raise BudgetPolicyError("second night continuation lineage IDs must be distinct")
    expected_policy = "owner_authorized_incomplete_usage_quarantined_by_full_cap_reservation"
    if values["policy"] != expected_policy:
        raise BudgetPolicyError("second night continuation incomplete-usage policy is unsupported")
    return (
        values["session"],
        values["parent"],
        values["parent_hash"],
        values["ancestor"],
        values["ancestor_hash"],
        values["root_ancestor"],
        values["root_ancestor_hash"],
        values["policy"],
        values["run_id"],
        _positive_int(values["tokens"], "second quarantined run token reservation"),
        _positive_float(values["cost"], "second quarantined run dollar reservation"),
    )


def _authorized_third_continuation_session(
    path: pathlib.Path,
) -> tuple[str, str, str, str, str, str, str, str, str, str, str, int, float] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BudgetPolicyError("night authorization handoff is unavailable") from exc
    patterns = {
        "session": r"continuation_v3_session_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "parent": r"continuation_v3_parent_night_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "parent_hash": r"continuation_v3_parent_authority_sha256=([0-9a-f]{64})",
        "ancestor": r"continuation_v3_ancestor_night_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "ancestor_hash": r"continuation_v3_ancestor_authority_sha256=([0-9a-f]{64})",
        "second_ancestor": (
            r"continuation_v3_second_ancestor_night_id="
            r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})"
        ),
        "second_ancestor_hash": (
            r"continuation_v3_second_ancestor_authority_sha256=([0-9a-f]{64})"
        ),
        "root_ancestor": (
            r"continuation_v3_root_ancestor_night_id="
            r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})"
        ),
        "root_ancestor_hash": r"continuation_v3_root_ancestor_authority_sha256=([0-9a-f]{64})",
        "policy": r"continuation_v3_incomplete_usage_policy=([a-z_]+)",
        "run_id": r"continuation_v3_quarantined_run_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "tokens": r"continuation_v3_quarantined_run_max_tokens=([0-9]+)",
        "cost": r"continuation_v3_quarantined_run_max_cost_usd=([0-9]+(?:\.[0-9]+)?)",
    }
    values: dict[str, str] = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, text)
        if match is not None:
            values[name] = match.group(1)
    if not values:
        return None
    if set(values) != set(patterns):
        raise BudgetPolicyError("third night continuation authorization is incomplete")
    lineage_ids = {
        values["session"],
        values["parent"],
        values["ancestor"],
        values["second_ancestor"],
        values["root_ancestor"],
    }
    if len(lineage_ids) != 5:
        raise BudgetPolicyError("third night continuation lineage IDs must be distinct")
    expected_policy = "owner_authorized_incomplete_usage_quarantined_by_full_cap_reservation"
    if values["policy"] != expected_policy:
        raise BudgetPolicyError("third night continuation incomplete-usage policy is unsupported")
    return (
        values["session"],
        values["parent"],
        values["parent_hash"],
        values["ancestor"],
        values["ancestor_hash"],
        values["second_ancestor"],
        values["second_ancestor_hash"],
        values["root_ancestor"],
        values["root_ancestor_hash"],
        values["policy"],
        values["run_id"],
        _positive_int(values["tokens"], "third quarantined run token reservation"),
        _positive_float(values["cost"], "third quarantined run dollar reservation"),
    )


def _authorized_additional_authority(path: pathlib.Path) -> AdditionalAuthority | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BudgetPolicyError("night authorization handoff is unavailable") from exc
    patterns = {
        "session": r"continuation_v4_session_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "parent": r"continuation_v4_parent_night_id=([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
        "parent_hash": r"continuation_v4_parent_authority_sha256=([0-9a-f]{64})",
        "policy": r"continuation_v4_accounting_policy=([a-z_]+)",
        "tokens": r"continuation_v4_additional_max_tokens=([0-9]+)",
        "cost": r"continuation_v4_additional_max_cost_usd=([0-9]+(?:\.[0-9]+)?)",
        "baseline_rows": r"continuation_v4_baseline_ledger_rows=([0-9]+)",
        "baseline_hash": r"continuation_v4_baseline_ledger_sha256=([0-9a-f]{64})",
        "baseline_tokens": r"continuation_v4_baseline_confirmed_tokens=([0-9]+)",
        "baseline_cost": (r"continuation_v4_baseline_confirmed_cost_usd=([0-9]+(?:\.[0-9]+)?)"),
        "prompt_price": (r"continuation_v4_prompt_price_usd_per_million=([0-9]+(?:\.[0-9]+)?)"),
        "completion_price": (
            r"continuation_v4_completion_price_usd_per_million=([0-9]+(?:\.[0-9]+)?)"
        ),
        "multiplier": r"continuation_v4_estimate_safety_multiplier=([0-9]+(?:\.[0-9]+)?)",
        "poll_seconds": r"continuation_v4_metadata_poll_max_seconds=([0-9]+)",
        "attempts": r"continuation_v4_max_directed_attempts_per_failure_class=([0-9]+)",
        "release": r"continuation_v4_release_historical_full_cap_reservations=(true|false)",
    }
    values: dict[str, str] = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, text)
        if match is not None:
            values[name] = match.group(1)
    if not values:
        return None
    if set(values) != set(patterns):
        raise BudgetPolicyError("additional continuation authority is incomplete")
    expected_policy = "owner_authorized_confirmed_plus_bounded_per_call_estimates"
    if values["policy"] != expected_policy or values["release"] != "true":
        raise BudgetPolicyError("additional continuation accounting policy is unsupported")
    if values["session"] == values["parent"]:
        raise BudgetPolicyError("additional continuation session must differ from its parent")
    poll_seconds = _positive_int(values["poll_seconds"], "metadata poll limit")
    if poll_seconds > 600:
        raise BudgetPolicyError("metadata poll limit exceeds owner authorization")
    attempts = _positive_int(values["attempts"], "directed attempt limit")
    if attempts > 6:
        raise BudgetPolicyError("directed attempt limit exceeds owner authorization")
    return AdditionalAuthority(
        session_id=values["session"],
        parent_night_id=values["parent"],
        parent_authority_sha256=values["parent_hash"],
        policy=values["policy"],
        max_tokens=_positive_int(values["tokens"], "additional token authority"),
        max_cost_usd=_positive_float(values["cost"], "additional dollar authority"),
        baseline_ledger_rows=_positive_int(values["baseline_rows"], "baseline ledger rows"),
        baseline_ledger_sha256=values["baseline_hash"],
        baseline_confirmed_tokens=_positive_int(
            values["baseline_tokens"], "baseline confirmed tokens"
        ),
        baseline_confirmed_cost_usd=_positive_float(
            values["baseline_cost"], "baseline confirmed cost"
        ),
        prompt_price_usd_per_million=_positive_float(values["prompt_price"], "prompt token price"),
        completion_price_usd_per_million=_positive_float(
            values["completion_price"], "completion token price"
        ),
        estimate_safety_multiplier=_positive_float(
            values["multiplier"], "estimate safety multiplier"
        ),
        metadata_poll_max_seconds=poll_seconds,
        max_directed_attempts_per_failure_class=attempts,
    )


def requested_night_budget(
    environment: dict[str, str] | None = None,
    *,
    root: pathlib.Path = ROOT,
) -> NightBudget:
    source = environment if environment is not None else dict(os.environ)
    night_id = str(source.get("GLM_NIGHT_ID") or "").strip()
    if not RUN_ID_PATTERN.fullmatch(night_id):
        raise BudgetPolicyError("GLM_NIGHT_ID must use only safe filename characters")
    authority = pathlib.Path(
        str(source.get("GLM_NIGHT_AUTHORITY_PATH") or root / "HANDOFF_VPS_P0_GLM_BASKET.md")
    ).resolve()
    expected_authority = (root / "HANDOFF_VPS_P0_GLM_BASKET.md").resolve()
    if authority != expected_authority:
        raise BudgetPolicyError("night authorization must use the active repository handoff")
    try:
        actual_hash = hashlib.sha256(authority.read_bytes()).hexdigest()
    except OSError as exc:
        raise BudgetPolicyError("night authorization handoff is unreadable") from exc
    supplied_hash = str(source.get("GLM_NIGHT_AUTHORITY_SHA256") or "").strip().lower()
    if supplied_hash != actual_hash:
        raise BudgetPolicyError("night authorization handoff hash does not match")
    additional = _authorized_additional_authority(authority)
    if additional is not None:
        prior = _authorized_third_continuation_session(authority)
        if prior is None or additional.parent_night_id != prior[0]:
            raise BudgetPolicyError("additional continuation parent differs from prior authority")
        if night_id != additional.session_id:
            raise BudgetPolicyError("active handoff authorizes only its additional continuation")
        max_tokens = _positive_int(source.get("GLM_NIGHT_MAX_TOKENS"), "night token cap")
        max_cost = _positive_float(source.get("GLM_NIGHT_MAX_COST_USD"), "night dollar cap")
        if max_tokens > additional.max_tokens or max_cost > additional.max_cost_usd:
            raise BudgetPolicyError("night cap exceeds the additional handoff authorization")
        phase = str(source.get("GLM_NIGHT_PHASE") or "").strip().lower()
        if phase not in {"smoke", "pilots", "basket"}:
            raise BudgetPolicyError("GLM_NIGHT_PHASE must be smoke, pilots, or basket")
        return NightBudget(
            night_id=night_id,
            authority_path=authority,
            authority_sha256=actual_hash,
            max_tokens=max_tokens,
            max_cost_usd=max_cost,
            phase=phase,
            phase_max_tokens=max_tokens,
            phase_max_cost_usd=max_cost,
            parent_night_id=additional.parent_night_id,
            parent_authority_sha256=additional.parent_authority_sha256,
            allow_accounted_model_drift=True,
            incomplete_usage_policy=additional.policy,
            additional_authority=True,
            baseline_ledger_rows=additional.baseline_ledger_rows,
            baseline_ledger_sha256=additional.baseline_ledger_sha256,
            baseline_confirmed_tokens=additional.baseline_confirmed_tokens,
            baseline_confirmed_cost_usd=additional.baseline_confirmed_cost_usd,
            prompt_price_usd_per_million=additional.prompt_price_usd_per_million,
            completion_price_usd_per_million=(additional.completion_price_usd_per_million),
            estimate_safety_multiplier=additional.estimate_safety_multiplier,
            metadata_poll_max_seconds=additional.metadata_poll_max_seconds,
            max_directed_attempts_per_failure_class=(
                additional.max_directed_attempts_per_failure_class
            ),
        )
    (
        authorized_tokens,
        authorized_cost,
        smoke_tokens,
        smoke_cost,
        pilot_tokens,
        pilot_cost,
    ) = _authorized_handoff_caps(authority)
    (
        resume_session_id,
        parent_night_id,
        parent_authority_sha256,
        allow_accounted_model_drift,
    ) = _authorized_resume_session(authority)
    continuation = _authorized_continuation_session(authority)
    second_continuation = _authorized_second_continuation_session(authority)
    third_continuation = _authorized_third_continuation_session(authority)
    if third_continuation is not None and second_continuation is None:
        raise BudgetPolicyError("third night continuation requires the prior continuation")
    ancestor_nights: tuple[tuple[str, str], ...] = ()
    incomplete_usage_policy = ""
    quarantined_run_id = ""
    quarantined_run_max_tokens = 0
    quarantined_run_max_cost_usd = 0.0
    additional_quarantined_runs: tuple[tuple[str, int, float], ...] = ()
    if continuation is not None:
        (
            continuation_session_id,
            continuation_parent_id,
            continuation_parent_hash,
            continuation_ancestor_id,
            continuation_ancestor_hash,
            incomplete_usage_policy,
            quarantined_run_id,
            quarantined_run_max_tokens,
            quarantined_run_max_cost_usd,
        ) = continuation
        if (
            continuation_parent_id != resume_session_id
            or continuation_ancestor_id != parent_night_id
            or continuation_ancestor_hash != parent_authority_sha256
        ):
            raise BudgetPolicyError("night continuation lineage differs from resume authority")
        if second_continuation is None and night_id != continuation_session_id:
            raise BudgetPolicyError("active handoff authorizes only its continuation session")
        parent_night_id = continuation_parent_id
        parent_authority_sha256 = continuation_parent_hash
        ancestor_nights = ((continuation_ancestor_id, continuation_ancestor_hash),)
        if second_continuation is not None:
            (
                second_session_id,
                second_parent_id,
                second_parent_hash,
                second_ancestor_id,
                second_ancestor_hash,
                second_root_ancestor_id,
                second_root_ancestor_hash,
                second_policy,
                second_run_id,
                second_run_max_tokens,
                second_run_max_cost_usd,
            ) = second_continuation
            if (
                second_parent_id != continuation_session_id
                or second_ancestor_id != continuation_parent_id
                or second_ancestor_hash != continuation_parent_hash
                or second_root_ancestor_id != continuation_ancestor_id
                or second_root_ancestor_hash != continuation_ancestor_hash
                or second_policy != incomplete_usage_policy
            ):
                raise BudgetPolicyError(
                    "second night continuation lineage differs from prior authority"
                )
            if second_run_id == quarantined_run_id:
                raise BudgetPolicyError("second quarantined run must be distinct")
            if third_continuation is None and night_id != second_session_id:
                raise BudgetPolicyError(
                    "active handoff authorizes only its second continuation session"
                )
            parent_night_id = second_parent_id
            parent_authority_sha256 = second_parent_hash
            ancestor_nights = (
                (second_ancestor_id, second_ancestor_hash),
                (second_root_ancestor_id, second_root_ancestor_hash),
            )
            additional_quarantined_runs = (
                (second_run_id, second_run_max_tokens, second_run_max_cost_usd),
            )
            if third_continuation is not None:
                (
                    third_session_id,
                    third_parent_id,
                    third_parent_hash,
                    third_ancestor_id,
                    third_ancestor_hash,
                    third_second_ancestor_id,
                    third_second_ancestor_hash,
                    third_root_ancestor_id,
                    third_root_ancestor_hash,
                    third_policy,
                    third_run_id,
                    third_run_max_tokens,
                    third_run_max_cost_usd,
                ) = third_continuation
                if (
                    third_parent_id != second_session_id
                    or third_ancestor_id != second_parent_id
                    or third_ancestor_hash != second_parent_hash
                    or third_second_ancestor_id != second_ancestor_id
                    or third_second_ancestor_hash != second_ancestor_hash
                    or third_root_ancestor_id != second_root_ancestor_id
                    or third_root_ancestor_hash != second_root_ancestor_hash
                    or third_policy != incomplete_usage_policy
                ):
                    raise BudgetPolicyError(
                        "third night continuation lineage differs from prior authority"
                    )
                if third_run_id in {quarantined_run_id, second_run_id}:
                    raise BudgetPolicyError("third quarantined run must be distinct")
                if night_id != third_session_id:
                    raise BudgetPolicyError(
                        "active handoff authorizes only its third continuation session"
                    )
                parent_night_id = third_parent_id
                parent_authority_sha256 = third_parent_hash
                ancestor_nights = (
                    (third_ancestor_id, third_ancestor_hash),
                    (third_second_ancestor_id, third_second_ancestor_hash),
                    (third_root_ancestor_id, third_root_ancestor_hash),
                )
                additional_quarantined_runs = (
                    (second_run_id, second_run_max_tokens, second_run_max_cost_usd),
                    (third_run_id, third_run_max_tokens, third_run_max_cost_usd),
                )
    elif resume_session_id and night_id != resume_session_id:
        raise BudgetPolicyError("active handoff authorizes only its resume session")
    max_tokens = _positive_int(source.get("GLM_NIGHT_MAX_TOKENS"), "night token cap")
    max_cost = _positive_float(source.get("GLM_NIGHT_MAX_COST_USD"), "night dollar cap")
    if max_tokens > authorized_tokens or max_cost > authorized_cost:
        raise BudgetPolicyError("night cap exceeds the active handoff authorization")
    reservation_tokens = quarantined_run_max_tokens + sum(
        row[1] for row in additional_quarantined_runs
    )
    reservation_cost = quarantined_run_max_cost_usd + sum(
        row[2] for row in additional_quarantined_runs
    )
    if reservation_tokens > max_tokens or reservation_cost > max_cost:
        raise BudgetPolicyError("quarantined run reservation exceeds the active night cap")
    phase = str(source.get("GLM_NIGHT_PHASE") or "").strip().lower()
    if phase == "smoke":
        phase_max_tokens, phase_max_cost = smoke_tokens, smoke_cost
    elif phase == "pilots":
        phase_max_tokens, phase_max_cost = pilot_tokens, pilot_cost
    elif phase == "basket":
        phase_max_tokens, phase_max_cost = max_tokens, max_cost
    else:
        raise BudgetPolicyError("GLM_NIGHT_PHASE must be smoke, pilots, or basket")
    return NightBudget(
        night_id=night_id,
        authority_path=authority,
        authority_sha256=actual_hash,
        max_tokens=max_tokens,
        max_cost_usd=max_cost,
        phase=phase,
        phase_max_tokens=min(max_tokens, phase_max_tokens),
        phase_max_cost_usd=min(max_cost, phase_max_cost),
        parent_night_id=parent_night_id,
        parent_authority_sha256=parent_authority_sha256,
        ancestor_nights=ancestor_nights,
        allow_accounted_model_drift=allow_accounted_model_drift,
        incomplete_usage_policy=incomplete_usage_policy,
        quarantined_run_id=quarantined_run_id,
        quarantined_run_max_tokens=quarantined_run_max_tokens,
        quarantined_run_max_cost_usd=quarantined_run_max_cost_usd,
        additional_quarantined_runs=additional_quarantined_runs,
    )


def night_marker_fields(budget: NightBudget) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "night_id": budget.night_id,
        "night_phase": budget.phase,
        "night_authority_sha256": budget.authority_sha256,
        "night_max_tokens": budget.max_tokens,
        "night_max_cost_usd": budget.max_cost_usd,
        "night_phase_max_tokens": budget.phase_max_tokens,
        "night_phase_max_cost_usd": budget.phase_max_cost_usd,
    }
    if budget.parent_night_id:
        fields.update(
            {
                "night_parent_id": budget.parent_night_id,
                "night_parent_authority_sha256": budget.parent_authority_sha256,
                "night_accounted_model_drift_policy": "failed_accounted_nonblocking",
            }
        )
    if budget.ancestor_nights:
        fields["night_ancestor_lineage"] = [
            {"night_id": night_id, "authority_sha256": authority_sha256}
            for night_id, authority_sha256 in budget.ancestor_nights
        ]
    if budget.additional_authority:
        fields.update(
            {
                "night_accounting_policy": budget.incomplete_usage_policy,
                "night_authority_window": "additional_from_ledger_prefix",
                "night_baseline_ledger_rows": budget.baseline_ledger_rows,
                "night_baseline_ledger_sha256": budget.baseline_ledger_sha256,
                "night_baseline_confirmed_tokens": budget.baseline_confirmed_tokens,
                "night_baseline_confirmed_cost_usd": budget.baseline_confirmed_cost_usd,
                "night_prompt_price_usd_per_million": (budget.prompt_price_usd_per_million),
                "night_completion_price_usd_per_million": (budget.completion_price_usd_per_million),
                "night_estimate_safety_multiplier": budget.estimate_safety_multiplier,
                "night_metadata_poll_max_seconds": budget.metadata_poll_max_seconds,
                "night_max_directed_attempts_per_failure_class": (
                    budget.max_directed_attempts_per_failure_class
                ),
                "night_historical_full_cap_reservations_released": True,
            }
        )
    if budget.quarantined_run_id:
        reservations = [
            {
                "run_id": budget.quarantined_run_id,
                "max_tokens": budget.quarantined_run_max_tokens,
                "max_cost_usd": budget.quarantined_run_max_cost_usd,
            },
            *[
                {"run_id": run_id, "max_tokens": tokens, "max_cost_usd": cost}
                for run_id, tokens, cost in budget.additional_quarantined_runs
            ],
        ]
        fields.update(
            {
                "night_incomplete_usage_policy": budget.incomplete_usage_policy,
                "night_quarantined_run_id": budget.quarantined_run_id,
                "night_quarantined_run_max_tokens": budget.quarantined_run_max_tokens,
                "night_quarantined_run_max_cost_usd": budget.quarantined_run_max_cost_usd,
                "night_quarantined_runs": reservations,
            }
        )
    return fields


def bounded_request_estimate(
    budget: NightBudget,
    *,
    estimated_prompt_tokens: int,
    configured_max_output_tokens: int,
) -> tuple[int, float]:
    if not budget.additional_authority:
        raise BudgetPolicyError("bounded request estimates require additional authority")
    prompt_tokens = _positive_int(estimated_prompt_tokens, "estimated prompt tokens")
    output_tokens = _positive_int(configured_max_output_tokens, "configured max output tokens")
    estimated_cost = (
        budget.estimate_safety_multiplier
        * (
            prompt_tokens * budget.prompt_price_usd_per_million
            + output_tokens * budget.completion_price_usd_per_million
        )
        / 1_000_000
    )
    return prompt_tokens + output_tokens, round(estimated_cost, 8)


def _additional_ledger_records(
    budget: NightBudget,
    ledger_path: pathlib.Path,
) -> tuple[list[UsageRecord], list[UsageRecord]]:
    try:
        raw_lines = ledger_path.read_bytes().splitlines(keepends=True)
    except OSError as exc:
        raise BudgetPolicyError("usage ledger is unreadable") from exc
    if len(raw_lines) < budget.baseline_ledger_rows:
        raise BudgetPolicyError("usage ledger is shorter than the authorized baseline")
    prefix = b"".join(raw_lines[: budget.baseline_ledger_rows])
    if hashlib.sha256(prefix).hexdigest() != budget.baseline_ledger_sha256:
        raise BudgetPolicyError("usage ledger baseline prefix drifted")
    records = read_usage_ledger(ledger_path)
    baseline = records[: budget.baseline_ledger_rows]
    if sum(
        record.total_tokens for record in baseline
    ) != budget.baseline_confirmed_tokens or not math.isclose(
        sum(record.cost_usd for record in baseline),
        budget.baseline_confirmed_cost_usd,
        rel_tol=0,
        abs_tol=1e-9,
    ):
        raise BudgetPolicyError("usage ledger baseline totals drifted")
    return baseline, records[budget.baseline_ledger_rows :]


def _validated_additional_estimates(
    budget: NightBudget,
    marker: dict[str, Any],
) -> tuple[int, float]:
    raw_estimates = marker.get("bounded_request_estimates")
    estimates = raw_estimates if isinstance(raw_estimates, list) else []
    total_tokens = 0
    total_cost = 0.0
    generation_ids: set[str] = set()
    for raw in estimates:
        if not isinstance(raw, dict):
            raise BudgetPolicyError("bounded request estimate is malformed")
        generation_id = str(raw.get("generation_id") or "")
        if (
            re.fullmatch(r"gen-[A-Za-z0-9_-]{8,128}", generation_id) is None
            or generation_id in generation_ids
            or raw.get("prompt_estimation_method") != "utf8_request_bytes_upper_bound_v1"
        ):
            raise BudgetPolicyError("bounded request estimate identity is invalid")
        generation_ids.add(generation_id)
        prompt_tokens = _positive_int(raw.get("estimated_prompt_tokens"), "estimated prompt tokens")
        max_output = _positive_int(
            raw.get("configured_max_output_tokens"), "configured max output tokens"
        )
        expected_tokens, expected_cost = bounded_request_estimate(
            budget,
            estimated_prompt_tokens=prompt_tokens,
            configured_max_output_tokens=max_output,
        )
        if (
            int(raw.get("estimated_tokens") or -1) != expected_tokens
            or not math.isclose(
                float(raw.get("estimated_cost_usd") or -1.0),
                expected_cost,
                rel_tol=0,
                abs_tol=1e-8,
            )
            or not math.isclose(
                float(raw.get("prompt_price_usd_per_million") or -1.0),
                budget.prompt_price_usd_per_million,
                rel_tol=0,
                abs_tol=1e-12,
            )
            or not math.isclose(
                float(raw.get("completion_price_usd_per_million") or -1.0),
                budget.completion_price_usd_per_million,
                rel_tol=0,
                abs_tol=1e-12,
            )
            or not math.isclose(
                float(raw.get("safety_multiplier") or -1.0),
                budget.estimate_safety_multiplier,
                rel_tol=0,
                abs_tol=1e-12,
            )
        ):
            raise BudgetPolicyError("bounded request estimate differs from authority")
        total_tokens += expected_tokens
        total_cost += expected_cost
    raw_anomalies = marker.get("pre_generation_anomalies")
    anomalies = raw_anomalies if isinstance(raw_anomalies, list) else []
    for raw in anomalies:
        if (
            not isinstance(raw, dict)
            or raw.get("generation_id_present") is not False
            or int(raw.get("reserved_tokens") or 0) != 0
            or float(raw.get("reserved_cost_usd") or 0.0) != 0.0
        ):
            raise BudgetPolicyError("pre-generation anomaly reservation is invalid")
    disposition = str(marker.get("accounting_disposition") or "")
    expected_dispositions = {
        (False, True): "pre_generation_anomaly",
        (True, False): "orphan_request_estimate",
        (True, True): "mixed_incomplete_usage",
    }
    if expected_dispositions.get((bool(estimates), bool(anomalies))) != disposition:
        raise BudgetPolicyError("incomplete usage disposition is missing or inconsistent")
    if estimates and (
        re.fullmatch(r"[0-9a-f]{64}", str(marker.get("metadata_poll_sha256") or "")) is None
        or _nonnegative_int(
            marker.get("metadata_poll_elapsed_seconds"), "metadata poll elapsed seconds"
        )
        > budget.metadata_poll_max_seconds
    ):
        raise BudgetPolicyError("orphan metadata poll proof is invalid")
    return total_tokens, round(total_cost, 8)


def _validate_additional_headroom(
    budget: NightBudget,
    request: RunRequest,
    *,
    run_state_dir: pathlib.Path,
    ledger_path: pathlib.Path,
    failure_class: str,
) -> None:
    _, post_baseline_records = _additional_ledger_records(budget, ledger_path)
    markers: list[dict[str, Any]] = []
    if run_state_dir.exists():
        for path in sorted(run_state_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BudgetPolicyError("night run marker is unreadable") from exc
            if isinstance(raw, dict) and raw.get("night_id") == budget.night_id:
                markers.append(raw)
    marker_by_run: dict[str, dict[str, Any]] = {}
    estimate_tokens = 0
    estimate_cost = 0.0
    failure_counts: dict[str, int] = {}
    for marker in markers:
        run_id = str(marker.get("run_id") or "")
        if (
            not RUN_ID_PATTERN.fullmatch(run_id)
            or run_id in marker_by_run
            or marker.get("night_authority_sha256") != budget.authority_sha256
            or marker.get("night_accounting_policy") != budget.incomplete_usage_policy
            or marker.get("night_baseline_ledger_sha256") != budget.baseline_ledger_sha256
            or marker.get("provider") != "openrouter"
            or normalize_model(str(marker.get("model") or "")) != "z-ai/glm-5.2"
        ):
            raise BudgetPolicyError("additional-authority run marker identity drifted")
        marker_by_run[run_id] = marker
        status = marker.get("status")
        if status not in {"completed", "failed"}:
            raise BudgetPolicyError("a previous additional-authority attempt is still active")
        raw_classes = marker.get("failure_classes")
        classes = raw_classes if isinstance(raw_classes, list) else []
        for value in classes:
            normalized = str(value or "").strip().lower()
            if re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", normalized) is None:
                raise BudgetPolicyError("run marker failure class is invalid")
            failure_counts[normalized] = failure_counts.get(normalized, 0) + 1
        if marker.get("usage_complete") is False:
            if (
                status != "failed"
                or marker.get("provider_usage_unknown") is not True
                or marker.get("evidence_eligible") is not False
                or re.fullmatch(
                    r"[0-9a-f]{64}", str(marker.get("accounting_artifact_sha256") or "")
                )
                is None
            ):
                raise BudgetPolicyError("incomplete run is not safely quarantined")
            tokens, cost = _validated_additional_estimates(budget, marker)
            estimate_tokens += tokens
            estimate_cost += cost
    post_by_run: dict[str, list[UsageRecord]] = {}
    for record in post_baseline_records:
        post_by_run.setdefault(record.run_id, []).append(record)
    if set(post_by_run) - set(marker_by_run):
        raise BudgetPolicyError("post-baseline usage has no bound run marker")
    for run_id, marker in marker_by_run.items():
        rows = post_by_run.get(run_id, [])
        known_tokens = sum(row.total_tokens for row in rows)
        known_cost = sum(row.cost_usd for row in rows)
        observed_providers = sorted({row.provider for row in rows})
        observed_models = sorted({row.model for row in rows})
        provider_drifted = bool(rows) and observed_providers != ["openrouter"]
        model_drifted = bool(rows) and observed_models != ["z-ai/glm-5.2"]
        if provider_drifted or model_drifted:
            if (
                marker.get("status") != "failed"
                or marker.get("usage_complete") is not True
                or marker.get("evidence_eligible") is not False
                or marker.get("provider_drift_detected") is not provider_drifted
                or marker.get("model_drift_detected") is not model_drifted
                or sorted(str(value) for value in marker.get("observed_providers") or [])
                != observed_providers
                or sorted(str(value) for value in marker.get("observed_models") or [])
                != observed_models
                or re.fullmatch(
                    r"[0-9a-f]{64}", str(marker.get("accounting_artifact_sha256") or "")
                )
                is None
            ):
                raise BudgetPolicyError("post-baseline provider/model drift is not quarantined")
        elif (
            marker.get("provider_drift_detected") is True
            or marker.get("model_drift_detected") is True
        ):
            raise BudgetPolicyError("post-baseline drift marker is inconsistent with its ledger")
        if marker.get("usage_complete") is True:
            if not rows:
                raise BudgetPolicyError("completed paid run has no post-baseline usage rows")
        elif int(marker.get("known_tokens") or 0) != known_tokens or not math.isclose(
            float(marker.get("known_cost_usd") or 0.0),
            known_cost,
            rel_tol=0,
            abs_tol=1e-8,
        ):
            raise BudgetPolicyError("incomplete run known usage differs from its ledger")
    normalized_failure_class = failure_class.strip().lower()
    if normalized_failure_class:
        if re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", normalized_failure_class) is None:
            raise BudgetPolicyError("requested failure class is invalid")
        if failure_counts.get(normalized_failure_class, 0) >= (
            budget.max_directed_attempts_per_failure_class
        ):
            raise BudgetPolicyError("directed attempt limit for failure class is exhausted")
    confirmed_tokens = sum(record.total_tokens for record in post_baseline_records)
    confirmed_cost = sum(record.cost_usd for record in post_baseline_records)
    used_tokens = confirmed_tokens + estimate_tokens
    used_cost = confirmed_cost + estimate_cost
    if (
        used_tokens + request.max_tokens > budget.max_tokens
        or used_cost + request.max_cost_usd > budget.max_cost_usd
        or used_tokens + request.projected_tokens > budget.max_tokens
        or used_cost + request.projected_cost_usd > budget.max_cost_usd
    ):
        raise BudgetPolicyError("additional authority headroom is insufficient")


def validate_night_headroom(
    budget: NightBudget,
    request: RunRequest,
    *,
    run_state_dir: pathlib.Path,
    ledger_path: pathlib.Path = DEFAULT_USAGE_LEDGER,
    run_kind: str,
    failure_class: str = "",
) -> None:
    if request.profile_name != GLM_FUNCTIONAL_PROFILE_NAME:
        raise BudgetPolicyError("night budget is restricted to the GLM functional profile")
    if budget.additional_authority:
        _validate_additional_headroom(
            budget,
            request,
            run_state_dir=run_state_dir,
            ledger_path=ledger_path,
            failure_class=failure_class,
        )
        return
    records = read_usage_ledger(ledger_path)
    markers: list[dict[str, Any]] = []
    current_markers: list[dict[str, Any]] = []
    authority_by_night = {budget.night_id: budget.authority_sha256}
    if budget.parent_night_id:
        authority_by_night[budget.parent_night_id] = budget.parent_authority_sha256
    for ancestor_id, ancestor_hash in budget.ancestor_nights:
        if ancestor_id in authority_by_night:
            raise BudgetPolicyError("night lineage contains a duplicate ID")
        authority_by_night[ancestor_id] = ancestor_hash
    selected_night_ids = set(authority_by_night)
    reservations = {
        budget.quarantined_run_id: (
            budget.quarantined_run_max_tokens,
            budget.quarantined_run_max_cost_usd,
        ),
        **{run_id: (tokens, cost) for run_id, tokens, cost in budget.additional_quarantined_runs},
    }
    reservations.pop("", None)
    if run_state_dir.exists():
        for path in sorted(run_state_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BudgetPolicyError("night run marker is unreadable") from exc
            if isinstance(payload, dict) and payload.get("night_id") in selected_night_ids:
                markers.append(payload)
                if payload.get("night_id") == budget.night_id:
                    current_markers.append(payload)
    for marker in markers:
        marker_night_id = str(marker.get("night_id") or "")
        expected_authority = authority_by_night[marker_night_id]
        if (
            marker.get("night_authority_sha256") != expected_authority
            or marker.get("provider") != "openrouter"
            or normalize_model(str(marker.get("model") or "")) != "z-ai/glm-5.2"
        ):
            raise BudgetPolicyError("night run marker identity drifted")
        if marker.get("status") not in {"completed", "failed"}:
            raise BudgetPolicyError("a previous night attempt is still active")
        run_id = str(marker.get("run_id") or "")
        quarantined = run_id in reservations
        if quarantined:
            reserved_tokens, reserved_cost = reservations[run_id]
            if (
                not budget.incomplete_usage_policy
                or marker.get("status") != "failed"
                or marker.get("usage_complete") is not False
                or marker.get("provider_usage_unknown") is not True
                or int(marker.get("max_tokens") or 0) != reserved_tokens
                or abs(float(marker.get("max_cost_usd") or 0.0) - reserved_cost) > 1e-9
                or re.fullmatch(
                    r"[0-9a-f]{64}", str(marker.get("accounting_postmortem_sha256") or "")
                )
                is None
                or re.fullmatch(r"[0-9a-f]{64}", str(marker.get("report_sha256") or "")) is None
            ):
                raise BudgetPolicyError(
                    "quarantined incomplete-usage marker differs from authority"
                )
        elif marker.get("usage_complete") is not True:
            raise BudgetPolicyError("a previous night attempt has missing usage")
    run_ids = {str(marker.get("run_id") or "") for marker in markers}
    by_run: dict[str, list[UsageRecord]] = {run_id: [] for run_id in run_ids}
    for record in records:
        if record.run_id in by_run:
            by_run[record.run_id].append(record)
    selected_records: list[UsageRecord] = []
    quarantined_markers: dict[str, dict[str, Any]] = {}
    for marker in markers:
        run_id = str(marker.get("run_id") or "")
        rows = by_run.get(run_id) or []
        if not rows:
            raise BudgetPolicyError("a previous night attempt has no usage ledger rows")
        if any(record.provider != "openrouter" for record in rows):
            raise BudgetPolicyError("night usage provider drifted")
        observed_models = sorted({record.model for record in rows})
        model_drifted = observed_models != ["z-ai/glm-5.2"]
        if model_drifted:
            recovery_hash = str(marker.get("usage_recovery_sha256") or "")
            marker_models = marker.get("observed_models")
            if (
                not budget.allow_accounted_model_drift
                or marker.get("status") != "failed"
                or marker.get("usage_complete") is not True
                or marker.get("usage_recovered") is not True
                or marker.get("model_drift_detected") is not True
                or not isinstance(marker_models, list)
                or sorted(str(model) for model in marker_models) != observed_models
                or re.fullmatch(r"[0-9a-f]{64}", recovery_hash) is None
            ):
                raise BudgetPolicyError("night usage provider/model drifted")
        elif marker.get("model_drift_detected") is True:
            raise BudgetPolicyError("night model-drift marker is inconsistent with its ledger")
        if run_id in reservations:
            known_tokens = sum(record.total_tokens for record in rows)
            known_cost = sum(record.cost_usd for record in rows)
            if (
                known_tokens != int(marker.get("known_tokens") or -1)
                or abs(known_cost - float(marker.get("known_cost_usd") or -1.0)) > 1e-9
            ):
                raise BudgetPolicyError("quarantined run known ledger differs from its marker")
            quarantined_markers[run_id] = marker
        else:
            selected_records.extend(rows)
    if set(quarantined_markers) != set(reservations):
        raise BudgetPolicyError("authorized quarantined run is absent from the selected lineage")
    used_tokens = sum(record.total_tokens for record in selected_records)
    used_cost = sum(record.cost_usd for record in selected_records)
    used_tokens += sum(tokens for tokens, _ in reservations.values())
    used_cost += sum(cost for _, cost in reservations.values())
    phase_ids = {
        str(marker.get("run_id") or "")
        for marker in markers
        if marker.get("night_phase") == budget.phase
    }
    phase_records = [record for record in selected_records if record.run_id in phase_ids]
    phase_tokens = sum(record.total_tokens for record in phase_records)
    phase_cost = sum(record.cost_usd for record in phase_records)
    for run_id, marker in quarantined_markers.items():
        if marker.get("night_phase") == budget.phase:
            reserved_tokens, reserved_cost = reservations[run_id]
            phase_tokens += reserved_tokens
            phase_cost += reserved_cost
    if (
        used_tokens + request.max_tokens > budget.max_tokens
        or used_cost + request.max_cost_usd > budget.max_cost_usd
        or used_tokens + request.projected_tokens > budget.max_tokens
        or used_cost + request.projected_cost_usd > budget.max_cost_usd
    ):
        raise BudgetPolicyError("night aggregate headroom is insufficient")
    if (
        phase_tokens + request.max_tokens > budget.phase_max_tokens
        or phase_cost + request.max_cost_usd > budget.phase_max_cost_usd
        or phase_tokens + request.projected_tokens > budget.phase_max_tokens
        or phase_cost + request.projected_cost_usd > budget.phase_max_cost_usd
    ):
        raise BudgetPolicyError("night phase headroom is insufficient")
    if run_kind == "gate0_live_probe":
        failed_attempts = sum(
            marker.get("kind") == run_kind and marker.get("status") == "failed"
            for marker in current_markers
        )
        if failed_attempts >= 3:
            raise BudgetPolicyError("night failed smoke attempt limit is exhausted")
    if run_kind == "full_live_evaluation":
        attempts = sum(marker.get("kind") == run_kind for marker in current_markers)
        if attempts >= 3:
            raise BudgetPolicyError("night full-basket attempt limit is exhausted")


def validate_paid_run_budget(
    request: RunRequest,
    *,
    run_state_dir: pathlib.Path,
    run_kind: str,
    environment: dict[str, str] | None = None,
    operator_path: pathlib.Path = DEFAULT_OPERATOR_LIMITS,
    ledger_path: pathlib.Path = DEFAULT_USAGE_LEDGER,
) -> NightBudget | None:
    source = dict(os.environ) if environment is None else environment
    if request.profile_name == GLM_FUNCTIONAL_PROFILE_NAME:
        night = requested_night_budget(source)
        validate_run_request(
            request,
            profile=None,
            observed_project_tokens=0,
            run_scoped_authority=True,
        )
        validate_night_headroom(
            night,
            request,
            run_state_dir=run_state_dir,
            ledger_path=ledger_path,
            run_kind=run_kind,
            failure_class=str(source.get("GLM_FAILURE_CLASS") or ""),
        )
        return night
    profile = load_operator_profile(operator_path, model=request.model)
    observed = observed_tokens_for_utc_day(
        read_usage_ledger(ledger_path),
        model=request.model,
    )
    validate_run_request(
        request,
        profile=profile,
        observed_project_tokens=observed,
    )
    return None


def case_boundary_allows_next(
    request: RunRequest,
    *,
    recorded_run_usage: Iterable[UsageRecord],
    next_case_projected_tokens: int,
    next_case_projected_cost_usd: float,
    usage_complete: bool,
    bounded_estimated_tokens: int = 0,
    bounded_estimated_cost_usd: float = 0.0,
) -> bool:
    if not usage_complete:
        return False
    records = list(recorded_run_usage)
    if any(record.run_id != request.run_id for record in records):
        return False
    used_tokens = sum(record.total_tokens for record in records)
    used_cost = sum(record.cost_usd for record in records)
    try:
        reserved_tokens = _nonnegative_int(bounded_estimated_tokens, "bounded estimated tokens")
        reserved_cost = _nonnegative_float(bounded_estimated_cost_usd, "bounded estimated cost")
    except BudgetPolicyError:
        return False
    try:
        next_tokens = _positive_int(next_case_projected_tokens, "next-case tokens")
        next_cost = _positive_float(next_case_projected_cost_usd, "next-case cost")
    except BudgetPolicyError:
        return False
    return (
        used_tokens + reserved_tokens + next_tokens <= request.max_tokens
        and used_cost + reserved_cost + next_cost <= request.max_cost_usd
    )


def assert_provider_unchanged(expected: str, observed: str) -> None:
    if str(expected or "").strip().lower() != str(observed or "").strip().lower():
        raise BudgetPolicyError("automatic provider switching is forbidden")


def status_report(
    *,
    operator_path: pathlib.Path = DEFAULT_OPERATOR_LIMITS,
    ledger_path: pathlib.Path = DEFAULT_USAGE_LEDGER,
    model: str = "gpt-5.4-mini",
) -> str:
    profile = load_operator_profile(operator_path, model=model)
    records = read_usage_ledger(ledger_path)
    observed = observed_tokens_for_utc_day(records, model=profile.model)
    return (
        "budget-status: PASS operator_assumptions=valid "
        f"model={profile.model} project_observed_tokens={observed} "
        "current_run=none account_remaining=unknown provider_switching=disabled"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status",))
    parser.add_argument(
        "--operator-limits",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("OPERATOR_LIMITS_HOST_PATH", DEFAULT_OPERATOR_LIMITS)),
    )
    parser.add_argument("--ledger", type=pathlib.Path, default=DEFAULT_USAGE_LEDGER)
    parser.add_argument("--model", default=os.environ.get("BUDGET_MODEL", "gpt-5.4-mini"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "status":
            print(
                status_report(
                    operator_path=args.operator_limits,
                    ledger_path=args.ledger,
                    model=args.model,
                )
            )
            return 0
    except BudgetPolicyError as exc:
        print(f"budget-status: FAIL: {exc}")
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
