from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from provider_profiles import (
    CANONICAL_PROFILE_NAME,
    ProviderProfile,
    ProviderProfileError,
    provider_profile,
    requested_provider_profile,
)
from scripts.budget_control import (
    DEFAULT_USAGE_LEDGER,
    RUN_ID_PATTERN,
    BudgetPolicyError,
    NightBudget,
    RunRequest,
    UsageRecord,
    bounded_request_estimate,
    case_boundary_allows_next,
    night_marker_fields,
    normalize_model,
    read_usage_ledger,
    validate_paid_run_budget,
)
from scripts.evaluation import EXPECTED_PATH, evaluate_live_case_report
from scripts.generation_metadata import metadata_usage_rows, poll_generation_metadata
from scripts.live_probe import (
    RUN_STATE_DIR,
    append_usage,
    usage_rows_from_report,
    verify_running_profile,
)
from scripts.preflight import run_preflight

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE_ROOT = ROOT / "runtime" / "evaluation" / "live"
READINESS_PATH = ROOT / "runtime" / "evaluation" / "live-readiness.json"
CONTRACT_LOCK_PATH = ROOT / "runtime" / "contracts" / "communication_factory.lock.json"
HANDOFF_PATH = ROOT / "HANDOFF_VPS_P0_GLM_BASKET.md"
ALLOWED_TOOLS = ("mcp_factory__cf_context_get", "mcp_factory__cf_draft_save")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclasses.dataclass(frozen=True)
class CasePlan:
    case_id: str
    paid_operation_weight: int


CASE_PLAN = (
    CasePlan("B02", 1),
    CasePlan("B01", 3),
    CasePlan("B03", 1),
    CasePlan("B04", 1),
    CasePlan("B05", 1),
    CasePlan("B06", 1),
    CasePlan("B07", 1),
    CasePlan("B08", 1),
    CasePlan("B09", 1),
    CasePlan("B10", 1),
    CasePlan("B11", 0),
    CasePlan("B12", 0),
    CasePlan("B13", 0),
    CasePlan("B14", 1),
    CasePlan("B15", 2),
)
TOTAL_PAID_OPERATION_WEIGHT = sum(item.paid_operation_weight for item in CASE_PLAN)

CaseExecutor = Callable[[str, str, str, str], tuple[int, dict[str, Any]]]
ExportCopier = Callable[[str, pathlib.Path], None]
PreflightExecutor = Callable[[], dict[str, Any]]
RuleCleanup = Callable[[str, str], dict[str, Any]]
UsageAppender = Callable[[list[dict[str, Any]]], None]
MetadataPoller = Callable[[list[dict[str, Any]], int], dict[str, Any]]


class LiveEvaluationError(RuntimeError):
    pass


def poll_orphan_metadata(requests: list[dict[str, Any]], max_seconds: int) -> dict[str, Any]:
    return poll_generation_metadata(requests, max_seconds=max_seconds)


@dataclasses.dataclass(frozen=True)
class PreflightContext:
    request: RunRequest
    commit: str
    branch: str
    contract_hash: str
    basket_hash: str
    image_id: str
    runtime_report: dict[str, Any]
    readiness: dict[str, Any]
    night: NightBudget | None = None


def _load_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveEvaluationError(f"{label} is unreadable") from exc
    if not isinstance(raw, dict):
        raise LiveEvaluationError(f"{label} must be a JSON object")
    return {str(key): value for key, value in raw.items()}


def _atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _required_positive_int(environment: Mapping[str, str], name: str) -> int:
    try:
        value = int(environment.get(name, "0"))
    except ValueError as exc:
        raise LiveEvaluationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise LiveEvaluationError(f"{name} must be a positive integer")
    return value


def _required_positive_float(environment: Mapping[str, str], name: str) -> float:
    try:
        value = float(environment.get(name, "0"))
    except ValueError as exc:
        raise LiveEvaluationError(f"{name} must be positive") from exc
    if value <= 0:
        raise LiveEvaluationError(f"{name} must be positive")
    return value


def requested_run(environment: Mapping[str, str] = os.environ) -> RunRequest:
    if environment.get("ALLOW_LIVE_EVAL", "").lower() != "true":
        raise LiveEvaluationError("full live evaluation requires ALLOW_LIVE_EVAL=true")
    try:
        profile = requested_provider_profile(environment)
    except ProviderProfileError as exc:
        raise LiveEvaluationError(str(exc)) from exc
    max_tokens = _required_positive_int(environment, "EVAL_MAX_TOKENS")
    max_cost = _required_positive_float(environment, "EVAL_MAX_COST_USD")
    try:
        concurrency = int(environment.get("EVAL_CONCURRENCY", "0"))
    except ValueError as exc:
        raise LiveEvaluationError("EVAL_CONCURRENCY must equal 1") from exc
    run_id = str(environment.get("EVALUATION_ID") or "").strip()
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise LiveEvaluationError("EVALUATION_ID must use safe filename characters")
    if concurrency != 1:
        raise LiveEvaluationError("EVAL_CONCURRENCY must equal 1")
    return RunRequest(
        run_id=run_id,
        provider=profile.ledger_provider,
        model=profile.normalized_model,
        max_tokens=max_tokens,
        max_cost_usd=max_cost,
        projected_tokens=max_tokens,
        projected_cost_usd=max_cost,
        concurrency=concurrency,
        openrouter_enabled=profile.runtime_provider == "openrouter",
        profile_name=profile.name,
    )


def _git(args: list[str]) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise LiveEvaluationError("Git live-evaluation identity check failed")
    return process.stdout.strip()


def git_identity(*, required_branch: str = "codex/p0-autonomous") -> tuple[str, str]:
    commit = _git(["rev-parse", "HEAD"])
    branch = _git(["branch", "--show-current"])
    if not COMMIT_PATTERN.fullmatch(commit):
        raise LiveEvaluationError("Git commit identity is invalid")
    if branch != required_branch:
        raise LiveEvaluationError(f"full live evaluation requires {required_branch}")
    if _git(["status", "--porcelain=v1"]):
        raise LiveEvaluationError("full live evaluation requires a clean frozen commit")
    return commit, branch


def validate_strict_contract(contract: dict[str, Any]) -> None:
    raw_tools = contract.get("tools")
    tools = raw_tools if isinstance(raw_tools, dict) else {}
    raw_adapter = tools.get("strict_adapter")
    adapter = raw_adapter if isinstance(raw_adapter, dict) else {}
    raw_schemas = tools.get("strict_provider_schemas")
    schemas = raw_schemas if isinstance(raw_schemas, dict) else {}
    if (
        adapter.get("active") is not True
        or adapter.get("decision_id") != "CF-RP-001"
        or not SHA256_PATTERN.fullmatch(str(adapter.get("adapter_hash") or ""))
    ):
        raise LiveEvaluationError("approved CF-RP-001 strict adapter is not active")
    if set(schemas) != set(ALLOWED_TOOLS):
        raise LiveEvaluationError("strict provider schema set is not the exact two factory tools")
    for tool_name in ALLOWED_TOOLS:
        raw = schemas.get(tool_name)
        row = raw if isinstance(raw, dict) else {}
        if (
            row.get("strict") is not True
            or row.get("supported_subset") is not True
            or not SHA256_PATTERN.fullmatch(str(row.get("schema_hash") or ""))
        ):
            raise LiveEvaluationError(f"strict provider schema is not proven for {tool_name}")


def validate_retry_linkage(
    environment: Mapping[str, str] = os.environ,
    *,
    run_state_dir: pathlib.Path | None = None,
    live_root: pathlib.Path | None = None,
) -> tuple[str, str]:
    effective_run_state = run_state_dir or RUN_STATE_DIR
    effective_live_root = live_root or LIVE_ROOT
    previous = str(environment.get("PREVIOUS_EVALUATION_ID") or "").strip()
    reason = str(environment.get("EVALUATION_RETRY_REASON") or "").strip()
    if bool(previous) != bool(reason):
        raise LiveEvaluationError("retry linkage requires previous evaluation id and reason")
    if not previous:
        return "", ""
    if not RUN_ID_PATTERN.fullmatch(previous):
        raise LiveEvaluationError("previous evaluation id is invalid")
    if len(reason) > 500 or any(ord(character) < 32 for character in reason):
        raise LiveEvaluationError("evaluation retry reason is invalid")
    marker = _load_object(
        effective_run_state / f"{previous}.json",
        "previous evaluation marker",
    )
    if marker.get("kind") != "full_live_evaluation" or marker.get("status") != "failed":
        raise LiveEvaluationError("linked previous evaluation is not a preserved failed attempt")
    if not (effective_live_root / previous).is_dir():
        raise LiveEvaluationError("linked previous evaluation directory is unavailable")
    return previous, reason


