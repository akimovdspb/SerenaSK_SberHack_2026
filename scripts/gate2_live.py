from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from provider_profiles import ProviderProfileError, requested_provider_profile
from scripts.budget_control import (
    DEFAULT_USAGE_LEDGER,
    RUN_ID_PATTERN,
    BudgetPolicyError,
    NightBudget,
    RunRequest,
    night_marker_fields,
    normalize_model,
    read_usage_ledger,
    requested_night_budget,
    validate_paid_run_budget,
)
from scripts.generation_metadata import metadata_usage_rows, poll_generation_metadata
from scripts.live_probe import (
    RUN_STATE_DIR,
    _probe_bounded_estimates,
    _report_provider_accounting,
    append_usage,
    usage_rows_from_report,
    verify_running_profile,
)
from scripts.preflight import run_preflight
from scripts.release_identity import frozen_git_identity

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = ROOT / "runtime" / "live-campaigns"
PILOT_CASE_IDS = ("B04", "B07", "B08", "B14", "B15")
MetadataPoller = Callable[[list[dict[str, Any]], int], dict[str, Any]]
UsageAppender = Callable[[list[dict[str, Any]]], None]


class Gate2LiveError(RuntimeError):
    pass


def _positive_int(name: str) -> int:
    try:
        value = int(os.environ.get(name, "0"))
    except ValueError as exc:
        raise Gate2LiveError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise Gate2LiveError(f"{name} must be a positive integer")
    return value


def _positive_float(name: str) -> float:
    try:
        value = float(os.environ.get(name, "0"))
    except ValueError as exc:
        raise Gate2LiveError(f"{name} must be positive") from exc
    if value <= 0:
        raise Gate2LiveError(f"{name} must be positive")
    return value


def requested_run() -> RunRequest:
    if os.environ.get("ALLOW_GATE2_LIVE", "").lower() != "true":
        raise Gate2LiveError("Gate 2 live pilot requires ALLOW_GATE2_LIVE=true")
    try:
        profile = requested_provider_profile(dict(os.environ))
    except ProviderProfileError as exc:
        raise Gate2LiveError(str(exc)) from exc
    return RunRequest(
        run_id=str(os.environ.get("EVALUATION_ID") or "").strip(),
        provider=profile.ledger_provider,
        model=profile.normalized_model,
        max_tokens=_positive_int("EVAL_MAX_TOKENS"),
        max_cost_usd=_positive_float("EVAL_MAX_COST_USD"),
        projected_tokens=_positive_int("EVAL_PROJECTED_TOKENS"),
        projected_cost_usd=_positive_float("EVAL_PROJECTED_COST_USD"),
        concurrency=int(os.environ.get("EVAL_CONCURRENCY", "0")),
        openrouter_enabled=profile.runtime_provider == "openrouter",
        profile_name=profile.name,
    )


def requested_case_id() -> str:
    case_id = str(os.environ.get("PILOT_CASE_ID") or "B04").strip()
    try:
        profile = requested_provider_profile(dict(os.environ))
    except ProviderProfileError as exc:
        raise Gate2LiveError(str(exc)) from exc
    if case_id not in profile.pilot_case_ids:
        allowed = (
            "B04, B07 or B08"
            if profile.pilot_case_ids == ("B04", "B07", "B08")
            else ", ".join(profile.pilot_case_ids)
        )
        raise Gate2LiveError(f"PILOT_CASE_ID must be one of {allowed}")
    return case_id


