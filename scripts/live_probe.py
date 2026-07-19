from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

from provider_profiles import (
    CANONICAL_PROFILE_NAME,
    GLM_FUNCTIONAL_PROFILE_NAME,
    ProviderProfileError,
    requested_provider_profile,
)
from scripts.budget_control import (
    DEFAULT_USAGE_LEDGER,
    RUN_ID_PATTERN,
    BudgetPolicyError,
    NightBudget,
    RunRequest,
    assert_provider_unchanged,
    bounded_request_estimate,
    night_marker_fields,
    normalize_model,
    read_usage_ledger,
    validate_paid_run_budget,
)
from scripts.generation_metadata import metadata_usage_rows, poll_generation_metadata
from scripts.preflight import run_preflight
from scripts.release_identity import frozen_git_identity

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_STATE_DIR = ROOT / "runtime" / "budget" / "runs"
EVIDENCE_ROOT = ROOT / "runtime" / "live-probes"
CONTRACT_LOCK = ROOT / "runtime" / "contracts" / "communication_factory.lock.json"
PROVIDER_ENV_NAMES = {
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
}


class LiveProbeError(RuntimeError):
    pass


def _required_positive_int(name: str, environment: dict[str, str] | None = None) -> int:
    source = environment if environment is not None else dict(os.environ)
    try:
        value = int(source.get(name, "0"))
    except ValueError as exc:
        raise LiveProbeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise LiveProbeError(f"{name} must be a positive integer")
    return value


def _required_positive_float(name: str, environment: dict[str, str] | None = None) -> float:
    source = environment if environment is not None else dict(os.environ)
    try:
        value = float(source.get(name, "0"))
    except ValueError as exc:
        raise LiveProbeError(f"{name} must be positive") from exc
    if value <= 0:
        raise LiveProbeError(f"{name} must be positive")
    return value


def requested_run(environment: dict[str, str] | None = None) -> RunRequest:
    source = environment if environment is not None else dict(os.environ)
    if source.get("ALLOW_LIVE_PROBE", "").lower() != "true":
        raise LiveProbeError("live probe requires ALLOW_LIVE_PROBE=true")
    try:
        profile = requested_provider_profile(source)
    except ProviderProfileError as exc:
        raise LiveProbeError(str(exc)) from exc
    return RunRequest(
        run_id=str(source.get("EVALUATION_ID") or "").strip(),
        provider=profile.ledger_provider,
        model=profile.normalized_model,
        max_tokens=_required_positive_int("EVAL_MAX_TOKENS", source),
        max_cost_usd=_required_positive_float("EVAL_MAX_COST_USD", source),
        projected_tokens=_required_positive_int("EVAL_PROJECTED_TOKENS", source),
        projected_cost_usd=_required_positive_float("EVAL_PROJECTED_COST_USD", source),
        concurrency=int(source.get("EVAL_CONCURRENCY", "0")),
        openrouter_enabled=profile.runtime_provider == "openrouter",
        profile_name=profile.name,
    )


def validate_retry_linkage() -> tuple[str, str]:
    previous_run_id = str(os.environ.get("PREVIOUS_EVALUATION_ID") or "").strip()
    retry_reason = str(os.environ.get("EVALUATION_RETRY_REASON") or "").strip()
    if bool(previous_run_id) != bool(retry_reason):
        raise LiveProbeError("retry linkage requires previous run id and reason")
    if not previous_run_id:
        return "", ""
    if not RUN_ID_PATTERN.fullmatch(previous_run_id):
        raise LiveProbeError("linked previous live probe id is invalid")
    if len(retry_reason) > 500 or any(ord(char) < 32 for char in retry_reason):
        raise LiveProbeError("live probe retry reason is invalid")
    previous_path = RUN_STATE_DIR / f"{previous_run_id}.json"
    try:
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveProbeError("linked previous live probe is unavailable") from exc
    if previous.get("status") != "failed":
        raise LiveProbeError("linked previous live probe is not failed")
    return previous_run_id, retry_reason