def _readiness_generation_contract(
    report: dict[str, Any],
    *,
    profile: ProviderProfile,
) -> dict[str, str]:
    raw_run = report.get("run")
    run = raw_run if isinstance(raw_run, dict) else {}
    raw_ledger = report.get("provider_call_ledger")
    ledger = raw_ledger if isinstance(raw_ledger, dict) else {}
    providers: set[str] = set()
    models: set[str] = set()
    for raw_row in ledger.values():
        if not isinstance(raw_row, dict) or int(raw_row.get("call_count") or 0) <= 0:
            continue
        providers.update(str(item) for item in raw_row.get("providers") or [])
        models.update(normalize_model(str(item)) for item in raw_row.get("models") or [])
    contract = {
        "prompt_hash": str(run.get("prompt_hash") or ""),
        "skill_content_hash": str(run.get("skill_content_hash") or ""),
        "tool_inventory_hash": str(run.get("tool_inventory_hash") or ""),
    }
    if (
        report.get("provider_profile") != profile.name
        or providers != {profile.ledger_provider}
        or models != {profile.normalized_model}
        or any(not SHA256_PATTERN.fullmatch(value) for value in contract.values())
    ):
        raise LiveEvaluationError("readiness rollover generation contract is incomplete")
    return contract


def _readiness_operation_envelope(entries: list[dict[str, Any]]) -> tuple[int, float, int]:
    operation_tokens: list[int] = []
    operation_costs: list[float] = []
    for entry in entries:
        report = _load_object(ROOT / str(entry["report_path"]), "readiness operation report")
        raw_operations = report.get("operations")
        operations = raw_operations if isinstance(raw_operations, list) else [report]
        for operation in operations:
            if not isinstance(operation, dict):
                raise LiveEvaluationError("readiness operation envelope is malformed")
            raw_ledger = operation.get("provider_call_ledger")
            ledger = raw_ledger if isinstance(raw_ledger, dict) else {}
            tokens = 0
            cost = 0.0
            for raw_row in ledger.values():
                if not isinstance(raw_row, dict) or int(raw_row.get("call_count") or 0) <= 0:
                    continue
                tokens += int(raw_row.get("prompt_tokens") or 0) + int(
                    raw_row.get("completion_tokens") or 0
                )
                cost += float(raw_row.get("cost_usd") or 0.0)
            if tokens <= 0 or cost <= 0:
                raise LiveEvaluationError("readiness operation envelope has missing usage")
            operation_tokens.append(tokens)
            operation_costs.append(cost)
    if not operation_tokens:
        raise LiveEvaluationError("readiness operation envelope is empty")
    return max(operation_tokens), max(operation_costs), len(operation_tokens)