def validate_retry_linkage(case_id: str) -> tuple[str, str]:
    previous_run_id = str(os.environ.get("PREVIOUS_EVALUATION_ID") or "").strip()
    retry_reason = str(os.environ.get("EVALUATION_RETRY_REASON") or "").strip()
    if bool(previous_run_id) != bool(retry_reason):
        raise Gate2LiveError("retry linkage requires previous run id and reason")
    if not previous_run_id:
        return "", ""
    if not RUN_ID_PATTERN.fullmatch(previous_run_id):
        raise Gate2LiveError("linked previous Gate 2 run id is invalid")
    if len(retry_reason) > 500 or any(ord(character) < 32 for character in retry_reason):
        raise Gate2LiveError("Gate 2 retry reason is invalid")
    try:
        previous = json.loads(
            (RUN_STATE_DIR / f"{previous_run_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise Gate2LiveError("linked previous Gate 2 run is unavailable") from exc
    if previous.get("kind") != "gate2_live_campaign" or previous.get("status") != "failed":
        raise Gate2LiveError("linked previous Gate 2 run is not a failed campaign attempt")
    if previous.get("case_id") != case_id:
        raise Gate2LiveError("linked previous Gate 2 run uses a different pilot case")
    return previous_run_id, retry_reason


def validate_preflight(
    request: RunRequest,
    case_id: str,
) -> tuple[str, str, NightBudget | None]:
    run_preflight("bootstrap")
    try:
        night = validate_paid_run_budget(
            request,
            run_state_dir=RUN_STATE_DIR,
            run_kind="gate2_live_campaign",
        )
    except BudgetPolicyError as exc:
        raise Gate2LiveError(str(exc)) from exc
    if (RUN_STATE_DIR / f"{request.run_id}.json").exists():
        raise Gate2LiveError("Gate 2 live run id was already used")
    if (EVIDENCE_ROOT / request.run_id).exists():
        raise Gate2LiveError("Gate 2 live evidence directory already exists")
    validate_retry_linkage(case_id)
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
    case_id: str,
    app_commit: str,
    night: NightBudget | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    RUN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    marker = RUN_STATE_DIR / f"{request.run_id}.json"
    evidence_dir = EVIDENCE_ROOT / request.run_id
    previous_run_id, retry_reason = validate_retry_linkage(case_id)
    payload = {
        "schema_version": 1,
        "run_id": request.run_id,
        "kind": "gate2_live_campaign",
        "case_id": case_id,
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
        raise Gate2LiveError("Gate 2 live run or evidence id was already used") from exc
    return marker, evidence_dir


def finish_run(
    marker: pathlib.Path,
    *,
    status: str,
    usage_complete: bool,
    total_tokens: int,
    total_cost_usd: float,
    report_sha256: str,
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


def poll_orphan_metadata(
    requests: list[dict[str, Any]],
    max_seconds: int,
) -> dict[str, Any]:
    return poll_generation_metadata(requests, max_seconds=max_seconds)


def _failure_classes(
    case_id: str,
    report: dict[str, Any],
    accounting: dict[str, list[dict[str, Any]]],
) -> list[str]:
    classes: set[str] = set()
    if accounting["orphan_requests"]:
        classes.add("provider.orphan_generation")
    for anomaly in accounting["pre_generation_anomalies"]:
        status = int(anomaly.get("status_code") or 0)
        if status == 429:
            classes.add("provider.http_429")
        elif 500 <= status <= 599:
            classes.add("provider.http_5xx")
        else:
            classes.add("provider.pre_generation_anomaly")
    reason_codes = {
        str((report.get("run") or {}).get("reason_code") or "")
        if isinstance(report.get("run"), dict)
        else ""
    }
    for operation in report.get("operations") or []:
        if isinstance(operation, dict) and isinstance(operation.get("run"), dict):
            reason_codes.add(str(operation["run"].get("reason_code") or ""))
    if report.get("ok") is not True:
        classes.add(
            f"case.{case_id.lower()}.tool_sequence_invalid"
            if "TOOL_SEQUENCE_INVALID" in reason_codes
            else f"case.{case_id.lower()}.evaluation"
        )
    return sorted(classes)


def _account_incomplete_usage(
    *,
    run_id: str,
    case_id: str,
    report: dict[str, Any],
    evidence_dir: pathlib.Path,
    night: NightBudget,
    expected_model: str,
    known_tokens: int,
    known_cost_usd: float,
    metadata_poller: MetadataPoller = poll_orphan_metadata,
    usage_appender: UsageAppender = append_usage,
) -> dict[str, Any]:
    accounting = _report_provider_accounting(report)
    orphan_requests = accounting["orphan_requests"]
    anomalies = accounting["pre_generation_anomalies"]
    if not orphan_requests and not anomalies:
        raise Gate2LiveError("incomplete pilot usage has no safe physical-request disposition")

    unresolved = list(orphan_requests)
    metadata_poll: dict[str, Any] | None = None
    recovered_rows: list[dict[str, Any]] = []
    if orphan_requests:
        try:
            metadata_poll = metadata_poller(orphan_requests, night.metadata_poll_max_seconds)
        except Exception as exc:
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
                "error_type": type(exc).__name__,
            }
        _atomic_json(evidence_dir / "generation-metadata-poll.json", metadata_poll)
        try:
            recovered_rows = metadata_usage_rows(
                run_id,
                orphan_requests,
                metadata_poll,
                expected_model=expected_model,
            )
        except Exception as exc:
            recovered_rows = []
            metadata_poll["recovery_error_type"] = type(exc).__name__
            metadata_poll["resolved_generation_ids"] = []
            metadata_poll["unresolved_generation_ids"] = [
                str(row.get("generation_id") or "") for row in orphan_requests
            ]
            _atomic_json(evidence_dir / "generation-metadata-poll.json", metadata_poll)
        if recovered_rows:
            usage_appender(recovered_rows)
            known_tokens += sum(
                int(row["prompt_tokens"]) + int(row["completion_tokens"]) for row in recovered_rows
            )
            known_cost_usd += sum(float(row["cost_usd"]) for row in recovered_rows)
        unresolved_ids = {
            str(value) for value in metadata_poll.get("unresolved_generation_ids") or []
        }
        unresolved = [
            row for row in orphan_requests if str(row.get("generation_id") or "") in unresolved_ids
        ]

    estimates = _probe_bounded_estimates(night, unresolved)
    usage_complete = bool(known_tokens) and not unresolved and not anomalies
    disposition = (
        "mixed_incomplete_usage"
        if estimates and anomalies
        else "orphan_request_estimate"
        if estimates
        else "pre_generation_anomaly"
        if anomalies
        else "metadata_recovered"
    )
    metadata_poll_hash = (
        hashlib.sha256((evidence_dir / "generation-metadata-poll.json").read_bytes()).hexdigest()
        if metadata_poll is not None
        else ""
    )
    failure_classes = _failure_classes(case_id, report, accounting)
    artifact = {
        "schema_version": 1,
        "run_id": run_id,
        "policy": night.incomplete_usage_policy,
        "accounting_disposition": disposition,
        "provider_usage_complete": usage_complete,
        "provider_ledger_mutated_by_estimate": False,
        "known_tokens": known_tokens,
        "known_cost_usd": round(known_cost_usd, 8),
        "pre_generation_anomalies": anomalies,
        "bounded_request_estimates": estimates,
        "metadata_poll_sha256": metadata_poll_hash,
        "metadata_poll_elapsed_seconds": math.ceil(
            float((metadata_poll or {}).get("elapsed_seconds") or 0.0)
        ),
        "failure_classes": failure_classes,
        "evidence_eligible": bool(report.get("ok") is True and usage_complete),
    }
    _atomic_json(evidence_dir / "accounting.json", artifact)
    marker_fields = {
        "known_tokens": known_tokens,
        "known_cost_usd": round(known_cost_usd, 8),
        "provider_usage_unknown": not usage_complete,
        "evidence_eligible": artifact["evidence_eligible"],
        "failure_classes": failure_classes,
        "accounting_artifact_sha256": hashlib.sha256(
            (evidence_dir / "accounting.json").read_bytes()
        ).hexdigest(),
        "accounting_disposition": disposition,
        "pre_generation_anomalies": anomalies,
        "bounded_request_estimates": estimates,
        "bounded_estimated_tokens": sum(int(row["estimated_tokens"]) for row in estimates),
        "bounded_estimated_cost_usd": round(
            sum(float(row["estimated_cost_usd"]) for row in estimates), 8
        ),
        "metadata_poll_sha256": metadata_poll_hash,
        "metadata_poll_elapsed_seconds": artifact["metadata_poll_elapsed_seconds"],
        "usage_recovered_from_metadata": bool(recovered_rows),
    }
    return {
        "usage_complete": usage_complete,
        "total_tokens": known_tokens,
        "total_cost_usd": round(known_cost_usd, 8),
        "estimated_tokens": marker_fields["bounded_estimated_tokens"],
        "estimated_cost_usd": marker_fields["bounded_estimated_cost_usd"],
        "marker_fields": marker_fields,
    }


def reconcile_failed_accounting(
    run_id: str,
    night: NightBudget,
    *,
    ledger_path: pathlib.Path = DEFAULT_USAGE_LEDGER,
    metadata_poller: MetadataPoller = poll_orphan_metadata,
    usage_appender: UsageAppender = append_usage,
) -> dict[str, Any]:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise Gate2LiveError("Gate 2 accounting reconciliation run id is invalid")
    marker = RUN_STATE_DIR / f"{run_id}.json"
    evidence_dir = EVIDENCE_ROOT / run_id
    report_path = evidence_dir / "report.json"
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Gate2LiveError("failed Gate 2 accounting evidence is unavailable") from exc
    if (evidence_dir / "accounting.json").exists():
        raise Gate2LiveError("Gate 2 accounting reconciliation already exists")
    if (
        not isinstance(state, dict)
        or state.get("kind") != "gate2_live_campaign"
        or state.get("status") != "failed"
        or state.get("usage_complete") is not False
        or state.get("night_id") != night.night_id
        or state.get("night_authority_sha256") != night.authority_sha256
    ):
        raise Gate2LiveError("accounting reconciliation requires the bound failed pilot")
    if report.get("evaluation_id") != run_id or report.get("case_id") != state.get("case_id"):
        raise Gate2LiveError("Gate 2 accounting report identity mismatch")
    report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
    if report_hash != state.get("report_sha256"):
        raise Gate2LiveError("Gate 2 accounting report hash differs from its marker")
    records = [record for record in read_usage_ledger(ledger_path) if record.run_id == run_id]
    known_tokens = sum(record.total_tokens for record in records)
    known_cost = sum(record.cost_usd for record in records)
    if known_tokens != int(state.get("total_tokens") or 0) or not math.isclose(
        known_cost,
        float(state.get("total_cost_usd") or 0.0),
        rel_tol=0,
        abs_tol=1e-8,
    ):
        raise Gate2LiveError("Gate 2 known usage differs from its ledger")
    result = _account_incomplete_usage(
        run_id=run_id,
        case_id=str(state["case_id"]),
        report=report,
        evidence_dir=evidence_dir,
        night=night,
        expected_model=str(state["model"]),
        known_tokens=known_tokens,
        known_cost_usd=known_cost,
        metadata_poller=metadata_poller,
        usage_appender=usage_appender,
    )
    state.update(
        {
            "usage_complete": result["usage_complete"],
            "total_tokens": result["total_tokens"],
            "total_cost_usd": result["total_cost_usd"],
            "accounting_reconciled_at": dt.datetime.now(dt.UTC).isoformat(),
            **result["marker_fields"],
            "evidence_eligible": False,
        }
    )
    _atomic_json(marker, state)
    return {
        "run_id": run_id,
        "usage_complete": result["usage_complete"],
        "total_tokens": result["total_tokens"],
        "total_cost_usd": result["total_cost_usd"],
        "bounded_estimated_tokens": result["estimated_tokens"],
        "bounded_estimated_cost_usd": result["estimated_cost_usd"],
    }


def execute_transport(
    run_id: str,
    case_id: str,
    *,
    timeout_seconds: int = 60,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    module = (
        "apps.api.app.live_evaluation_transport"
        if case_id == "B15"
        else "apps.api.app.live_campaign_transport"
    )
    process = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "app",
            "python",
            "-m",
            module,
            "--case-id",
            case_id,
            "--evaluation-id",
            run_id,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    try:
        report = json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise Gate2LiveError("Gate 2 live transport returned no safe report") from exc
    if not isinstance(report, dict):
        raise Gate2LiveError("Gate 2 live transport report is invalid")
    return process, {str(key): value for key, value in report.items()}


def execute_recovery(
    evaluation_id: str,
    internal_run_id: str,
    case_id: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    process = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "app",
            "python",
            "-m",
            "apps.api.app.live_campaign_transport",
            "--recover-run-id",
            internal_run_id,
            "--recover-case-id",
            case_id,
            "--evaluation-id",
            evaluation_id,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    try:
        report = json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise Gate2LiveError("Gate 2 recovery returned no safe report") from exc
    if not isinstance(report, dict):
        raise Gate2LiveError("Gate 2 recovery report is invalid")
    return process, {str(key): value for key, value in report.items()}


def recover_failed_run(evaluation_id: str, internal_run_id: str) -> dict[str, Any]:
    marker = RUN_STATE_DIR / f"{evaluation_id}.json"
    evidence_dir = EVIDENCE_ROOT / evaluation_id
    postmortem_path = evidence_dir / "postmortem.json"
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Gate2LiveError("failed Gate 2 run marker is unavailable") from exc
    if state.get("kind") != "gate2_live_campaign" or state.get("status") != "failed":
        raise Gate2LiveError("recovery requires a failed Gate 2 live marker")
    case_id = str(state.get("case_id") or "")
    if case_id not in PILOT_CASE_IDS:
        raise Gate2LiveError("failed Gate 2 marker has an invalid pilot case")
    if postmortem_path.exists():
        raise Gate2LiveError("Gate 2 postmortem already exists")
    if any(record.run_id == evaluation_id for record in read_usage_ledger(DEFAULT_USAGE_LEDGER)):
        raise Gate2LiveError("Gate 2 usage was already recorded")
    process, report = execute_recovery(evaluation_id, internal_run_id, case_id)
    if report.get("evaluation_id") != evaluation_id:
        raise Gate2LiveError("Gate 2 recovery evaluation identity mismatch")
    raw_run = report.get("run")
    run: dict[str, Any] = (
        {str(key): value for key, value in raw_run.items()} if isinstance(raw_run, dict) else {}
    )
    if str(run.get("run_id") or "") != internal_run_id:
        raise Gate2LiveError("Gate 2 recovery internal run identity mismatch")
    rows = usage_rows_from_report(evaluation_id, report)
    total_tokens = sum(int(row["prompt_tokens"]) + int(row["completion_tokens"]) for row in rows)
    total_cost = sum(float(row["cost_usd"]) for row in rows)
    _atomic_json(postmortem_path, report)
    postmortem_hash = hashlib.sha256(postmortem_path.read_bytes()).hexdigest()
    append_usage(rows)
    raw_checks = report.get("checks")
    checks = raw_checks if isinstance(raw_checks, dict) else {}
    state.update(
        {
            "usage_complete": bool(checks.get("usage_complete")),
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 8),
            "usage_recovered": True,
            "postmortem_sha256": postmortem_hash,
            "usage_recovered_at": dt.datetime.now(dt.UTC).isoformat(),
        }
    )
    _atomic_json(marker, state)
    if process.returncode == 0 or report.get("ok"):
        raise Gate2LiveError("failed-run recovery unexpectedly reported success")
    return {
        "evaluation_id": evaluation_id,
        "internal_run_id": internal_run_id,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 8),
        "usage_complete": bool(checks.get("usage_complete")),
        "postmortem_sha256": postmortem_hash,
    }


def run_live(
    request: RunRequest,
    image_id: str,
    case_id: str,
    app_commit: str,
    night: NightBudget | None = None,
) -> dict[str, Any]:
    marker, evidence_dir = reserve_run(request, image_id, case_id, app_commit, night)
    usage_complete = False
    total_tokens = 0
    total_cost = 0.0
    report_hash = ""
    extra_fields: dict[str, Any] = {}
    try:
        profile = requested_provider_profile({"EVAL_PROVIDER_PROFILE": request.profile_name})
        operation_multiplier = 2 if case_id == "B15" else 1
        process, report = execute_transport(
            request.run_id,
            case_id,
            timeout_seconds=(profile.effective_terminal_deadline_seconds + 30)
            * operation_multiplier,
        )
        if report.get("case_id") != case_id:
            raise Gate2LiveError("Gate 2 live transport case identity mismatch")
        report_path = evidence_dir / "report.json"
        _atomic_json(report_path, report)
        report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
        accounting = _report_provider_accounting(report)
        try:
            rows = usage_rows_from_report(
                request.run_id,
                report,
                expected_provider=request.provider,
                expected_model=normalize_model(request.model),
            )
        except RuntimeError:
            if not (night and night.additional_authority and any(accounting.values())):
                raise
            rows = []
        total_tokens = sum(
            int(row["prompt_tokens"]) + int(row["completion_tokens"]) for row in rows
        )
        total_cost = sum(float(row["cost_usd"]) for row in rows)
        raw_checks = report.get("checks")
        checks: dict[str, Any] = (
            {str(key): value for key, value in raw_checks.items()}
            if isinstance(raw_checks, dict)
            else {}
        )
        raw_metrics = report.get("metrics")
        metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
        usage_complete = bool(checks.get("usage_complete") or metrics.get("usage_complete"))
        if rows:
            append_usage(rows)
        failure_classes = _failure_classes(case_id, report, accounting)
        extra_fields = {
            "evidence_eligible": False,
            "failure_classes": failure_classes,
        }
        estimated_tokens = 0
        estimated_cost = 0.0
        if not usage_complete and night and night.additional_authority:
            accounted = _account_incomplete_usage(
                run_id=request.run_id,
                case_id=case_id,
                report=report,
                evidence_dir=evidence_dir,
                night=night,
                expected_model=request.model,
                known_tokens=total_tokens,
                known_cost_usd=total_cost,
            )
            usage_complete = bool(accounted["usage_complete"])
            total_tokens = int(accounted["total_tokens"])
            total_cost = float(accounted["total_cost_usd"])
            estimated_tokens = int(accounted["estimated_tokens"])
            estimated_cost = float(accounted["estimated_cost_usd"])
            extra_fields.update(accounted["marker_fields"])
        if (
            total_tokens + estimated_tokens > request.max_tokens
            or total_cost + estimated_cost > request.max_cost_usd
        ):
            raise Gate2LiveError("Gate 2 live pilot exceeded its supplied run cap")
        if not usage_complete:
            raise Gate2LiveError(
                "Gate 2 live pilot usage remains incomplete after bounded metadata polling"
            )
        if process.returncode != 0 or not report.get("ok"):
            raise Gate2LiveError("Gate 2 live pilot completed with a failed engineering check")
        finish_run(
            marker,
            status="completed",
            usage_complete=usage_complete,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            report_sha256=report_hash,
            extra_fields={
                **extra_fields,
                "provider_usage_unknown": False,
                "evidence_eligible": True,
            },
        )
        return report
    except Exception as exc:
        _atomic_json(
            evidence_dir / "failure.json",
            {
                "schema_version": 1,
                "run_id": request.run_id,
                "status": "failed",
                "error": str(exc),
                "preserved_at": dt.datetime.now(dt.UTC).isoformat(),
            },
        )
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
                **extra_fields,
                "evidence_eligible": False,
            },
        )
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--recover-run-id")
    parser.add_argument("--reconcile-accounting", action="store_true")
    args = parser.parse_args(argv)
    if args.reconcile_accounting:
        evaluation_id = str(os.environ.get("EVALUATION_ID") or "").strip()
        try:
            reconciliation_night = requested_night_budget(dict(os.environ))
            reconciled = reconcile_failed_accounting(evaluation_id, reconciliation_night)
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
            print(f"gate2-live-accounting: FAIL: {exc}", file=sys.stderr)
            return 1
        print(
            "gate2-live-accounting: PASS "
            f"evaluation={reconciled['run_id']} "
            f"usage_complete={str(reconciled['usage_complete']).lower()} "
            f"tokens={reconciled['total_tokens']} "
            f"cost_usd={reconciled['total_cost_usd']} "
            f"bounded_tokens={reconciled['bounded_estimated_tokens']}"
        )
        return 0
    if args.recover_run_id:
        evaluation_id = str(os.environ.get("EVALUATION_ID") or "").strip()
        try:
            recovered = recover_failed_run(evaluation_id, args.recover_run_id)
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
            print(f"gate2-live-recovery: FAIL: {exc}", file=sys.stderr)
            return 1
        print(
            "gate2-live-recovery: PASS "
            f"evaluation={recovered['evaluation_id']} tokens={recovered['total_tokens']} "
            f"cost_usd={recovered['total_cost_usd']}"
        )
        return 0
    try:
        request = requested_run()
        case_id = requested_case_id()
        image_id, app_commit, night = validate_preflight(request, case_id)
        if args.check_only:
            print(
                f"gate2-live-preflight: PASS provider={request.provider} model={request.model} "
                f"case={case_id} concurrency=1 account_remaining=unknown"
            )
            return 0
        report = run_live(request, image_id, case_id, app_commit, night)
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
        print(f"gate2-live: FAIL: {exc}", file=sys.stderr)
        return 1
    raw_run = report.get("run")
    run: dict[str, Any] = (
        {str(key): value for key, value in raw_run.items()} if isinstance(raw_run, dict) else {}
    )
    raw_latency = report.get("latency_ms")
    latency: dict[str, Any] = (
        {str(key): value for key, value in raw_latency.items()}
        if isinstance(raw_latency, dict)
        else {}
    )
    print(
        "gate2-live: PASS "
        f"evaluation={request.run_id} case={case_id} run={run.get('run_id')} "
        f"observed_ms={int(latency.get('end_to_observation') or 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
