from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from provider_profiles import (
    CANONICAL_PROFILE_NAME,
    ProviderProfile,
    ProviderProfileError,
    requested_provider_profile,
)
from scripts.budget_control import RUN_ID_PATTERN
from scripts.evaluation import EXPECTED_PATH
from scripts.live_evaluation import (
    CONTRACT_LOCK_PATH,
    READINESS_PATH,
    TOTAL_PAID_OPERATION_WEIGHT,
    _readiness_generation_contract,
    _readiness_operation_envelope,
    validate_readiness_manifest,
)
from scripts.live_probe import RUN_STATE_DIR, verify_running_profile
from scripts.release_identity import frozen_git_identity

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE_PROBE_ROOT = ROOT / "runtime" / "live-probes"
LIVE_CAMPAIGN_ROOT = ROOT / "runtime" / "live-campaigns"
REQUIRED_CAMPAIGN_CHECKS = ("initial_fact_placement_exact",)


class LiveReadinessError(RuntimeError):
    pass


def _load_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveReadinessError(f"{label} is unreadable") from exc
    if not isinstance(raw, dict):
        raise LiveReadinessError(f"{label} must be a JSON object")
    return {str(key): value for key, value in raw.items()}


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_bytes(path: pathlib.Path, payload: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _required_run_id(environment: Mapping[str, str], name: str) -> str:
    run_id = str(environment.get(name) or "").strip()
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise LiveReadinessError(f"{name} must be a valid existing run id")
    return run_id


def _requested_sources(
    environment: Mapping[str, str],
) -> tuple[ProviderProfile, str | None, str, tuple[str, ...], float]:
    if environment.get("READINESS_OUTPUTS_REVIEWED_BY_CODEX", "").lower() != "true":
        raise LiveReadinessError(
            "READINESS_OUTPUTS_REVIEWED_BY_CODEX=true is required after actual output review"
        )
    try:
        profile = requested_provider_profile(environment, default=CANONICAL_PROFILE_NAME)
    except ProviderProfileError as exc:
        raise LiveReadinessError(str(exc)) from exc
    warmup = (
        None
        if profile.functional_latency_gap_allowed
        else _required_run_id(environment, "READINESS_WARMUP_ID")
    )
    smoke = _required_run_id(environment, "READINESS_SMOKE_ID")
    pilots = tuple(
        item.strip()
        for item in str(environment.get("READINESS_PILOT_IDS") or "").split(",")
        if item.strip()
    )
    minimum, maximum = (
        (3, len(profile.pilot_case_ids)) if profile.functional_latency_gap_allowed else (2, 3)
    )
    if not minimum <= len(pilots) <= maximum or any(
        not RUN_ID_PATTERN.fullmatch(item) for item in pilots
    ):
        raise LiveReadinessError("READINESS_PILOT_IDS has an invalid run count or id")
    all_ids = [smoke, *pilots, *([warmup] if warmup is not None else [])]
    if len(set(all_ids)) != len(all_ids):
        raise LiveReadinessError("readiness source run ids must be distinct")
    try:
        multiplier = float(environment.get("READINESS_SAFETY_MULTIPLIER", "1.25"))
    except ValueError as exc:
        raise LiveReadinessError("READINESS_SAFETY_MULTIPLIER must be numeric") from exc
    if not math.isfinite(multiplier) or multiplier < 1.2:
        raise LiveReadinessError("READINESS_SAFETY_MULTIPLIER must be at least 1.2")
    return profile, warmup, smoke, pilots, multiplier


def _source_entry(
    run_id: str,
    *,
    kind: str,
    app_commit: str,
    runtime_image_id: str,
    root: pathlib.Path,
    run_state_dir: pathlib.Path,
    live_probe_root: pathlib.Path,
    live_campaign_root: pathlib.Path,
    profile: ProviderProfile | None = None,
    allow_identity_rollover: bool = False,
    expected_generation_contract: dict[str, str] | None = None,
    require_current_runtime: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    selected = profile or requested_provider_profile({}, default=CANONICAL_PROFILE_NAME)
    marker = _load_object(run_state_dir / f"{run_id}.json", f"readiness marker {run_id}")
    report_path = (
        live_probe_root / run_id / "report.json"
        if kind == "gate0_live_probe"
        else live_campaign_root / run_id / "report.json"
    )
    report = _load_object(report_path, f"readiness report {run_id}")
    report_hash = _sha256(report_path)
    raw_checks = report.get("checks")
    checks = raw_checks if isinstance(raw_checks, dict) else {}
    try:
        total_tokens = int(marker.get("total_tokens") or 0)
        total_cost = float(marker.get("total_cost_usd") or 0.0)
        max_tokens = int(marker.get("max_tokens") or 0)
        max_cost = float(marker.get("max_cost_usd") or 0.0)
    except (TypeError, ValueError) as exc:
        raise LiveReadinessError(f"readiness source {run_id} accounting is malformed") from exc
    if allow_identity_rollover:
        source_commit = str(marker.get("app_commit") or "")
        source_image = str(marker.get("runtime_image_id") or "")
        identity_valid = (
            len(source_commit) in {40, 64}
            and all(character in "0123456789abcdef" for character in source_commit)
            and len(source_image) == 71
            and source_image.startswith("sha256:")
            and all(character in "0123456789abcdef" for character in source_image[7:])
            and (not require_current_runtime or source_image == runtime_image_id)
        )
        if (
            expected_generation_contract is None
            or _readiness_generation_contract(
                report,
                profile=selected,
            )
            != expected_generation_contract
        ):
            raise LiveReadinessError(
                f"readiness source {run_id} generation contract differs from current B01"
            )
    else:
        identity_valid = (
            marker.get("app_commit") == app_commit
            and marker.get("runtime_image_id") == runtime_image_id
        )
    if (
        marker.get("run_id") != run_id
        or marker.get("kind") != kind
        or marker.get("status") != "completed"
        or marker.get("usage_complete") is not True
        or not identity_valid
        or marker.get("provider_profile", CANONICAL_PROFILE_NAME) != selected.name
        or marker.get("provider") != selected.ledger_provider
        or marker.get("model") != selected.normalized_model
        or marker.get("concurrency") != 1
        or marker.get("report_sha256") != report_hash
        or total_tokens <= 0
        or total_cost <= 0
        or max_tokens < total_tokens
        or max_cost < total_cost
        or report.get("ok") is not True
        or (
            report.get("functional_quality_passed") is not True
            if selected.functional_latency_gap_allowed
            else not checks or not all(checks.values())
        )
    ):
        raise LiveReadinessError(f"readiness source {run_id} is not current, green and accounted")
    entry: dict[str, Any] = {
        "run_id": run_id,
        "report_path": report_path.relative_to(root).as_posix(),
        "report_sha256": report_hash,
        "status": "PASS",
        "usage_complete": True,
        "output_reviewed_by_codex": True,
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost,
        "max_tokens": max_tokens,
        "max_cost_usd": max_cost,
    }
    if kind == "gate0_live_probe":
        if report.get("run_id") != run_id:
            raise LiveReadinessError(f"readiness source {run_id} report identity differs")
        if selected.functional_latency_gap_allowed and report.get("case_id") != "B01":
            raise LiveReadinessError("GLM capability smoke must be B01")
    else:
        case_id = str(marker.get("case_id") or "")
        if (
            case_id not in selected.pilot_case_ids
            or report.get("evaluation_id") != run_id
            or report.get("case_id") != case_id
        ):
            raise LiveReadinessError(f"readiness source {run_id} case identity differs")
        if case_id != "B15" and any(
            checks.get(name) is not True for name in REQUIRED_CAMPAIGN_CHECKS
        ):
            raise LiveReadinessError(
                f"readiness source {run_id} lacks required output-integrity checks"
            )
        entry["case_id"] = case_id
    if allow_identity_rollover:
        entry.update(
            {
                "source_app_commit": str(marker["app_commit"]),
                "source_runtime_image_id": str(marker["runtime_image_id"]),
            }
        )
    return entry, marker


def build_readiness(
    environment: Mapping[str, str] = os.environ,
    *,
    root: pathlib.Path = ROOT,
    run_state_dir: pathlib.Path = RUN_STATE_DIR,
    live_probe_root: pathlib.Path = LIVE_PROBE_ROOT,
    live_campaign_root: pathlib.Path = LIVE_CAMPAIGN_ROOT,
    contract_path: pathlib.Path = CONTRACT_LOCK_PATH,
    basket_path: pathlib.Path = EXPECTED_PATH,
    output_path: pathlib.Path = READINESS_PATH,
) -> dict[str, Any]:
    profile, warmup_id, smoke_id, pilot_ids, multiplier = _requested_sources(environment)
    allow_identity_rollover = (
        environment.get("READINESS_ALLOW_IDENTITY_ROLLOVER", "").lower() == "true"
    )
    if allow_identity_rollover and not profile.functional_latency_gap_allowed:
        raise LiveReadinessError("readiness identity rollover is limited to the exact GLM profile")
    previous_manifest = output_path.read_bytes() if output_path.exists() else None
    if (
        previous_manifest is not None
        and environment.get("ALLOW_READINESS_REPLACE", "").lower() != "true"
    ):
        raise LiveReadinessError(
            "live readiness manifest already exists; replacement requires "
            "ALLOW_READINESS_REPLACE=true"
        )
    app_commit, branch = frozen_git_identity(
        root=root,
        required_branch=profile.required_branch,
    )
    runtime_image_id = verify_running_profile()
    contract = _load_object(contract_path, "runtime contract lock")
    raw_runtime = contract.get("runtime")
    runtime = raw_runtime if isinstance(raw_runtime, dict) else {}
    if runtime.get("image_id") != runtime_image_id:
        raise LiveReadinessError("runtime contract and running image identities differ")
    generation_contract: dict[str, str] | None = None
    authority_sha256 = ""
    if allow_identity_rollover:
        raw_skill = contract.get("skill")
        skill = raw_skill if isinstance(raw_skill, dict) else {}
        raw_tools = contract.get("tools")
        tools = raw_tools if isinstance(raw_tools, dict) else {}
        generation_contract = {
            "prompt_hash": str(skill.get("prompt_hash") or ""),
            "skill_content_hash": str(skill.get("skill_content_hash") or ""),
            "tool_inventory_hash": str(tools.get("inventory_hash") or ""),
        }
        authority_path = root / "HANDOFF_VPS_P0_GLM_BASKET.md"
        authority_sha256 = _sha256(authority_path)
        if environment.get("GLM_NIGHT_AUTHORITY_SHA256", "").lower() != authority_sha256:
            raise LiveReadinessError("readiness identity rollover authority differs")

    def source(
        run_id: str,
        kind: str,
        *,
        require_current_runtime: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return _source_entry(
            run_id,
            kind=kind,
            app_commit=app_commit,
            runtime_image_id=runtime_image_id,
            root=root,
            run_state_dir=run_state_dir,
            live_probe_root=live_probe_root,
            live_campaign_root=live_campaign_root,
            profile=profile,
            allow_identity_rollover=allow_identity_rollover,
            expected_generation_contract=generation_contract,
            require_current_runtime=require_current_runtime,
        )

    if warmup_id is None:
        warmup = None
        smoke, smoke_marker = source(
            smoke_id,
            "gate0_live_probe",
            require_current_runtime=allow_identity_rollover,
        )
    else:
        warmup, _ = source(warmup_id, "gate0_live_probe")
        smoke, smoke_marker = source(smoke_id, "gate2_live_campaign")
    pilot_pairs = [source(run_id, "gate2_live_campaign") for run_id in pilot_ids]
    pilots = [entry for entry, _ in pilot_pairs]
    if len({str(entry.get("case_id") or "") for entry in pilots}) != len(pilots):
        raise LiveReadinessError("representative pilot cases must be distinct")
    selected_markers = [smoke_marker, *[marker for _, marker in pilot_pairs]]
    use_empirical_envelope = (
        environment.get("READINESS_USE_EMPIRICAL_OPERATION_ENVELOPE", "").lower() == "true"
    )
    observed_operation_count = 0
    if use_empirical_envelope:
        if not allow_identity_rollover:
            raise LiveReadinessError(
                "empirical recovery projection requires authorized identity rollover"
            )
        per_operation_tokens, per_operation_cost, observed_operation_count = (
            _readiness_operation_envelope([smoke, *pilots])
        )
        projection_basis = "largest_observed_hash_equal_operation"
        includes_maximum_output = False
    else:
        per_operation_tokens = max(int(marker["max_tokens"]) for marker in selected_markers)
        per_operation_cost = max(float(marker["max_cost_usd"]) for marker in selected_markers)
        projection_basis = "largest_selected_smoke_or_pilot_run_cap"
        includes_maximum_output = True
    projected_tokens = math.ceil(per_operation_tokens * TOTAL_PAID_OPERATION_WEIGHT * multiplier)
    projected_cost = round(
        per_operation_cost * TOTAL_PAID_OPERATION_WEIGHT * multiplier,
        8,
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "PASS",
        "generated_at": datetime.now(UTC).isoformat(),
        "app_commit": app_commit,
        "app_branch": branch,
        "provider_profile": profile.name,
        "runtime_image_id": runtime_image_id,
        "runtime_contract_hash": _sha256(contract_path),
        "basket_hash": _sha256(basket_path),
        "smoke": smoke,
        "pilots": pilots,
        "projection": {
            "basis": projection_basis,
            "paid_operation_count": TOTAL_PAID_OPERATION_WEIGHT,
            "per_operation_token_cap": per_operation_tokens,
            "per_operation_cost_cap_usd": per_operation_cost,
            "projected_tokens": projected_tokens,
            "projected_cost_usd": projected_cost,
            "safety_multiplier": multiplier,
            "includes_maximum_output": includes_maximum_output,
            "includes_bounded_output_headroom": use_empirical_envelope,
            "includes_configured_retries": True,
            "includes_safety_and_post_task": True,
            "account_remaining": "unknown",
        },
    }
    if warmup is not None:
        warmup["excluded_from_metrics"] = True
        manifest["warmup"] = warmup
    if allow_identity_rollover:
        manifest["identity_rollover"] = {
            "policy": "owner_authorized_current_b01_historical_green_pilots",
            "authority_sha256": authority_sha256,
            "current_smoke_required": True,
            "current_smoke_id": smoke_id,
            "generation_contract": generation_contract,
        }
    if use_empirical_envelope:
        quarantined_run_id = str(
            environment.get("READINESS_RECOVERY_QUARANTINED_RUN_ID") or ""
        ).strip()
        if not RUN_ID_PATTERN.fullmatch(quarantined_run_id):
            raise LiveReadinessError("empirical recovery projection requires quarantined run id")
        manifest["recovery_projection"] = {
            "policy": "owner_authorized_post_quarantine_empirical_envelope",
            "authority_sha256": authority_sha256,
            "quarantined_run_id": quarantined_run_id,
            "main_loop_max_tokens": profile.main_loop_max_tokens,
            "observed_operation_count": observed_operation_count,
        }
    _atomic_json(output_path, manifest)
    try:
        validate_readiness_manifest(
            output_path,
            commit=app_commit,
            contract_hash=manifest["runtime_contract_hash"],
            basket_hash=manifest["basket_hash"],
            runtime_image_id=runtime_image_id,
            profile=profile,
        )
    except Exception:
        if previous_manifest is None:
            output_path.unlink(missing_ok=True)
        else:
            _atomic_bytes(output_path, previous_manifest)
        raise
    return manifest


def main() -> int:
    try:
        report = build_readiness()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"live-readiness: FAIL: {exc}", file=sys.stderr)
        return 1
    projection = report["projection"]
    print(
        "live-readiness: PASS "
        f"pilots={len(report['pilots'])} projected_tokens={projection['projected_tokens']} "
        f"projected_cost_usd={projection['projected_cost_usd']} account_remaining=unknown"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