def _run_command(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _compose_container_id(service: str) -> str:
    process = _run_command(["docker", "compose", "ps", "-q", service])
    container_id = process.stdout.strip()
    if process.returncode != 0 or not container_id:
        raise LiveProbeError(f"Compose service {service} is not running")
    return container_id


def _inspect_json(container_id: str, template: str) -> Any:
    process = _run_command(["docker", "inspect", container_id, "--format", template])
    if process.returncode != 0:
        raise LiveProbeError("Docker runtime identity inspection failed")
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise LiveProbeError("Docker runtime identity inspection was invalid") from exc


def verify_running_profile() -> str:
    try:
        profile = requested_provider_profile(dict(os.environ))
    except ProviderProfileError as exc:
        raise LiveProbeError(str(exc)) from exc
    try:
        lock = json.loads(CONTRACT_LOCK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveProbeError("contract lock is unavailable") from exc
    expected_image = str((lock.get("runtime") or {}).get("image_id") or "")
    expected_main_loop_max_tokens = int(
        (lock.get("runtime") or {}).get("main_loop_max_tokens") or 0
    )
    expected_safety_call_timeout = int(
        (lock.get("runtime") or {}).get("safety_call_timeout_seconds") or 0
    )
    expected_tool_call_timeout = int(
        (lock.get("runtime") or {}).get("tool_call_timeout_seconds") or 0
    )
    ouroboros_id = _compose_container_id("ouroboros")
    app_id = _compose_container_id("app")
    running_image = str(_inspect_json(ouroboros_id, "{{json .Image}}") or "")
    if running_image != expected_image:
        raise LiveProbeError("running Ouroboros image differs from the contract lock")
    if expected_main_loop_max_tokens != profile.main_loop_max_tokens:
        raise LiveProbeError("runtime main-loop output cap differs from the requested profile")
    if expected_safety_call_timeout != profile.safety_call_timeout_seconds:
        raise LiveProbeError("runtime safety-call timeout differs from the requested profile")
    if expected_tool_call_timeout != profile.tool_call_timeout_seconds:
        raise LiveProbeError("runtime tool-call timeout differs from the requested profile")
    app_env = _inspect_json(app_id, "{{json .Config.Env}}")
    ouroboros_env = _inspect_json(ouroboros_id, "{{json .Config.Env}}")
    if not isinstance(app_env, list) or not isinstance(ouroboros_env, list):
        raise LiveProbeError("app environment inspection was invalid")
    names = {str(item).split("=", 1)[0] for item in app_env}
    if names & PROVIDER_ENV_NAMES:
        raise LiveProbeError("app container received a provider credential variable")
    app_values = {
        str(item).split("=", 1)[0]: str(item).split("=", 1)[1]
        for item in app_env
        if "=" in str(item)
    }
    expected_app = {
        "LIVE_PROVIDER_PROFILE": profile.name,
        "LIVE_TASK_TIMEOUT_SECONDS": str(profile.task_timeout_seconds),
        "LIVE_RUN_TERMINAL_DEADLINE_SECONDS": str(profile.terminal_deadline_seconds),
        "LIVE_USAGE_EXPECTED_PROVIDER": profile.ledger_provider,
        "LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY": str(profile.require_post_task_summary).lower(),
    }
    if any(app_values.get(name) != value for name, value in expected_app.items()):
        raise LiveProbeError("app live provider profile differs from the requested profile")
    runtime_values = {
        str(item).split("=", 1)[0]: str(item).split("=", 1)[1]
        for item in ouroboros_env
        if "=" in str(item)
    }
    if (
        runtime_values.get("CF_PROVIDER_PROFILE") != profile.name
        or runtime_values.get("CF_RUNTIME_PROVIDER") != profile.runtime_provider
        or runtime_values.get("OUROBOROS_MODEL") != profile.runtime_route
        or runtime_values.get("OUROBOROS_MODEL_FALLBACKS", "") != ""
        or runtime_values.get(profile.secret_file_env) != profile.secret_container_path
    ):
        raise LiveProbeError("Ouroboros runtime profile differs from the requested profile")
    return running_image


def validate_preflight(request: RunRequest) -> tuple[str, str, NightBudget | None]:
    run_preflight("bootstrap")
    try:
        night = validate_paid_run_budget(
            request,
            run_state_dir=RUN_STATE_DIR,
            run_kind="gate0_live_probe",
        )
    except BudgetPolicyError as exc:
        raise LiveProbeError(str(exc)) from exc
    if (RUN_STATE_DIR / f"{request.run_id}.json").exists():
        raise LiveProbeError("live probe run id was already used")
    if (EVIDENCE_ROOT / request.run_id).exists():
        raise LiveProbeError("live probe evidence directory already exists")
    validate_retry_linkage()
    selected = requested_provider_profile({"EVAL_PROVIDER_PROFILE": request.profile_name})
    commit, _ = frozen_git_identity(root=ROOT, required_branch=selected.required_branch)
    return verify_running_profile(), commit, night


def _atomic_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def reserve_run(
    request: RunRequest,
    image_id: str,
    app_commit: str,
    night: NightBudget | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    RUN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    marker = RUN_STATE_DIR / f"{request.run_id}.json"
    evidence_dir = EVIDENCE_ROOT / request.run_id
    previous_run_id, retry_reason = validate_retry_linkage()
    payload = {
        "schema_version": 1,
        "run_id": request.run_id,
        "kind": "gate0_live_probe",
        "provider": request.provider,
        "model": normalize_model(request.model),
        "provider_profile": request.profile_name,
        "max_tokens": request.max_tokens,
        "max_cost_usd": request.max_cost_usd,
        "projected_tokens": request.projected_tokens,
        "projected_cost_usd": request.projected_cost_usd,
        "concurrency": request.concurrency,
        "app_commit": app_commit,
        "runtime_image_id": image_id,
        "status": "running",
        "usage_complete": False,
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "account_remaining": "unknown",
    }
    if night is not None:
        payload.update(night_marker_fields(night))
    if previous_run_id:
        payload["retry_of"] = previous_run_id
        payload["retry_reason"] = retry_reason
    try:
        with marker.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        evidence_dir.mkdir()
    except FileExistsError as exc:
        raise LiveProbeError("live probe run or evidence id was already used") from exc
    return marker, evidence_dir


def finish_run(
    marker: pathlib.Path,
    *,
    status: str,
    usage_complete: bool,
    total_tokens: int = 0,
    total_cost_usd: float = 0.0,
    report_sha256: str = "",
    extra_fields: dict[str, Any] | None = None,
) -> None:
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload.update(
        {
            "status": status,
            "usage_complete": usage_complete,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost_usd, 8),
            "report_sha256": report_sha256,
            "finished_at": dt.datetime.now(dt.UTC).isoformat(),
        }
    )
    if extra_fields:
        payload.update(extra_fields)
    _atomic_json(marker, payload)


def usage_rows_from_report(
    run_id: str,
    report: dict[str, Any],
    *,
    expected_provider: str = "openai",
    expected_model: str = "gpt-5.4-mini",
) -> list[dict[str, Any]]:
    return _usage_rows_from_report(
        run_id,
        report,
        expected_provider=expected_provider,
        expected_model=expected_model,
    )


def observed_usage_rows_from_report(
    run_id: str,
    report: dict[str, Any],
    *,
    expected_provider: str,
) -> list[dict[str, Any]]:
    """Recover complete observed usage without concealing a model-route drift."""
    return _usage_rows_from_report(
        run_id,
        report,
        expected_provider=expected_provider,
        expected_model=None,
    )


def _usage_rows_from_report(
    run_id: str,
    report: dict[str, Any],
    *,
    expected_provider: str,
    expected_model: str | None,
) -> list[dict[str, Any]]:
    raw_operations = report.get("operations")
    operation_ledgers = (
        [
            operation.get("provider_call_ledger")
            for operation in raw_operations
            if isinstance(operation, dict)
        ]
        if isinstance(raw_operations, list) and raw_operations
        else [report.get("provider_call_ledger")]
    )
    if not operation_ledgers or any(not isinstance(ledger, dict) for ledger in operation_ledgers):
        raise LiveProbeError("live probe provider-call ledger is missing")
    rows: list[dict[str, Any]] = []
    for ledger in operation_ledgers:
        assert isinstance(ledger, dict)
        for category, raw in ledger.items():
            if not isinstance(raw, dict) or int(raw.get("call_count") or 0) <= 0:
                continue
            if category == "provider_retry" and int(raw.get("prompt_tokens") or 0) == 0:
                continue
            providers = [str(item) for item in raw.get("providers") or []]
            models = [normalize_model(str(item)) for item in raw.get("models") or []]
            prompt_tokens = int(raw.get("prompt_tokens") or 0)
            completion_tokens = int(raw.get("completion_tokens") or 0)
            cost_usd = float(raw.get("cost_usd") or 0.0)
            if (
                providers != [expected_provider]
                or len(models) != 1
                or (expected_model is not None and models[0] != expected_model)
                or prompt_tokens <= 0
                or completion_tokens < 0
                or cost_usd < 0
            ):
                raise LiveProbeError("live probe provider usage is incomplete")
            assert_provider_unchanged(expected_provider, providers[0])
            rows.append(
                {
                    "ts": dt.datetime.now(dt.UTC).isoformat(),
                    "run_id": run_id,
                    "provider": providers[0],
                    "model": models[0],
                    "category": str(category),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cost_usd": cost_usd,
                }
            )
    if not rows:
        raise LiveProbeError("live probe returned no provider usage")
    return rows


def _report_provider_accounting(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    raw_operations = report.get("operations")
    operations = raw_operations if isinstance(raw_operations, list) else [report]
    orphans: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        raw = operation.get("provider_accounting")
        accounting = raw if isinstance(raw, dict) else {}
        for candidate in accounting.get("orphan_requests") or []:
            if not isinstance(candidate, dict):
                continue
            generation_id = str(candidate.get("generation_id") or "")
            if generation_id and generation_id not in seen:
                seen.add(generation_id)
                orphans.append({str(key): value for key, value in candidate.items()})
        for candidate in accounting.get("pre_generation_anomalies") or []:
            if isinstance(candidate, dict):
                anomalies.append({str(key): value for key, value in candidate.items()})
    return {"orphan_requests": orphans, "pre_generation_anomalies": anomalies}


def _probe_bounded_estimates(
    night: NightBudget,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    estimates: list[dict[str, Any]] = []
    for request in requests:
        prompt_tokens = int(request.get("estimated_prompt_tokens") or 0)
        max_output = int(request.get("configured_max_output_tokens") or 0)
        tokens, cost = bounded_request_estimate(
            night,
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
                "estimated_tokens": tokens,
                "estimated_cost_usd": cost,
                "prompt_price_usd_per_million": night.prompt_price_usd_per_million,
                "completion_price_usd_per_million": (night.completion_price_usd_per_million),
                "safety_multiplier": night.estimate_safety_multiplier,
                "prompt_estimation_method": str(request.get("prompt_estimation_method") or ""),
            }
        )
    return estimates


def append_usage(rows: list[dict[str, Any]]) -> None:
    DEFAULT_USAGE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_USAGE_LEDGER.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def execute_transport(
    run_id: str,
    *,
    provider_profile_name: str = CANONICAL_PROFILE_NAME,
    timeout_seconds: int = 50,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if provider_profile_name == GLM_FUNCTIONAL_PROFILE_NAME:
        module = "apps.api.app.live_campaign_transport"
        identity_args = ["--case-id", "B01", "--evaluation-id", run_id]
    else:
        module = "apps.api.app.live_probe_transport"
        identity_args = ["--run-id", run_id]
    process = _run_command(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "app",
            "python",
            "-m",
            module,
            *identity_args,
        ],
        timeout=timeout_seconds,
    )
    try:
        report = json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise LiveProbeError("live probe transport returned no safe report") from exc
    if not isinstance(report, dict):
        raise LiveProbeError("live probe transport report is invalid")
    return process, report


def run_live_probe(
    request: RunRequest,
    image_id: str,
    app_commit: str,
    night: NightBudget | None = None,
) -> dict[str, Any]:
    marker, evidence_dir = reserve_run(request, image_id, app_commit, night)
    usage_complete = False
    total_tokens = 0
    total_cost = 0.0
    report_hash = ""
    extra_fields: dict[str, Any] = {}
    try:
        profile = requested_provider_profile({"EVAL_PROVIDER_PROFILE": request.profile_name})
        process, report = execute_transport(
            request.run_id,
            provider_profile_name=request.profile_name,
            timeout_seconds=profile.effective_terminal_deadline_seconds + 30,
        )
        report_path = evidence_dir / "report.json"
        _atomic_json(report_path, report)
        report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
        if report.get("runtime_image_id") != image_id:
            raise LiveProbeError("transport runtime image identity differs from the running image")
        if request.profile_name == GLM_FUNCTIONAL_PROFILE_NAME and (
            report.get("run_id") != request.run_id or report.get("case_id") != "B01"
        ):
            raise LiveProbeError("GLM capability smoke did not execute the exact B01 flow")
        accounting = _report_provider_accounting(report)
        try:
            rows = usage_rows_from_report(
                request.run_id,
                report,
                expected_provider=request.provider,
                expected_model=normalize_model(request.model),
            )
        except LiveProbeError:
            if not (night and night.additional_authority and any(accounting.values())):
                raise
            rows = []
        if rows:
            append_usage(rows)
        total_tokens = sum(
            int(row["prompt_tokens"]) + int(row["completion_tokens"]) for row in rows
        )
        total_cost = sum(float(row["cost_usd"]) for row in rows)
        metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
        usage_complete = bool(
            metrics.get("usage_complete")
            if metrics
            else (report.get("checks") or {}).get("usage_complete")
        )
        estimates: list[dict[str, Any]] = []
        metadata_poll: dict[str, Any] | None = None
        if not usage_complete and night and night.additional_authority:
            orphan_requests = accounting["orphan_requests"]
            anomalies = accounting["pre_generation_anomalies"]
            if not orphan_requests and not anomalies:
                raise LiveProbeError("incomplete usage has no safe physical-request disposition")
            unresolved = list(orphan_requests)
            if orphan_requests:
                try:
                    metadata_poll = poll_generation_metadata(
                        orphan_requests,
                        max_seconds=night.metadata_poll_max_seconds,
                    )
                except Exception as poll_exc:
                    metadata_poll = {
                        "schema_version": 1,
                        "status": "incomplete",
                        "poll_max_seconds": night.metadata_poll_max_seconds,
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
                        "error_type": type(poll_exc).__name__,
                    }
                _atomic_json(evidence_dir / "generation-metadata-poll.json", metadata_poll)
                try:
                    recovered_rows = metadata_usage_rows(
                        request.run_id,
                        orphan_requests,
                        metadata_poll,
                        expected_model=request.model,
                    )
                except Exception as recovery_exc:
                    recovered_rows = []
                    metadata_poll["recovery_error_type"] = type(recovery_exc).__name__
                    metadata_poll["resolved_generation_ids"] = []
                    metadata_poll["unresolved_generation_ids"] = [
                        str(row.get("generation_id") or "") for row in orphan_requests
                    ]
                    _atomic_json(evidence_dir / "generation-metadata-poll.json", metadata_poll)
                if recovered_rows:
                    append_usage(recovered_rows)
                    rows.extend(recovered_rows)
                    total_tokens = sum(
                        int(row["prompt_tokens"]) + int(row["completion_tokens"]) for row in rows
                    )
                    total_cost = sum(float(row["cost_usd"]) for row in rows)
                unresolved_ids = {
                    str(value) for value in metadata_poll.get("unresolved_generation_ids") or []
                }
                unresolved = [
                    row
                    for row in orphan_requests
                    if str(row.get("generation_id") or "") in unresolved_ids
                ]
                estimates = _probe_bounded_estimates(night, unresolved)
            usage_complete = bool(rows) and not unresolved
            disposition = (
                "mixed_incomplete_usage"
                if estimates and anomalies
                else "orphan_request_estimate"
                if estimates
                else "pre_generation_anomaly"
                if anomalies and not usage_complete
                else "metadata_recovered"
            )
            metadata_poll_hash = (
                hashlib.sha256(
                    (evidence_dir / "generation-metadata-poll.json").read_bytes()
                ).hexdigest()
                if metadata_poll is not None
                else ""
            )
            failure_classes = sorted(
                {
                    *("provider.orphan_generation" for _ in orphan_requests),
                    *(
                        "provider.http_429"
                        if int(row.get("status_code") or 0) == 429
                        else "provider.http_5xx"
                        if 500 <= int(row.get("status_code") or 0) <= 599
                        else "provider.pre_generation_anomaly"
                        for row in anomalies
                    ),
                }
            )
            artifact = {
                "schema_version": 1,
                "run_id": request.run_id,
                "policy": night.incomplete_usage_policy,
                "accounting_disposition": disposition,
                "known_tokens": total_tokens,
                "known_cost_usd": round(total_cost, 8),
                "pre_generation_anomalies": anomalies,
                "bounded_request_estimates": estimates,
                "metadata_poll_sha256": metadata_poll_hash,
                "metadata_poll_elapsed_seconds": int(
                    float((metadata_poll or {}).get("elapsed_seconds") or 0)
                ),
                "provider_ledger_mutated_by_estimate": False,
                "evidence_eligible": False,
                "failure_classes": failure_classes,
            }
            _atomic_json(evidence_dir / "accounting.json", artifact)
            extra_fields = {
                "known_tokens": total_tokens,
                "known_cost_usd": round(total_cost, 8),
                "provider_usage_unknown": not usage_complete,
                "evidence_eligible": False,
                "failure_classes": failure_classes,
                "accounting_artifact_sha256": hashlib.sha256(
                    (evidence_dir / "accounting.json").read_bytes()
                ).hexdigest(),
                "accounting_disposition": disposition,
                "pre_generation_anomalies": anomalies,
                "bounded_request_estimates": estimates,
                "metadata_poll_sha256": metadata_poll_hash,
                "metadata_poll_elapsed_seconds": artifact["metadata_poll_elapsed_seconds"],
            }
        estimated_tokens = sum(int(row["estimated_tokens"]) for row in estimates)
        estimated_cost = sum(float(row["estimated_cost_usd"]) for row in estimates)
        if (
            total_tokens + estimated_tokens > request.max_tokens
            or total_cost + estimated_cost > request.max_cost_usd
        ):
            raise LiveProbeError("live probe exceeded its supplied run cap")
        if process.returncode != 0 or not report.get("ok"):
            raise LiveProbeError("live probe completed with a failed engineering check")
        finish_run(
            marker,
            status="completed",
            usage_complete=usage_complete,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            report_sha256=report_hash,
            extra_fields={"evidence_eligible": True},
        )
        return report
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "run_id": request.run_id,
            "status": "failed",
            "error": str(exc),
            "preserved_at": dt.datetime.now(dt.UTC).isoformat(),
        }
        _atomic_json(evidence_dir / "failure.json", failure)
        finish_run(
            marker,
            status="failed",
            usage_complete=usage_complete,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            report_sha256=report_hash,
            extra_fields={
                "known_tokens": total_tokens,
                "known_cost_usd": round(total_cost, 8),
                "provider_usage_unknown": not usage_complete,
                "evidence_eligible": False,
                **extra_fields,
            },
        )
        raise


def recover_failed_probe(run_id: str) -> dict[str, Any]:
    marker = RUN_STATE_DIR / f"{run_id}.json"
    evidence_dir = EVIDENCE_ROOT / run_id
    report_path = evidence_dir / "report.json"
    recovery_path = evidence_dir / "usage-recovery.json"
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveProbeError("failed live probe accounting evidence is unavailable") from exc
    if (
        not isinstance(state, dict)
        or state.get("kind") != "gate0_live_probe"
        or state.get("status") != "failed"
        or state.get("usage_complete") is not False
    ):
        raise LiveProbeError("usage recovery requires a failed unaccounted live probe")
    if state.get("provider_usage_unknown") is True:
        raise LiveProbeError(
            "failed live probe has an orphaned physical provider request without exact usage"
        )
    if recovery_path.exists():
        raise LiveProbeError("live probe usage recovery already exists")
    if any(record.run_id == run_id for record in read_usage_ledger(DEFAULT_USAGE_LEDGER)):
        raise LiveProbeError("live probe usage was already recorded")
    report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
    if state.get("report_sha256") != report_hash or report.get("run_id") != run_id:
        raise LiveProbeError("failed live probe report identity or hash differs")
    checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
    if checks.get("usage_complete") is not True:
        raise LiveProbeError("failed live probe does not contain complete provider usage")
    expected_provider = str(state.get("provider") or "")
    expected_model = normalize_model(str(state.get("model") or ""))
    rows = observed_usage_rows_from_report(
        run_id,
        report,
        expected_provider=expected_provider,
    )
    observed_models = sorted({str(row["model"]) for row in rows})
    model_drift_detected = observed_models != [expected_model]
    total_tokens = sum(int(row["prompt_tokens"]) + int(row["completion_tokens"]) for row in rows)
    total_cost = sum(float(row["cost_usd"]) for row in rows)
    recovered_at = dt.datetime.now(dt.UTC).isoformat()
    recovery = {
        "schema_version": 1,
        "run_id": run_id,
        "report_sha256": report_hash,
        "provider": expected_provider,
        "expected_model": expected_model,
        "observed_models": observed_models,
        "model_drift_detected": model_drift_detected,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 8),
        "recovered_at": recovered_at,
    }
    _atomic_json(recovery_path, recovery)
    append_usage(rows)
    state.update(
        {
            "usage_complete": True,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 8),
            "usage_recovered": True,
            "usage_recovery_sha256": hashlib.sha256(recovery_path.read_bytes()).hexdigest(),
            "usage_recovered_at": recovered_at,
            "observed_models": observed_models,
            "model_drift_detected": model_drift_detected,
        }
    )
    _atomic_json(marker, state)
    return recovery


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--recover-failed-run")
    args = parser.parse_args(argv)
    if args.recover_failed_run:
        try:
            recovered = recover_failed_probe(str(args.recover_failed_run))
        except (OSError, ValueError, LiveProbeError) as exc:
            print(f"live-probe-recovery: FAIL: {exc}", file=sys.stderr)
            return 1
        print(
            "live-probe-recovery: PASS "
            f"run={recovered['run_id']} tokens={recovered['total_tokens']} "
            f"cost_usd={recovered['total_cost_usd']} "
            f"model_drift={str(recovered['model_drift_detected']).lower()}"
        )
        return 0
    try:
        request = requested_run()
        image_id, app_commit, night = validate_preflight(request)
        if args.check_only:
            print(
                f"live-probe-preflight: PASS provider={request.provider} model={request.model} "
                "concurrency=1 account_remaining=unknown"
            )
            return 0
        report = run_live_probe(request, image_id, app_commit, night)
    except (OSError, ValueError, subprocess.SubprocessError, LiveProbeError) as exc:
        print(f"live-probe: FAIL: {exc}", file=sys.stderr)
        return 1
    latency = report.get("latency_ms") or {}
    ledger = report.get("provider_call_ledger") or {}
    calls = sum(int(row.get("call_count") or 0) for row in ledger.values())
    print(
        "live-probe: PASS "
        f"run={request.run_id} calls={calls} "
        f"user_visible_ms={int(latency.get('user_visible') or 0)} "
        f"worker_occupancy_ms={int(latency.get('full_worker_occupancy') or 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