def _validate_readiness_entry(
    raw: Any,
    *,
    label: str,
    expected_kind: str,
    app_commit: str,
    runtime_image_id: str,
    profile: ProviderProfile,
    allow_identity_rollover: bool = False,
    expected_generation_contract: dict[str, str] | None = None,
    require_current_runtime: bool = False,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LiveEvaluationError(f"readiness {label} entry is missing")
    row = {str(key): value for key, value in raw.items()}
    run_id = str(row.get("run_id") or "")
    report_path = str(row.get("report_path") or "")
    report_hash = str(row.get("report_sha256") or "")
    if (
        not RUN_ID_PATTERN.fullmatch(run_id)
        or row.get("status") != "PASS"
        or row.get("usage_complete") is not True
        or row.get("output_reviewed_by_codex") is not True
        or not report_path
        or not SHA256_PATTERN.fullmatch(report_hash)
    ):
        raise LiveEvaluationError(f"readiness {label} entry is incomplete")
    candidate = (ROOT / report_path).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise LiveEvaluationError(f"readiness {label} report path is unsafe") from exc
    if not candidate.is_file() or _sha256(candidate) != report_hash:
        raise LiveEvaluationError(f"readiness {label} report hash does not match")
    source_report = _load_object(candidate, f"readiness {label} report")
    marker = _load_object(RUN_STATE_DIR / f"{run_id}.json", f"readiness {label} run marker")
    try:
        total_tokens = int(row.get("total_tokens") or 0)
        total_cost = float(row.get("total_cost_usd") or 0.0)
        max_tokens = int(row.get("max_tokens") or 0)
        max_cost = float(row.get("max_cost_usd") or 0.0)
        marker_total_tokens = int(marker.get("total_tokens") or 0)
        marker_total_cost = float(marker.get("total_cost_usd") or 0.0)
        marker_max_tokens = int(marker.get("max_tokens") or 0)
        marker_max_cost = float(marker.get("max_cost_usd") or 0.0)
    except (TypeError, ValueError) as exc:
        raise LiveEvaluationError(f"readiness {label} accounting is malformed") from exc
    if allow_identity_rollover:
        source_commit = str(row.get("source_app_commit") or "")
        source_image = str(row.get("source_runtime_image_id") or "")
        identity_valid = (
            COMMIT_PATTERN.fullmatch(source_commit) is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", source_image) is not None
            and marker.get("app_commit") == source_commit
            and marker.get("runtime_image_id") == source_image
            and (not require_current_runtime or source_image == runtime_image_id)
        )
        if (
            expected_generation_contract is None
            or _readiness_generation_contract(
                source_report,
                profile=profile,
            )
            != expected_generation_contract
        ):
            raise LiveEvaluationError("readiness rollover generation contract differs")
    else:
        identity_valid = (
            marker.get("app_commit") == app_commit
            and marker.get("runtime_image_id") == runtime_image_id
        )
    if (
        marker.get("run_id") != run_id
        or marker.get("kind") != expected_kind
        or marker.get("status") != "completed"
        or marker.get("usage_complete") is not True
        or not identity_valid
        or (
            profile.functional_latency_gap_allowed
            and (
                marker.get("provider_profile") != profile.name
                or marker.get("provider") != profile.ledger_provider
                or normalize_model(str(marker.get("model") or "")) != profile.normalized_model
            )
        )
        or marker.get("report_sha256") != report_hash
        or marker_total_tokens != total_tokens
        or not math.isclose(
            marker_total_cost,
            total_cost,
            rel_tol=0,
            abs_tol=1e-8,
        )
        or marker_max_tokens != max_tokens
        or not math.isclose(
            marker_max_cost,
            max_cost,
            rel_tol=0,
            abs_tol=1e-8,
        )
        or total_tokens <= 0
        or total_cost <= 0
        or max_tokens < total_tokens
        or max_cost < total_cost
        or source_report.get("ok") is not True
    ):
        raise LiveEvaluationError(f"readiness {label} run is not completed and accounted")
    raw_checks = source_report.get("checks")
    functional_green = (
        source_report.get("functional_quality_passed") is True
        if profile.functional_latency_gap_allowed
        else isinstance(raw_checks, dict) and bool(raw_checks) and all(raw_checks.values())
    )
    if not functional_green:
        raise LiveEvaluationError(f"readiness {label} engineering checks are not green")
    if expected_kind == "gate0_live_probe":
        if source_report.get("run_id") != run_id:
            raise LiveEvaluationError(f"readiness {label} report identity does not match")
        if profile.functional_latency_gap_allowed and source_report.get("case_id") != "B01":
            raise LiveEvaluationError("readiness GLM smoke must be the B01 capability case")
    else:
        case_id = str(row.get("case_id") or "")
        if (
            case_id not in set(profile.pilot_case_ids)
            or marker.get("case_id") != case_id
            or source_report.get("case_id") != case_id
            or source_report.get("evaluation_id") != run_id
        ):
            raise LiveEvaluationError(f"readiness {label} case identity does not match")
    return row


def validate_readiness_manifest(
    path: pathlib.Path,
    *,
    commit: str,
    contract_hash: str,
    basket_hash: str,
    runtime_image_id: str,
    profile: ProviderProfile | None = None,
) -> dict[str, Any]:
    selected = profile or provider_profile(CANONICAL_PROFILE_NAME)
    report = _load_object(path, "live readiness manifest")
    raw_rollover = report.get("identity_rollover")
    if raw_rollover is not None and not isinstance(raw_rollover, dict):
        raise LiveEvaluationError("readiness identity rollover is malformed")
    allow_identity_rollover = isinstance(raw_rollover, dict)
    rollover = raw_rollover if isinstance(raw_rollover, dict) else {}
    expected_generation_contract: dict[str, str] | None = None
    if allow_identity_rollover:
        raw_generation_contract = rollover.get("generation_contract")
        expected_generation_contract = (
            {str(key): str(value) for key, value in raw_generation_contract.items()}
            if isinstance(raw_generation_contract, dict)
            else None
        )
        if (
            not selected.functional_latency_gap_allowed
            or rollover.get("policy") != "owner_authorized_current_b01_historical_green_pilots"
            or rollover.get("authority_sha256") != _sha256(HANDOFF_PATH)
            or rollover.get("current_smoke_required") is not True
            or expected_generation_contract is None
            or set(expected_generation_contract)
            != {"prompt_hash", "skill_content_hash", "tool_inventory_hash"}
            or any(
                not SHA256_PATTERN.fullmatch(value)
                for value in expected_generation_contract.values()
            )
        ):
            raise LiveEvaluationError("readiness identity rollover is unauthorized or incomplete")
    if (
        report.get("schema_version") != 1
        or report.get("status") != "PASS"
        or report.get("provider_profile", CANONICAL_PROFILE_NAME) != selected.name
        or report.get("app_commit") != commit
        or report.get("runtime_contract_hash") != contract_hash
        or report.get("basket_hash") != basket_hash
        or report.get("runtime_image_id") != runtime_image_id
    ):
        raise LiveEvaluationError("live readiness manifest is stale or invalid")
    if selected.functional_latency_gap_allowed:
        warmup = None
        smoke = _validate_readiness_entry(
            report.get("smoke"),
            label="smoke",
            expected_kind="gate0_live_probe",
            app_commit=commit,
            runtime_image_id=runtime_image_id,
            profile=selected,
            allow_identity_rollover=allow_identity_rollover,
            expected_generation_contract=expected_generation_contract,
            require_current_runtime=allow_identity_rollover,
        )
        if allow_identity_rollover and rollover.get("current_smoke_id") != smoke.get("run_id"):
            raise LiveEvaluationError("readiness rollover current B01 identity differs")
    else:
        warmup = _validate_readiness_entry(
            report.get("warmup"),
            label="warmup",
            expected_kind="gate0_live_probe",
            app_commit=commit,
            runtime_image_id=runtime_image_id,
            profile=selected,
        )
        if warmup.get("excluded_from_metrics") is not True:
            raise LiveEvaluationError("readiness warmup is not explicitly excluded")
        smoke = _validate_readiness_entry(
            report.get("smoke"),
            label="smoke",
            expected_kind="gate2_live_campaign",
            app_commit=commit,
            runtime_image_id=runtime_image_id,
            profile=selected,
        )
    raw_pilots = report.get("pilots")
    minimum_pilots, maximum_pilots = (
        (3, len(selected.pilot_case_ids)) if selected.functional_latency_gap_allowed else (2, 3)
    )
    if not isinstance(raw_pilots, list) or not minimum_pilots <= len(raw_pilots) <= maximum_pilots:
        raise LiveEvaluationError("readiness has an invalid representative pilot count")
    pilots = [
        _validate_readiness_entry(
            item,
            label=f"pilot-{index}",
            expected_kind="gate2_live_campaign",
            app_commit=commit,
            runtime_image_id=runtime_image_id,
            profile=selected,
            allow_identity_rollover=allow_identity_rollover,
            expected_generation_contract=expected_generation_contract,
        )
        for index, item in enumerate(raw_pilots, start=1)
    ]
    run_ids = [
        *([str(warmup["run_id"])] if warmup is not None else []),
        str(smoke["run_id"]),
        *[str(item["run_id"]) for item in pilots],
    ]
    if len(set(run_ids)) != len(run_ids):
        raise LiveEvaluationError("readiness source runs must be distinct")
    if len({str(item.get("case_id") or "") for item in pilots}) != len(pilots):
        raise LiveEvaluationError("readiness pilot cases must be distinct")
    representative_runs = [smoke, *pilots]
    raw_recovery_projection = report.get("recovery_projection")
    recovery_projection = (
        raw_recovery_projection if isinstance(raw_recovery_projection, dict) else None
    )
    if recovery_projection is not None:
        if (
            not allow_identity_rollover
            or recovery_projection.get("policy")
            != "owner_authorized_post_quarantine_empirical_envelope"
            or recovery_projection.get("authority_sha256") != _sha256(HANDOFF_PATH)
            or not RUN_ID_PATTERN.fullmatch(
                str(recovery_projection.get("quarantined_run_id") or "")
            )
            or recovery_projection.get("main_loop_max_tokens") != selected.main_loop_max_tokens
        ):
            raise LiveEvaluationError("readiness recovery projection is unauthorized or incomplete")
        (
            expected_operation_tokens,
            expected_operation_cost,
            expected_observed_operations,
        ) = _readiness_operation_envelope(representative_runs)
        if recovery_projection.get("observed_operation_count") != expected_observed_operations:
            raise LiveEvaluationError("readiness recovery operation count differs")
        expected_basis = "largest_observed_hash_equal_operation"
        expected_includes_maximum_output = False
    else:
        expected_operation_tokens = max(int(item["max_tokens"]) for item in representative_runs)
        expected_operation_cost = max(float(item["max_cost_usd"]) for item in representative_runs)
        expected_basis = "largest_selected_smoke_or_pilot_run_cap"
        expected_includes_maximum_output = True
    raw_projection = report.get("projection")
    projection = raw_projection if isinstance(raw_projection, dict) else {}
    try:
        operations = int(projection.get("paid_operation_count") or 0)
        tokens = int(projection.get("projected_tokens") or 0)
        cost = float(projection.get("projected_cost_usd") or 0.0)
        multiplier = float(projection.get("safety_multiplier") or 0.0)
        per_operation_tokens = int(projection.get("per_operation_token_cap") or 0)
        per_operation_cost = float(projection.get("per_operation_cost_cap_usd") or 0.0)
    except (TypeError, ValueError) as exc:
        raise LiveEvaluationError("readiness projection is malformed") from exc
    if (
        operations != TOTAL_PAID_OPERATION_WEIGHT
        or tokens <= 0
        or cost <= 0
        or multiplier < 1.2
        or per_operation_tokens <= 0
        or per_operation_cost <= 0
        or per_operation_tokens != expected_operation_tokens
        or not math.isclose(
            per_operation_cost,
            expected_operation_cost,
            rel_tol=0,
            abs_tol=1e-8,
        )
        or tokens != math.ceil(per_operation_tokens * operations * multiplier)
        or not math.isclose(
            cost,
            round(per_operation_cost * operations * multiplier, 8),
            rel_tol=0,
            abs_tol=1e-8,
        )
        or projection.get("basis") != expected_basis
        or projection.get("includes_maximum_output") is not expected_includes_maximum_output
        or (
            recovery_projection is not None
            and projection.get("includes_bounded_output_headroom") is not True
        )
        or projection.get("includes_configured_retries") is not True
        or projection.get("includes_safety_and_post_task") is not True
    ):
        raise LiveEvaluationError("readiness projection is not conservative and complete")
    return report


def execute_app_preflight() -> dict[str, Any]:
    process = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "app",
            "python",
            "-m",
            "apps.api.app.live_evaluation_transport",
            "--preflight",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        raw = json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise LiveEvaluationError("app live-evaluation preflight returned no safe report") from exc
    if not isinstance(raw, dict):
        raise LiveEvaluationError("app live-evaluation preflight report is invalid")
    report = {str(key): value for key, value in raw.items()}
    if process.returncode != 0:
        raise LiveEvaluationError("app live-evaluation preflight failed")
    return report


def _validate_runtime_report(
    report: dict[str, Any],
    *,
    request: RunRequest,
    contract: dict[str, Any],
    image_id: str,
) -> None:
    selected = provider_profile(request.profile_name)
    raw_budget = report.get("runtime_budget")
    budget = raw_budget if isinstance(raw_budget, dict) else {}
    raw_admission = report.get("admission")
    admission = raw_admission if isinstance(raw_admission, dict) else {}
    raw_skill = contract.get("skill")
    skill = raw_skill if isinstance(raw_skill, dict) else {}
    raw_tools = contract.get("tools")
    tools = raw_tools if isinstance(raw_tools, dict) else {}
    if (
        report.get("ok") is not True
        or report.get("provider_profile") != selected.name
        or report.get("provider") != selected.ledger_provider
        or normalize_model(str(report.get("model") or "")) != selected.normalized_model
        or report.get("task_timeout_seconds") != selected.effective_task_timeout_seconds
        or report.get("terminal_deadline_seconds") != selected.effective_terminal_deadline_seconds
        or report.get("require_post_task_summary") is not selected.require_post_task_summary
        or report.get("provider_calls") != 0
        or report.get("active_rule_ids") != []
        or report.get("active_run_count") != 0
        or float(budget.get("remaining_usd") or 0.0) < request.max_cost_usd
        or admission.get("runtime_image_id") != image_id
        or admission.get("prompt_hash") != skill.get("prompt_hash")
        or admission.get("skill_content_hash") != skill.get("skill_content_hash")
        or admission.get("tool_inventory_hash") != tools.get("inventory_hash")
    ):
        raise LiveEvaluationError("app/runtime live-evaluation preflight is not green")


def validate_preflight(
    request: RunRequest,
    *,
    environment: Mapping[str, str] = os.environ,
    readiness_path: pathlib.Path = READINESS_PATH,
    app_preflight: PreflightExecutor = execute_app_preflight,
) -> PreflightContext:
    run_preflight("bootstrap")
    try:
        selected = requested_provider_profile(environment)
    except ProviderProfileError as exc:
        raise LiveEvaluationError(str(exc)) from exc
    commit, branch = git_identity(required_branch=selected.required_branch)
    contract_hash = _sha256(CONTRACT_LOCK_PATH)
    basket_hash = _sha256(EXPECTED_PATH)
    contract = _load_object(CONTRACT_LOCK_PATH, "runtime contract lock")
    validate_strict_contract(contract)
    raw_runtime = contract.get("runtime")
    runtime = raw_runtime if isinstance(raw_runtime, dict) else {}
    expected_image_id = str(runtime.get("image_id") or "")
    if not expected_image_id.startswith("sha256:"):
        raise LiveEvaluationError("runtime contract image identity is invalid")
    readiness = validate_readiness_manifest(
        readiness_path,
        commit=commit,
        contract_hash=contract_hash,
        basket_hash=basket_hash,
        runtime_image_id=expected_image_id,
        profile=selected,
    )
    projection = readiness["projection"]
    effective_request = dataclasses.replace(
        request,
        projected_tokens=int(projection["projected_tokens"]),
        projected_cost_usd=float(projection["projected_cost_usd"]),
    )
    ledger = read_usage_ledger(DEFAULT_USAGE_LEDGER)
    try:
        night = validate_paid_run_budget(
            effective_request,
            run_state_dir=RUN_STATE_DIR,
            run_kind="full_live_evaluation",
            environment=dict(environment),
        )
    except BudgetPolicyError as exc:
        raise LiveEvaluationError(str(exc)) from exc
    if any(record.run_id == effective_request.run_id for record in ledger):
        raise LiveEvaluationError("full evaluation run id already appears in usage ledger")
    if (RUN_STATE_DIR / f"{effective_request.run_id}.json").exists():
        raise LiveEvaluationError("full evaluation run id was already used")
    if (LIVE_ROOT / effective_request.run_id).exists():
        raise LiveEvaluationError("full evaluation evidence directory already exists")
    validate_retry_linkage(environment)
    image_id = verify_running_profile()
    if image_id != expected_image_id:
        raise LiveEvaluationError("running image differs from readiness and contract")
    runtime_report = app_preflight()
    _validate_runtime_report(
        runtime_report,
        request=effective_request,
        contract=contract,
        image_id=image_id,
    )
    return PreflightContext(
        request=effective_request,
        commit=commit,
        branch=branch,
        contract_hash=contract_hash,
        basket_hash=basket_hash,
        image_id=image_id,
        runtime_report=runtime_report,
        readiness=readiness,
        night=night,
    )


def reserve_run(
    context: PreflightContext,
    *,
    environment: Mapping[str, str] = os.environ,
) -> tuple[pathlib.Path, pathlib.Path]:
    request = context.request
    RUN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_ROOT.mkdir(parents=True, exist_ok=True)
    marker = RUN_STATE_DIR / f"{request.run_id}.json"
    source_root = LIVE_ROOT / request.run_id
    previous, reason = validate_retry_linkage(environment)
    state: dict[str, Any] = {
        "schema_version": 1,
        "run_id": request.run_id,
        "kind": "full_live_evaluation",
        "status": "running",
        "provider": request.provider,
        "model": normalize_model(request.model),
        "provider_profile": request.profile_name,
        "max_tokens": request.max_tokens,
        "max_cost_usd": request.max_cost_usd,
        "projected_tokens": request.projected_tokens,
        "projected_cost_usd": request.projected_cost_usd,
        "concurrency": request.concurrency,
        "app_commit": context.commit,
        "runtime_contract_hash": context.contract_hash,
        "basket_hash": context.basket_hash,
        "runtime_image_id": context.image_id,
        "completed_case_count": 0,
        "usage_complete": False,
        "account_remaining": "unknown",
        "started_at": datetime.now(UTC).isoformat(),
    }
    if context.night is not None:
        state.update(night_marker_fields(context.night))
    if previous:
        state["retry_of"] = previous
        state["retry_reason"] = reason
    try:
        with marker.open("x", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        source_root.mkdir()
    except FileExistsError as exc:
        raise LiveEvaluationError("full evaluation run or evidence id was already used") from exc
    _atomic_json(
        source_root / "attempt.json",
        {
            **state,
            "case_execution_order": [item.case_id for item in CASE_PLAN],
            "paid_operation_weights": {
                item.case_id: item.paid_operation_weight for item in CASE_PLAN
            },
            "readiness_manifest_sha256": _sha256(READINESS_PATH),
        },
    )
    return marker, source_root


def execute_case_transport(
    case_id: str,
    evaluation_id: str,
    rule_version_id: str,
    active_rules_version: str,
) -> tuple[int, dict[str, Any]]:
    try:
        profile = requested_provider_profile(dict(os.environ))
    except ProviderProfileError as exc:
        raise LiveEvaluationError(str(exc)) from exc
    command = [
        "docker",
        "compose",
        "exec",
        "-T",
        "app",
        "python",
        "-m",
        "apps.api.app.live_evaluation_transport",
        "--case-id",
        case_id,
        "--evaluation-id",
        evaluation_id,
    ]
    if case_id == "B03":
        command.extend(
            [
                "--rule-version-id",
                rule_version_id,
                "--active-rules-version",
                active_rules_version,
            ]
        )
    weight = next(item.paid_operation_weight for item in CASE_PLAN if item.case_id == case_id)
    process = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=max(30, weight * (profile.effective_terminal_deadline_seconds + 30)),
        check=False,
    )
    try:
        raw = json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise LiveEvaluationError(f"{case_id} transport returned no safe report") from exc
    if not isinstance(raw, dict):
        raise LiveEvaluationError(f"{case_id} transport report is invalid")
    return process.returncode, {str(key): value for key, value in raw.items()}


def copy_demo_export(container_path: str, destination: pathlib.Path) -> None:
    if not container_path.startswith("/data/artifacts/exports/") or not container_path.endswith(
        ".zip"
    ):
        raise LiveEvaluationError("demo export container path is unsafe")
    destination.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.run(
        ["docker", "compose", "cp", f"app:{container_path}", str(destination)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0 or not destination.is_file() or not zipfile.is_zipfile(destination):
        raise LiveEvaluationError("demo campaign export could not be preserved")


def cleanup_rule_transport(rule_version_id: str, active_rules_version: str) -> dict[str, Any]:
    process = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "app",
            "python",
            "-m",
            "apps.api.app.live_evaluation_transport",
            "--cleanup-rule-version-id",
            rule_version_id,
            "--active-rules-version",
            active_rules_version,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        raw = json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise LiveEvaluationError("rule cleanup returned no safe report") from exc
    if process.returncode != 0 or not isinstance(raw, dict) or raw.get("ok") is not True:
        raise LiveEvaluationError("rule cleanup failed")
    return {str(key): value for key, value in raw.items()}


def _case_usage_rows(
    evaluation_id: str,
    report: dict[str, Any],
    *,
    expected_provider: str = "openai",
    expected_model: str = "gpt-5.4-mini",
    require_complete: bool = True,
) -> list[dict[str, Any]]:
    raw_metrics = report.get("metrics")
    metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    raw_usage = metrics.get("usage_by_category")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    ledger: dict[str, Any] = {}
    for category, raw_row in usage.items():
        if not isinstance(raw_row, dict):
            raise LiveEvaluationError("case usage category row is malformed")
        ledger[str(category)] = {
            "call_count": raw_row.get("calls"),
            "prompt_tokens": raw_row.get("prompt_tokens"),
            "completion_tokens": raw_row.get("completion_tokens"),
            "cached_tokens": raw_row.get("cached_tokens"),
            "cache_write_tokens": raw_row.get("cache_write_tokens"),
            "cost_usd": raw_row.get("cost_usd"),
            "models": raw_row.get("models"),
            "providers": raw_row.get("providers"),
        }
    if require_complete and metrics.get("usage_complete") is not True:
        raise LiveEvaluationError("case provider usage is incomplete")
    if not usage:
        if require_complete:
            raise LiveEvaluationError("case returned no provider usage")
        return []
    calls = sum(int(row.get("calls") or 0) for row in usage.values() if isinstance(row, dict))
    if not require_complete and calls == metrics.get("provider_calls") == 0:
        return []
    rows = usage_rows_from_report(
        evaluation_id,
        {"provider_call_ledger": ledger},
        expected_provider=expected_provider,
        expected_model=expected_model,
    )
    if calls != metrics.get("provider_calls") or not rows:
        raise LiveEvaluationError("case provider usage totals do not match")
    return rows


def _provider_accounting(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    raw_operations = report.get("operations")
    operations = raw_operations if isinstance(raw_operations, list) else [report]
    orphan_requests: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    seen_generations: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        raw = operation.get("provider_accounting")
        accounting = raw if isinstance(raw, dict) else {}
        for candidate in accounting.get("orphan_requests") or []:
            if not isinstance(candidate, dict):
                continue
            generation_id = str(candidate.get("generation_id") or "")
            if generation_id and generation_id not in seen_generations:
                seen_generations.add(generation_id)
                orphan_requests.append({str(key): value for key, value in candidate.items()})
        for candidate in accounting.get("pre_generation_anomalies") or []:
            if isinstance(candidate, dict):
                anomalies.append({str(key): value for key, value in candidate.items()})
    return {
        "orphan_requests": orphan_requests,
        "pre_generation_anomalies": anomalies,
    }


def _bounded_estimates(
    budget: NightBudget,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    estimates: list[dict[str, Any]] = []
    for request in requests:
        prompt_tokens = int(request.get("estimated_prompt_tokens") or 0)
        max_output = int(request.get("configured_max_output_tokens") or 0)
        estimated_tokens, estimated_cost = bounded_request_estimate(
            budget,
            estimated_prompt_tokens=prompt_tokens,
            configured_max_output_tokens=max_output,
        )
        estimates.append(
            {
                "generation_id": str(request.get("generation_id") or ""),
                "provider_call_id": str(request.get("provider_call_id") or ""),
                "category": str(request.get("category") or "unattributed"),
                "status_code": int(request.get("status_code") or 0),
                "estimated_prompt_tokens": prompt_tokens,
                "configured_max_output_tokens": max_output,
                "estimated_tokens": estimated_tokens,
                "estimated_cost_usd": estimated_cost,
                "prompt_price_usd_per_million": budget.prompt_price_usd_per_million,
                "completion_price_usd_per_million": (budget.completion_price_usd_per_million),
                "safety_multiplier": budget.estimate_safety_multiplier,
                "prompt_estimation_method": str(request.get("prompt_estimation_method") or ""),
            }
        )
    return estimates


def _failure_classes(
    case_id: str,
    report: dict[str, Any],
    outcome: dict[str, Any],
    accounting: dict[str, list[dict[str, Any]]],
) -> list[str]:
    classes: set[str] = set()
    for anomaly in accounting["pre_generation_anomalies"]:
        status = int(anomaly.get("status_code") or 0)
        if status == 429:
            classes.add("provider.http_429")
        elif 500 <= status <= 599:
            classes.add("provider.http_5xx")
        else:
            classes.add("provider.pre_generation_anomaly")
    if accounting["orphan_requests"]:
        classes.add("provider.orphan_generation")
    for operation in report.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        error_type = str(operation.get("error_type") or "").lower()
        if "timeout" in error_type:
            classes.add("provider.timeout")
    if outcome.get("passed") is not True and not classes:
        classes.add(f"case.{case_id.lower()}.evaluation")
    return sorted(classes)


def _usage_records(rows: list[dict[str, Any]]) -> list[UsageRecord]:
    return [
        UsageRecord(
            ts=datetime.fromisoformat(str(row["ts"])),
            run_id=str(row["run_id"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            category=str(row["category"]),
            prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]),
            cost_usd=float(row["cost_usd"]),
        )
        for row in rows
    ]


def _runtime_remaining(report: dict[str, Any]) -> float:
    raw_operations = report.get("operations")
    operations = raw_operations if isinstance(raw_operations, list) else []
    if not operations:
        return math.inf
    values: list[float] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        raw_budget = operation.get("runtime_budget")
        if not isinstance(raw_budget, dict):
            continue
        value = raw_budget.get("remaining_usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            values.append(float(value))
    return values[-1] if values else math.inf


def _remaining_projection(
    request: RunRequest,
    *,
    remaining_weight: int,
) -> tuple[int, float]:
    token_projection = math.ceil(
        request.projected_tokens * remaining_weight / TOTAL_PAID_OPERATION_WEIGHT
    )
    cost_projection = request.projected_cost_usd * remaining_weight / TOTAL_PAID_OPERATION_WEIGHT
    return max(1, token_projection), max(0.00000001, cost_projection)


def _case_inputs() -> dict[str, dict[str, Any]]:
    fixture = _load_object(
        ROOT / "data" / "synthetic" / "cases" / "gate1.json",
        "business case fixture",
    )
    raw = fixture.get("cases")
    if not isinstance(raw, list):
        raise LiveEvaluationError("business case input fixture is malformed")
    return {
        str(item["case_id"]): dict(item)
        for item in raw
        if isinstance(item, dict) and item.get("case_id")
    }


def _collect_learning(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    learning: dict[str, Any] = {}
    for outcome in outcomes:
        raw = outcome.get("learning")
        if isinstance(raw, dict):
            learning.update(raw)
    return learning


def _stability(outcomes: list[dict[str, Any]]) -> dict[str, int]:
    operations = [
        operation
        for outcome in outcomes
        for operation in (outcome.get("operations") or [])
        if isinstance(operation, dict)
    ]
    unsupported = 0
    for outcome in outcomes:
        package = outcome.get("package")
        quality = package.get("quality_report") if isinstance(package, dict) else None
        if isinstance(quality, dict) and quality.get("approvable") is True:
            unsupported += sum(
                isinstance(item, dict) and item.get("check_id") == "QA18"
                for item in quality.get("findings") or []
            )
    return {
        "crash_count": sum(operation.get("error_type") is not None for operation in operations),
        "stuck_run_count": sum(
            (operation.get("checks") or {}).get("worker_released") is not True
            for operation in operations
        ),
        "timeout_over_30s_count": sum(
            int((operation.get("latency_ms") or {}).get("user_visible_terminal") or 0) >= 30_000
            for operation in operations
        ),
        "unsupported_approved_claim_count": unsupported,
        "prompt_injection_success_count": sum(
            outcome.get("case_id") in {"B01", "B14"}
            and outcome.get("assertions", {}).get("injection_ignored") is not True
            for outcome in outcomes
        ),
        "blocker_approval_success_count": 0,
        "duplicate_paid_generation_count": sum(
            int((operation.get("run") or {}).get("physical_attempt_count") or 1) > 1
            for operation in operations
        ),
    }


def _latency_summary(outcomes: list[dict[str, Any]]) -> dict[str, int | bool]:
    values = sorted(
        int((operation.get("latency_ms") or {}).get("user_visible_terminal") or 0)
        for outcome in outcomes
        for operation in (outcome.get("operations") or [])
        if isinstance(operation, dict)
        and int((operation.get("latency_ms") or {}).get("user_visible_terminal") or 0) > 0
    )
    if not values:
        return {"p50_ms": 0, "p95_ms": 0, "max_ms": 0, "latency_gap": False}

    def percentile(percent: float) -> int:
        index = max(0, math.ceil(len(values) * percent) - 1)
        return values[index]

    return {
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": values[-1],
        "latency_gap": any(value >= 30_000 for value in values),
    }


def _mode_counts(outcomes: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for outcome in outcomes:
        mode = str(outcome.get("mode") or "unknown")
        result[mode] = result.get(mode, 0) + 1
    return result


def _checksums(root: pathlib.Path) -> None:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name in {
            "checksums.sha256",
            "FROZEN.json",
            "FAILED.json",
            "FUNCTIONAL_IMMUTABLE.json",
        }:
            continue
        rows.append(f"{_sha256(path)}  {path.relative_to(root).as_posix()}\n")
    (root / "checksums.sha256").write_text("".join(rows), encoding="utf-8")


def _finish_marker(
    marker: pathlib.Path,
    *,
    status: str,
    completed_case_count: int,
    usage_complete: bool,
    usage_records: list[UsageRecord],
    report_hash: str,
    extra_fields: Mapping[str, Any] | None = None,
) -> None:
    state = _load_object(marker, "full evaluation marker")
    state.update(
        {
            "status": status,
            "completed_case_count": completed_case_count,
            "usage_complete": usage_complete,
            "total_tokens": sum(record.total_tokens for record in usage_records),
            "total_cost_usd": round(sum(record.cost_usd for record in usage_records), 8),
            "report_sha256": report_hash,
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )
    if extra_fields:
        state.update(extra_fields)
    _atomic_json(marker, state)


def _dependency_quarantine_outcome(
    case_id: str, fixture: dict[str, Any], dependency: str
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "passed": False,
        "mode": "not_executed_dependency_quarantine",
        "initial_state": "NOT_EXECUTED",
        "terminal_state": "NOT_EXECUTED",
        "assertions": {},
        "operations": [],
        "metrics": {"provider_calls": 0, "usage_complete": True},
        "learning": {},
        "input": fixture,
        "quarantine": {"dependency": dependency, "revisit_required": True},
    }


def run_live_evaluation(
    context: PreflightContext,
    *,
    environment: Mapping[str, str] = os.environ,
    executor: CaseExecutor = execute_case_transport,
    export_copier: ExportCopier = copy_demo_export,
    rule_cleanup: RuleCleanup = cleanup_rule_transport,
    usage_appender: UsageAppender = append_usage,
    metadata_poller: MetadataPoller = poll_orphan_metadata,
) -> dict[str, Any]:
    marker, source_root = reserve_run(context, environment=environment)
    request = context.request
    outcomes: list[dict[str, Any]] = []
    recorded_usage: list[UsageRecord] = []
    release_blockers: list[str] = []
    rule_version_id = ""
    active_rules_version = ""
    rule_active = False
    accounting_complete = True
    current_case_id = ""
    inputs = _case_inputs()
    additional_policy = bool(context.night and context.night.additional_authority)
    orphan_requests: list[dict[str, Any]] = []
    pre_generation_anomalies: list[dict[str, Any]] = []
    failure_classes: set[str] = set()
    quarantined_cases: set[str] = set()
    bounded_estimates: list[dict[str, Any]] = []
    metadata_poll: dict[str, Any] | None = None
    accounting_unclassified = False
    current_case_accounted = False
    try:
        for index, plan in enumerate(CASE_PLAN):
            current_case_id = plan.case_id
            current_case_accounted = plan.paid_operation_weight == 0
            if plan.case_id == "B03" and (not rule_version_id or not active_rules_version):
                if not additional_policy:
                    raise LiveEvaluationError("B03 requires completed B01 rule linkage")
                outcome = _dependency_quarantine_outcome(
                    plan.case_id,
                    inputs[plan.case_id],
                    "B01_rule_linkage",
                )
                outcomes.append(outcome)
                quarantined_cases.add(plan.case_id)
                failure_classes.add("case.b03.dependency")
                release_blockers.append("B03_DEPENDENCY_QUARANTINED")
                _atomic_json(source_root / "cases" / plan.case_id / "outcome.json", outcome)
                continue
            returncode, raw = executor(
                plan.case_id,
                request.run_id,
                rule_version_id,
                active_rules_version,
            )
            _atomic_json(source_root / "cases" / plan.case_id / "transport.json", raw)
            if raw.get("evaluation_id") != request.run_id or raw.get("case_id") != plan.case_id:
                raise LiveEvaluationError(f"{plan.case_id} transport identity does not match")
            outcome = evaluate_live_case_report(raw)
            outcome["input"] = inputs[plan.case_id]
            outcomes.append(outcome)
            _atomic_json(source_root / "cases" / plan.case_id / "outcome.json", outcome)

            if plan.paid_operation_weight > 0:
                raw_metrics = raw.get("metrics")
                metrics: dict[str, Any] = raw_metrics if isinstance(raw_metrics, dict) else {}
                case_usage_complete = metrics.get("usage_complete") is True
                if not case_usage_complete and not additional_policy:
                    accounting_complete = False
                    raise LiveEvaluationError("case provider usage is incomplete")
                rows = _case_usage_rows(
                    request.run_id,
                    raw,
                    expected_provider=request.provider,
                    expected_model=normalize_model(request.model),
                    require_complete=not additional_policy,
                )
                if rows:
                    usage_appender(rows)
                    records = _usage_records(rows)
                    recorded_usage.extend(records)
                if not case_usage_complete:
                    accounting = _provider_accounting(raw)
                    if not any(accounting.values()):
                        accounting_complete = False
                        accounting_unclassified = True
                        raise LiveEvaluationError(
                            "incomplete usage has no safe physical-request disposition"
                        )
                    accounting_complete = False
                    orphan_requests.extend(accounting["orphan_requests"])
                    pre_generation_anomalies.extend(accounting["pre_generation_anomalies"])
                    failure_classes.update(_failure_classes(plan.case_id, raw, outcome, accounting))
                    quarantined_cases.add(plan.case_id)
                current_case_accounted = True

            if plan.case_id == "B01":
                raw_learning = raw.get("learning")
                learning = raw_learning if isinstance(raw_learning, dict) else {}
                raw_rule = learning.get("rule_approval")
                rule = raw_rule if isinstance(raw_rule, dict) else {}
                rule_version_id = str(rule.get("rule_version_id") or "")
                active_rules_version = str(rule.get("rules_version") or "")
                export_path = str(learning.get("campaign_export_container_path") or "")
                if not rule_version_id or not active_rules_version or not export_path:
                    if not additional_policy:
                        raise LiveEvaluationError("B01 learning linkage is incomplete")
                    quarantined_cases.add("B01")
                    failure_classes.add("case.b01.learning_linkage")
                    release_blockers.append("B01_LEARNING_LINKAGE_QUARANTINED")
                else:
                    rule_active = True
                    export_copier(
                        export_path,
                        source_root / "demo-case" / "campaign-export.zip",
                    )
            elif plan.case_id == "B03":
                raw_learning = raw.get("learning")
                learning = raw_learning if isinstance(raw_learning, dict) else {}
                raw_rollback = learning.get("rollback")
                rollback = raw_rollback if isinstance(raw_rollback, dict) else {}
                if rollback.get("status") == "ROLLED_BACK":
                    rule_active = False

            if plan.paid_operation_weight > 0:
                if context.night is not None and orphan_requests:
                    bounded_estimates = _bounded_estimates(context.night, orphan_requests)
                bounded_tokens = sum(int(row["estimated_tokens"]) for row in bounded_estimates)
                bounded_cost = sum(float(row["estimated_cost_usd"]) for row in bounded_estimates)
                remaining_weight = sum(
                    item.paid_operation_weight for item in CASE_PLAN[index + 1 :]
                )
                if remaining_weight:
                    projected_tokens, projected_cost = _remaining_projection(
                        request,
                        remaining_weight=remaining_weight,
                    )
                    if not case_boundary_allows_next(
                        request,
                        recorded_run_usage=recorded_usage,
                        next_case_projected_tokens=projected_tokens,
                        next_case_projected_cost_usd=projected_cost,
                        usage_complete=True,
                        bounded_estimated_tokens=bounded_tokens,
                        bounded_estimated_cost_usd=bounded_cost,
                    ):
                        raise LiveEvaluationError("run cap lacks headroom for the remaining cases")
                    if _runtime_remaining(raw) < projected_cost:
                        raise LiveEvaluationError(
                            "Ouroboros budget lacks headroom for the remaining cases"
                        )

            state = _load_object(marker, "full evaluation marker")
            state["completed_case_count"] = len(outcomes)
            state["total_tokens"] = sum(record.total_tokens for record in recorded_usage)
            state["total_cost_usd"] = round(
                sum(record.cost_usd for record in recorded_usage),
                8,
            )
            state["bounded_estimated_tokens"] = sum(
                int(row["estimated_tokens"]) for row in bounded_estimates
            )
            state["bounded_estimated_cost_usd"] = round(
                sum(float(row["estimated_cost_usd"]) for row in bounded_estimates),
                8,
            )
            _atomic_json(marker, state)

            if returncode != 0 or outcome.get("passed") is not True:
                release_blockers.append(f"{plan.case_id}_FAILED")
                quarantined_cases.add(plan.case_id)
                failure_classes.update(
                    _failure_classes(
                        plan.case_id,
                        raw,
                        outcome,
                        _provider_accounting(raw),
                    )
                )
                if not additional_policy:
                    break
    except Exception as exc:
        prefix = current_case_id or "PREFLIGHT"
        release_blockers.append(f"{prefix}_{type(exc).__name__}")
        if (
            not current_case_accounted
            and current_case_id
            and next(
                (
                    item.paid_operation_weight
                    for item in CASE_PLAN
                    if item.case_id == current_case_id
                ),
                0,
            )
        ):
            accounting_unclassified = True

    unresolved_orphans = list(orphan_requests)
    if orphan_requests and additional_policy and context.night is not None:
        try:
            metadata_poll = metadata_poller(
                orphan_requests,
                context.night.metadata_poll_max_seconds,
            )
        except Exception as exc:
            metadata_poll = {
                "schema_version": 1,
                "status": "incomplete",
                "poll_max_seconds": context.night.metadata_poll_max_seconds,
                "elapsed_seconds": 0,
                "requested_generation_ids": [
                    str(row.get("generation_id") or "") for row in orphan_requests
                ],
                "resolved_generation_ids": [],
                "unresolved_generation_ids": [
                    str(row.get("generation_id") or "") for row in orphan_requests
                ],
                "attempts": [],
                "results": [],
                "error_type": type(exc).__name__,
            }
        _atomic_json(source_root / "generation-metadata-poll.json", metadata_poll)
        try:
            recovered_rows = metadata_usage_rows(
                request.run_id,
                orphan_requests,
                metadata_poll,
                expected_model=request.model,
            )
        except Exception as exc:
            recovered_rows = []
            metadata_poll["recovery_error_type"] = type(exc).__name__
            _atomic_json(source_root / "generation-metadata-poll.json", metadata_poll)
        if recovered_rows:
            usage_appender(recovered_rows)
            recorded_usage.extend(_usage_records(recovered_rows))
        unresolved_ids = {
            str(value) for value in metadata_poll.get("unresolved_generation_ids") or []
        }
        unresolved_orphans = [
            row for row in orphan_requests if str(row.get("generation_id") or "") in unresolved_ids
        ]
        bounded_estimates = _bounded_estimates(context.night, unresolved_orphans)
    accounting_complete = not unresolved_orphans and not accounting_unclassified

    if rule_active:
        try:
            cleanup = rule_cleanup(rule_version_id, active_rules_version)
            _atomic_json(source_root / "rule-cleanup.json", cleanup)
            rule_active = False
        except Exception as exc:
            release_blockers.append(f"RULE_CLEANUP_{type(exc).__name__}")

    accounting_artifact: dict[str, Any] | None = None
    accounting_artifact_hash = ""
    metadata_poll_hash = ""
    if metadata_poll is not None:
        metadata_poll_hash = _sha256(source_root / "generation-metadata-poll.json")
    if additional_policy and (
        orphan_requests or pre_generation_anomalies or accounting_unclassified
    ):
        disposition = (
            "mixed_incomplete_usage"
            if bounded_estimates and pre_generation_anomalies
            else "orphan_request_estimate"
            if bounded_estimates
            else "pre_generation_anomaly"
            if pre_generation_anomalies
            else "metadata_recovered"
            if orphan_requests and not accounting_unclassified
            else "unclassified_incomplete_usage"
        )
        accounting_artifact = {
            "schema_version": 1,
            "evaluation_id": request.run_id,
            "policy": context.night.incomplete_usage_policy if context.night else "",
            "accounting_disposition": disposition,
            "provider_usage_complete": accounting_complete,
            "provider_ledger_mutated_by_estimate": False,
            "known_tokens": sum(record.total_tokens for record in recorded_usage),
            "known_cost_usd": round(sum(record.cost_usd for record in recorded_usage), 8),
            "pre_generation_anomalies": pre_generation_anomalies,
            "bounded_request_estimates": bounded_estimates,
            "metadata_poll_sha256": metadata_poll_hash,
            "metadata_poll_elapsed_seconds": math.ceil(
                float((metadata_poll or {}).get("elapsed_seconds") or 0.0)
            ),
            "failure_classes": sorted(failure_classes),
            "quarantined_cases": sorted(quarantined_cases),
            "evidence_eligible": False,
            "recovered_generation_ids": sorted(
                str(value) for value in (metadata_poll or {}).get("resolved_generation_ids") or []
            ),
        }
        _atomic_json(source_root / "accounting.json", accounting_artifact)
        accounting_artifact_hash = _sha256(source_root / "accounting.json")

    learning = _collect_learning(outcomes)
    stability = _stability(outcomes)
    latency = _latency_summary(outcomes)
    live_count = sum(outcome.get("mode") == "live_ouroboros" for outcome in outcomes)
    all_cases = len(outcomes) == 15
    passed_count = sum(outcome.get("passed") is True for outcome in outcomes)
    functional_quality_passed = (
        all_cases
        and passed_count == 15
        and live_count >= 10
        and next(
            (outcome.get("mode") for outcome in outcomes if outcome.get("case_id") == "B01"),
            None,
        )
        == "live_ouroboros"
        and next(
            (outcome.get("mode") for outcome in outcomes if outcome.get("case_id") == "B03"),
            None,
        )
        == "live_ouroboros"
        and all(value == 0 for name, value in stability.items() if name != "timeout_over_30s_count")
        and not release_blockers
    )
    canonical_latency_passed = bool(
        functional_quality_passed and stability["timeout_over_30s_count"] == 0
    )
    release_ok = functional_quality_passed and canonical_latency_passed
    functional_latency_gap = bool(
        functional_quality_passed
        and not canonical_latency_passed
        and provider_profile(request.profile_name).functional_latency_gap_allowed
    )
    if functional_quality_passed and not canonical_latency_passed:
        release_blockers.append("CANONICAL_LATENCY_GAP")
    release_blockers = list(dict.fromkeys(release_blockers))
    report_status = (
        "PASS"
        if release_ok
        else "FUNCTIONAL_PASS_WITH_LATENCY_GAP"
        if functional_latency_gap
        else "FAIL"
    )
    report = {
        "schema_version": 1,
        "evaluation_id": request.run_id,
        "execution_kind": "live_evaluation",
        "provider_profile": request.profile_name,
        "frozen": release_ok,
        "functional_frozen": functional_latency_gap,
        "generated_at": datetime.now(UTC).isoformat(),
        "app_commit": context.commit,
        "git_dirty": False,
        "runtime_contract_hash": context.contract_hash,
        "basket_hash": context.basket_hash,
        "rules_hash": hashlib.sha256(
            json.dumps(learning, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
        "status": report_status,
        "functional_quality_passed": functional_quality_passed,
        "canonical_latency_passed": canonical_latency_passed,
        "latency_gap": functional_latency_gap,
        "latency": latency,
        "provider_calls": sum(
            int((outcome.get("metrics") or {}).get("provider_calls") or 0) for outcome in outcomes
        ),
        "business_case_count": len(outcomes),
        "passed_case_count": passed_count,
        "expected_assertion_pass_rate": passed_count / 15,
        "live_case_count": live_count,
        "mode_counts": _mode_counts(outcomes),
        "release_targets_passed": release_ok,
        "release_blockers": release_blockers,
        "quarantined_cases": sorted(quarantined_cases),
        "failure_classes": sorted(failure_classes),
        "stability": stability,
        "cases": sorted(outcomes, key=lambda item: str(item["case_id"])),
        "learning": learning,
        "usage": {
            "total_tokens": sum(record.total_tokens for record in recorded_usage),
            "total_cost_usd": round(sum(record.cost_usd for record in recorded_usage), 8),
            "usage_complete": bool(recorded_usage) and accounting_complete,
            "bounded_estimated_tokens": sum(
                int(row["estimated_tokens"]) for row in bounded_estimates
            ),
            "bounded_estimated_cost_usd": round(
                sum(float(row["estimated_cost_usd"]) for row in bounded_estimates), 8
            ),
            "estimate_is_provider_usage": False,
            "account_remaining": "unknown",
            "run_token_cap": request.max_tokens,
            "run_cost_cap_usd": request.max_cost_usd,
            "projected_tokens": request.projected_tokens,
            "projected_cost_usd": request.projected_cost_usd,
        },
        "primary_attempt": request.run_id,
        "accounting": accounting_artifact
        or {
            "schema_version": 1,
            "provider_usage_complete": bool(recorded_usage) and accounting_complete,
            "provider_ledger_mutated_by_estimate": False,
            "pre_generation_anomalies": [],
            "bounded_request_estimates": [],
        },
        "repeats": (
            [str(environment.get("PREVIOUS_EVALUATION_ID"))]
            if environment.get("PREVIOUS_EVALUATION_ID")
            else []
        ),
        "exclusions": [],
        "case_execution_order": [item.case_id for item in CASE_PLAN],
        "preflight": {
            "runtime": context.runtime_report,
            "readiness_manifest_sha256": _sha256(READINESS_PATH),
            "projection_basis": "checksum_bound_post_smoke_and_pilot_manifest",
        },
        "synthetic": True,
        "no_send": True,
    }
    _atomic_json(source_root / "report.json", report)
    _checksums(source_root)
    report_hash = _sha256(source_root / "report.json")
    if release_ok:
        _atomic_json(
            source_root / "FROZEN.json",
            {
                "schema_version": 1,
                "evaluation_id": request.run_id,
                "status": "FROZEN",
                "report_sha256": report_hash,
                "checksums_sha256": _sha256(source_root / "checksums.sha256"),
                "frozen_at": datetime.now(UTC).isoformat(),
            },
        )
    elif functional_latency_gap:
        _atomic_json(
            source_root / "FUNCTIONAL_IMMUTABLE.json",
            {
                "schema_version": 1,
                "evaluation_id": request.run_id,
                "status": "FUNCTIONAL_IMMUTABLE",
                "canonical_release_eligible": False,
                "latency_gap": True,
                "report_sha256": report_hash,
                "checksums_sha256": _sha256(source_root / "checksums.sha256"),
                "frozen_at": datetime.now(UTC).isoformat(),
            },
        )
    else:
        _atomic_json(
            source_root / "FAILED.json",
            {
                "schema_version": 1,
                "evaluation_id": request.run_id,
                "status": "FAILED",
                "report_sha256": report_hash,
                "checksums_sha256": _sha256(source_root / "checksums.sha256"),
                "preserved_at": datetime.now(UTC).isoformat(),
            },
        )
    _finish_marker(
        marker,
        status="completed" if release_ok or functional_latency_gap else "failed",
        completed_case_count=len(outcomes),
        usage_complete=bool(recorded_usage) and accounting_complete,
        usage_records=recorded_usage,
        report_hash=report_hash,
        extra_fields={
            "known_tokens": sum(record.total_tokens for record in recorded_usage),
            "known_cost_usd": round(sum(record.cost_usd for record in recorded_usage), 8),
            "provider_usage_unknown": not (bool(recorded_usage) and accounting_complete),
            "evidence_eligible": bool(release_ok or functional_latency_gap),
            "failure_classes": sorted(failure_classes),
            "quarantined_cases": sorted(quarantined_cases),
            "bounded_estimated_tokens": sum(
                int(row["estimated_tokens"]) for row in bounded_estimates
            ),
            "bounded_estimated_cost_usd": round(
                sum(float(row["estimated_cost_usd"]) for row in bounded_estimates), 8
            ),
            **(
                {
                    "accounting_artifact_sha256": accounting_artifact_hash,
                    "accounting_disposition": accounting_artifact["accounting_disposition"],
                    "pre_generation_anomalies": pre_generation_anomalies,
                    "bounded_request_estimates": bounded_estimates,
                    "metadata_poll_sha256": metadata_poll_hash,
                    "metadata_poll_elapsed_seconds": accounting_artifact[
                        "metadata_poll_elapsed_seconds"
                    ],
                }
                if accounting_artifact is not None
                else {}
            ),
        },
    )
    return report


def _cleanup_empty_reservation(context: PreflightContext) -> None:
    marker = RUN_STATE_DIR / f"{context.request.run_id}.json"
    source_root = LIVE_ROOT / context.request.run_id
    if marker.exists() or source_root.exists():
        raise LiveEvaluationError("preflight unexpectedly created live evaluation state")


def _preflight_summary(request: RunRequest) -> str:
    return (
        "eval-live-preflight: PASS "
        f"provider={request.provider} model={normalize_model(request.model)} "
        "cases=15 paid_operations=15 concurrency=1 account_remaining=unknown"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded sequential live business evaluation")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        request = requested_run()
        context = validate_preflight(request)
        if args.check_only:
            _cleanup_empty_reservation(context)
            print(_preflight_summary(context.request))
            return 0
        report = run_live_evaluation(context)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"eval-live: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "eval-live: "
        f"{report['status']} evaluation={report['evaluation_id']} "
        f"business={report['passed_case_count']}/{report['business_case_count']} "
        f"live={report['live_case_count']} human=WAITING_FOR_OPERATOR"
    )
    return 0 if report["status"] in {"PASS", "FUNCTIONAL_PASS_WITH_LATENCY_GAP"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
