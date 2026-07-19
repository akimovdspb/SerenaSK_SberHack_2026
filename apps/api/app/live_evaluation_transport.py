from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from typing import Any

from apps.api.app.domain.campaigns import CampaignBriefInput
from apps.api.app.domain.learning import (
    FeedbackCreateRequest,
    RuleApprovalRequest,
    RuleRollbackRequest,
)
from apps.api.app.domain.models import RuleScope
from apps.api.app.domain.workflow import (
    ApprovalDecision,
    ApprovalRequest,
    CampaignState,
    RunStatus,
)
from apps.api.app.live_probe_transport import (
    account_terminal_task_without_provider_request,
    observed_tool_names,
    parse_sse_events,
    provider_call_ledger,
    provider_request_accounting,
    queue_contains_task,
    usage_is_complete,
)
from apps.api.app.mcp.service import FactoryMcpService
from apps.api.app.ouroboros_client import ALLOWED_PROVIDER_TOOLS, OuroborosTaskAdapter
from apps.api.app.settings import get_settings
from apps.api.app.workflow.runs import RunCoordinator
from apps.api.app.workflow.store import WorkflowStore
from provider_profiles import (
    CANONICAL_PROFILE_NAME,
    normalize_provider_model,
    provider_profile,
)

ONLINE_BANK_CONCEPT = "payouts_via_online_bank"
CASE_IDS = tuple(f"B{ordinal:02d}" for ordinal in range(1, 16))


class LiveEvaluationTransportError(RuntimeError):
    pass


def _workflow() -> WorkflowStore:
    settings = get_settings()
    workflow = WorkflowStore(
        settings.DATABASE_URL,
        data_dir=settings.SYNTHETIC_DATA_DIR,
        artifacts_dir=settings.ARTIFACTS_DIR,
    )
    workflow.initialize()
    return workflow


def _adapter() -> OuroborosTaskAdapter:
    settings = get_settings()
    return OuroborosTaskAdapter(
        base_url=settings.OUROBOROS_BASE_URL,
        lock_path=settings.CONTRACT_LOCK_PATH,
        skill_path=settings.SKILL_PATH,
    )


def _mcp(workflow: WorkflowStore) -> FactoryMcpService:
    settings = get_settings()
    service = FactoryMcpService(settings.DATABASE_URL, draft_processor=workflow)
    service.initialize()
    return service


def _elapsed_ms(start: datetime | None, end: datetime | None) -> int:
    if start is None or end is None:
        return 0
    return max(0, round((end - start).total_seconds() * 1_000))


def _safe_task(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        key: raw.get(key)
        for key in (
            "task_id",
            "status",
            "reason_code",
            "total_rounds",
            "project_id",
            "memory_mode",
        )
    }


def _providers(ledger: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(provider)
            for row in ledger.values()
            for provider in row.get("providers") or []
            if provider
        }
    )


def _usage_projection(ledger: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for category, row in ledger.items():
        result[category] = {
            "calls": int(row.get("call_count") or 0),
            "prompt_tokens": int(row.get("prompt_tokens") or 0),
            "completion_tokens": int(row.get("completion_tokens") or 0),
            "cached_tokens": int(row.get("cached_tokens") or 0),
            "cache_write_tokens": int(row.get("cache_write_tokens") or 0),
            "cost_usd": round(float(row.get("cost_usd") or 0.0), 8),
            "models": list(row.get("models") or []),
            "providers": list(row.get("providers") or []),
        }
    return result


def _result_for_operation(
    workflow: WorkflowStore,
    *,
    campaign_id: str,
    operation: str,
    context_version: str,
    package_id: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    package = workflow.get_package(package_id) if package_id is not None else None
    proposal = None
    if operation == "rule_proposal":
        proposal = next(
            (
                item
                for item in workflow.workspace(campaign_id).rule_proposals
                if item.context_version == context_version
            ),
            None,
        )
    return (
        package.model_dump(mode="json") if package is not None else None,
        proposal.model_dump(mode="json") if proposal is not None else None,
    )


def _operation_report(
    *,
    workflow: WorkflowStore,
    mcp: FactoryMcpService,
    adapter: OuroborosTaskAdapter,
    run_id: str,
) -> dict[str, Any]:
    settings = get_settings()
    profile = provider_profile(settings.LIVE_PROVIDER_PROFILE)
    completed = workflow.get_run(run_id)
    task_attempts: list[dict[str, Any]] = []
    for attempt in completed.attempts:
        try:
            observed = adapter.task(attempt.task_id)
        except Exception:
            observed = {}
        task_attempts.append(
            {
                "attempt_id": attempt.attempt_id,
                "attempt_number": attempt.attempt_number,
                "task_id": attempt.task_id,
                "status": observed.get("status"),
                "reason_code": observed.get("reason_code"),
            }
        )
    task: dict[str, Any] = {}
    raw_events: list[dict[str, Any]] = []
    task_in_queue = True
    if completed.task_id is not None:
        try:
            task = adapter.task(completed.task_id)
            raw_events = parse_sse_events(adapter.task_events_text(completed.task_id))
            task_in_queue = queue_contains_task(adapter.tasks(), completed.task_id)
        except Exception:
            task = {}
            raw_events = []
    ledger = provider_call_ledger(raw_events)
    tool_receipts = observed_tool_names(raw_events)
    snapshot = mcp.probe_snapshot(
        completed.campaign_id,
        operation=completed.operation,
        iteration=completed.iteration,
    )
    package, proposal = _result_for_operation(
        workflow,
        campaign_id=completed.campaign_id,
        operation=completed.operation,
        context_version=completed.context_version,
        package_id=completed.package_id,
    )
    safe_events = [item.model_dump(mode="json") for item in workflow.run_events(run_id)]
    raw_mcp_events = snapshot.get("events")
    mcp_events: list[Any] = list(raw_mcp_events) if isinstance(raw_mcp_events, list) else []
    audit_types = [
        str(item.get("event_type") or "") for item in mcp_events if isinstance(item, dict)
    ]
    package_ok = bool(
        package is not None
        and isinstance(package.get("quality_report"), dict)
        and package["quality_report"].get("approvable") is True
        and package["quality_report"].get("findings") == []
        and len(package["quality_report"].get("checked_ids") or []) == 22
    )
    proposal_ok = bool(
        proposal is not None
        and proposal.get("status") == "READY_FOR_APPROVAL"
        and proposal.get("validation_errors") == []
        and isinstance(proposal.get("tests"), list)
        and proposal.get("tests")
        and all(
            isinstance(item, dict) and item.get("passed") is True
            for item in proposal.get("tests") or []
        )
    )
    user_visible_ms = _elapsed_ms(completed.created_at, completed.terminal_at)
    worker_ms = _elapsed_ms(completed.created_at, completed.worker_released_at)
    try:
        runtime_state = adapter.state()
    except Exception:
        runtime_state = {}
    runtime_spent = float(runtime_state.get("spent_usd") or 0.0)
    runtime_limit = float(runtime_state.get("budget_limit") or 0.0)
    checks = {
        "run_completed_live": completed.status is RunStatus.COMPLETED
        and completed.mode == "live_ouroboros",
        "operation_output_present": proposal_ok
        if completed.operation == "rule_proposal"
        else package_ok,
        "exact_two_tool_receipts": tool_receipts == ALLOWED_PROVIDER_TOOLS,
        "context_completed_once": (
            1 <= audit_types.count("context_tool_completed") <= completed.physical_attempt_count
        ),
        "context_completed_within_attempt_bound": (
            1 <= audit_types.count("context_tool_completed") <= completed.physical_attempt_count
        ),
        "draft_saved_once": audit_types.count("draft_saved") == 1,
        "one_persisted_draft": isinstance(snapshot.get("draft"), dict),
        "usage_complete": usage_is_complete(
            ledger,
            expected_provider=profile.ledger_provider,
            require_post_task_summary=profile.require_post_task_summary,
        ),
        "provider_route_unchanged": _providers(ledger) == [profile.ledger_provider],
        "model_route_unchanged": sorted(
            {
                normalize_provider_model(str(model))
                for row in ledger.values()
                for model in row.get("models") or []
                if model
            }
        )
        == [profile.normalized_model],
        "provider_retry_absent": int((ledger.get("provider_retry") or {}).get("call_count") or 0)
        == 0,
        "user_visible_under_30s": 0 < user_visible_ms < 30_000,
        "worker_released": completed.worker_released_at is not None and not task_in_queue,
        "attempt_audit_complete": len(completed.attempts) == completed.physical_attempt_count
        and all(attempt.released_at is not None for attempt in completed.attempts),
        "retry_identity_stable": len(
            {
                (
                    attempt.provider,
                    attempt.model,
                    attempt.provider_profile,
                    attempt.request_digest,
                    attempt.context_digest,
                )
                for attempt in completed.attempts
            }
        )
        == 1,
    }
    raw_draft = snapshot.get("draft")
    draft: dict[str, Any] = dict(raw_draft) if isinstance(raw_draft, dict) else {}
    canonical_latency_passed = bool(checks["user_visible_under_30s"])
    functional_quality_passed = all(
        value for name, value in checks.items() if name != "user_visible_under_30s"
    )
    safe_task = _safe_task(task)
    return {
        "schema_version": 1,
        "ok": functional_quality_passed
        and (canonical_latency_passed or profile.functional_latency_gap_allowed),
        "functional_quality_passed": functional_quality_passed,
        "canonical_latency_passed": canonical_latency_passed,
        "latency_gap": functional_quality_passed and not canonical_latency_passed,
        "operation": completed.operation,
        "context": workflow.get_current_context(completed.campaign_id).model_dump(mode="json"),
        "run": completed.model_dump(mode="json"),
        "task": safe_task,
        "task_attempts": task_attempts,
        "package": package,
        "rule_proposal": proposal,
        "provider_call_ledger": ledger,
        "logical_provider_call_ledger": completed.provider_call_ledger,
        "provider_accounting": account_terminal_task_without_provider_request(
            provider_request_accounting(raw_events),
            task=safe_task,
            ledger=ledger,
        ),
        "usage_by_category": _usage_projection(ledger),
        "safe_events": safe_events,
        "mcp_calls": [dict(item) for item in mcp_events if isinstance(item, dict)],
        "mcp": {
            "draft_id": draft.get("draft_id"),
            "draft_hash": draft.get("draft_hash"),
            "audit_event_types": audit_types,
            "authorization_attempts": mcp.authorization_attempts(completed.run_id),
        },
        "latency_ms": {
            "user_visible_terminal": user_visible_ms,
            "full_worker_occupancy": worker_ms,
        },
        "runtime_budget": {
            "spent_usd": round(runtime_spent, 8),
            "limit_usd": round(runtime_limit, 8),
            "remaining_usd": round(max(0.0, runtime_limit - runtime_spent), 8),
        },
        "checks": checks,
    }


def run_operation(workflow: WorkflowStore, campaign_id: str) -> dict[str, Any]:
    service = _mcp(workflow)
    adapter = _adapter()
    settings = get_settings()
    profile = provider_profile(settings.LIVE_PROVIDER_PROFILE)
    coordinator = RunCoordinator(
        store=workflow,
        mcp_service=service,
        adapter=adapter,
        task_timeout_seconds=profile.effective_task_timeout_seconds,
        terminal_deadline_seconds=profile.effective_terminal_deadline_seconds,
        usage_expected_provider=profile.ledger_provider,
        usage_require_post_task_summary=profile.require_post_task_summary,
        controlled_provider_retry_enabled=settings.CONTROLLED_PROVIDER_RETRY_ENABLED,
        provider_identity=profile.runtime_provider,
        model_identity=profile.normalized_model,
        provider_profile=profile.name,
    )
    run_id = ""
    failure: Exception | None = None
    try:
        accepted = coordinator.start_live(campaign_id)
        run_id = accepted.run_id
        coordinator.wait(run_id, timeout=profile.effective_terminal_deadline_seconds + 5)
    except Exception as exc:
        failure = exc
    finally:
        coordinator.shutdown()
    if run_id:
        report_adapter = _adapter()
        try:
            report = _operation_report(
                workflow=workflow,
                mcp=service,
                adapter=report_adapter,
                run_id=run_id,
            )
        except Exception as exc:
            failure = failure or exc
        else:
            if failure is not None:
                report.update(
                    {
                        "ok": False,
                        "functional_quality_passed": False,
                        "error_type": type(failure).__name__,
                    }
                )
            return report
        finally:
            report_adapter.close()
    return {
        "schema_version": 1,
        "ok": False,
        "run_id": run_id or None,
        "error_type": type(failure).__name__ if failure is not None else "UnknownError",
        "checks": {"usage_complete": False},
        "provider_call_ledger": {},
        "provider_accounting": {
            "schema_version": 1,
            "orphan_requests": [],
            "pre_generation_anomalies": [],
        },
        "usage_by_category": {},
    }


def _combined_metrics(operations: list[dict[str, Any]], workflow_elapsed_ms: int) -> dict[str, Any]:
    usage: dict[str, dict[str, Any]] = {}
    user_latencies: list[int] = []
    worker_latencies: list[int] = []
    for operation in operations:
        raw_latency = operation.get("latency_ms")
        if isinstance(raw_latency, dict):
            user_latencies.append(int(raw_latency.get("user_visible_terminal") or 0))
            worker_latencies.append(int(raw_latency.get("full_worker_occupancy") or 0))
        raw_usage = operation.get("usage_by_category")
        if not isinstance(raw_usage, dict):
            continue
        for raw_name, raw_row in raw_usage.items():
            if not isinstance(raw_row, dict):
                continue
            name = str(raw_name)
            row = usage.setdefault(
                name,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                    "cost_usd": 0.0,
                    "models": [],
                    "providers": [],
                },
            )
            for field in (
                "calls",
                "prompt_tokens",
                "completion_tokens",
                "cached_tokens",
                "cache_write_tokens",
            ):
                row[field] = int(row[field]) + int(raw_row.get(field) or 0)
            row["cost_usd"] = round(
                float(row["cost_usd"]) + float(raw_row.get("cost_usd") or 0.0),
                8,
            )
            for field in ("models", "providers"):
                values = row[field]
                if not isinstance(values, list):
                    continue
                for value in raw_row.get(field) or []:
                    if isinstance(value, str) and value not in values:
                        values.append(value)
                values.sort()
    return {
        "usage_complete": bool(operations)
        and all(
            (operation.get("checks") or {}).get("usage_complete") is True
            for operation in operations
        ),
        "user_visible_terminal_ms": max(user_latencies, default=0),
        "full_worker_occupancy_ms": max(worker_latencies, default=0),
        "workflow_elapsed_ms": workflow_elapsed_ms,
        "operation_count": len(operations),
        "provider_calls": sum(int(row["calls"]) for row in usage.values()),
        "prompt_tokens": sum(int(row["prompt_tokens"]) for row in usage.values()),
        "completion_tokens": sum(int(row["completion_tokens"]) for row in usage.values()),
        "cached_tokens": sum(int(row["cached_tokens"]) for row in usage.values()),
        "cache_write_tokens": sum(int(row["cache_write_tokens"]) for row in usage.values()),
        "cost_usd": round(sum(float(row["cost_usd"]) for row in usage.values()), 8),
        "usage_by_category": usage,
    }


def _primary_fields(operation: dict[str, Any] | None) -> dict[str, Any]:
    selected = operation or {}
    return {
        "run": selected.get("run"),
        "task": selected.get("task"),
        "safe_events": selected.get("safe_events", []),
        "mcp_calls": selected.get("mcp_calls", []),
        "provider_call_ledger": selected.get("provider_call_ledger", {}),
        "provider_accounting": selected.get("provider_accounting", {}),
    }


def _case_report(
    *,
    evaluation_id: str,
    case_id: str,
    initial_state: str,
    terminal_state: str,
    context: dict[str, Any] | None,
    package: dict[str, Any] | None,
    validation: dict[str, Any] | None,
    operations: list[dict[str, Any]],
    primary: dict[str, Any] | None,
    started: float,
    learning: dict[str, Any] | None = None,
    extra_ok: bool = True,
) -> dict[str, Any]:
    elapsed = max(0, round((time.monotonic() - started) * 1_000))
    mode = (
        str((primary.get("run") or {}).get("mode") or "live_ouroboros")
        if primary is not None
        else "validation_only"
    )
    operations_ok = all(operation.get("ok") is True for operation in operations)
    functional_quality_passed = (
        all(
            operation.get("functional_quality_passed", operation.get("ok")) is True
            for operation in operations
        )
        if operations
        else True
    )
    canonical_latency_passed = (
        all(operation.get("canonical_latency_passed", True) is True for operation in operations)
        if operations
        else True
    )
    return {
        "schema_version": 1,
        "evaluation_id": evaluation_id,
        "case_id": case_id,
        "provider_profile": provider_profile(
            os.environ.get("LIVE_PROVIDER_PROFILE", CANONICAL_PROFILE_NAME)
        ).name,
        "ok": operations_ok and extra_ok,
        "functional_quality_passed": functional_quality_passed and extra_ok,
        "canonical_latency_passed": canonical_latency_passed,
        "latency_gap": functional_quality_passed and extra_ok and not canonical_latency_passed,
        "initial_state": initial_state,
        "terminal_state": terminal_state,
        "mode": mode,
        "context": context,
        "package": package,
        "validation": validation,
        "operations": operations,
        "metrics": _combined_metrics(operations, elapsed)
        if operations
        else {
            "usage_complete": True,
            "user_visible_terminal_ms": elapsed,
            "full_worker_occupancy_ms": elapsed,
            "workflow_elapsed_ms": elapsed,
            "operation_count": 0,
            "provider_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "cost_usd": 0.0,
            "usage_by_category": {},
        },
        "learning": learning or {},
        **_primary_fields(primary),
    }


def _standard_case(workflow: WorkflowStore, case_id: str, evaluation_id: str) -> dict[str, Any]:
    started = time.monotonic()
    created = workflow.create_campaign(brief=None, case_id=case_id)
    validated = workflow.validate_campaign(created.campaign_id)
    validation = validated.validation.model_dump(mode="json") if validated.validation else None
    if validated.state is not CampaignState.READY:
        return _case_report(
            evaluation_id=evaluation_id,
            case_id=case_id,
            initial_state=validated.state.value,
            terminal_state=validated.state.value,
            context=None,
            package=None,
            validation=validation,
            operations=[],
            primary=None,
            started=started,
        )
    context = workflow.get_current_context(created.campaign_id).model_dump(mode="json")
    operation = run_operation(workflow, created.campaign_id)
    package = operation.get("package") if isinstance(operation.get("package"), dict) else None
    terminal = workflow.get_campaign(created.campaign_id).state.value
    return _case_report(
        evaluation_id=evaluation_id,
        case_id=case_id,
        initial_state=validated.state.value,
        terminal_state=terminal,
        context=context,
        package=package,
        validation=validation,
        operations=[operation],
        primary=operation,
        started=started,
    )


def _b01(workflow: WorkflowStore, evaluation_id: str) -> dict[str, Any]:
    started = time.monotonic()
    operations: list[dict[str, Any]] = []
    created = workflow.create_campaign(brief=None, case_id="B01")
    needs = workflow.validate_campaign(created.campaign_id)
    validation = needs.validation.model_dump(mode="json") if needs.validation else None
    answers = CampaignBriefInput(
        cta_label="Собрать первый реестр",
        cta_url="https://pulse-pay.example.test/start",
    )
    workflow.patch_brief(
        created.campaign_id,
        answers,
        fields_set=set(answers.model_fields_set),
    )
    ready = workflow.validate_campaign(created.campaign_id)
    if ready.state is not CampaignState.READY:
        return _case_report(
            evaluation_id=evaluation_id,
            case_id="B01",
            initial_state=needs.state.value,
            terminal_state=ready.state.value,
            context=None,
            package=None,
            validation=validation,
            operations=[],
            primary=None,
            started=started,
            extra_ok=False,
        )
    context_v1 = workflow.get_current_context(created.campaign_id).model_dump(mode="json")
    initial = run_operation(workflow, created.campaign_id)
    operations.append(initial)
    package_v1 = initial.get("package") if isinstance(initial.get("package"), dict) else None
    if initial.get("ok") is not True or package_v1 is None:
        return _case_report(
            evaluation_id=evaluation_id,
            case_id="B01",
            initial_state=needs.state.value,
            terminal_state=workflow.get_campaign(created.campaign_id).state.value,
            context=context_v1,
            package=package_v1,
            validation=validation,
            operations=operations,
            primary=initial,
            started=started,
            extra_ok=False,
        )
    feedback = workflow.create_feedback(
        str(package_v1["package_id"]),
        FeedbackCreateRequest(
            artifact_path="/email/sections/0/body",
            comment=f"Добавьте разрешённое понятие {ONLINE_BANK_CONCEPT}.",
            scope="CURRENT_CHANNEL",
            author_role="editor",
        ),
        author_id="evaluation_test_editor",
    )
    workflow.prepare_revision_context(str(package_v1["package_id"]), feedback.feedback_id)
    revision = run_operation(workflow, created.campaign_id)
    operations.append(revision)
    package_v2 = revision.get("package") if isinstance(revision.get("package"), dict) else None
    if revision.get("ok") is not True or package_v2 is None:
        return _case_report(
            evaluation_id=evaluation_id,
            case_id="B01",
            initial_state=needs.state.value,
            terminal_state=workflow.get_campaign(created.campaign_id).state.value,
            context=context_v1,
            package=package_v1,
            validation=validation,
            operations=operations,
            primary=initial,
            started=started,
            extra_ok=False,
        )
    diff = workflow.get_package_diff(str(package_v2["package_id"]))
    package_approval = workflow.approve_package(
        str(package_v2["package_id"]),
        ApprovalRequest(
            package_hash=str(package_v2["package_hash"]),
            decision=ApprovalDecision.APPROVED,
            test_only=True,
        ),
        actor_id="evaluation_test_approver",
    )
    export = workflow.export_package(str(package_v2["package_id"]))
    workflow.prepare_rule_proposal_context(
        feedback.feedback_id,
        RuleScope(product_ids=["synthetic_payroll"], channel="email", segment_ids=[]),
    )
    rule_operation = run_operation(workflow, created.campaign_id)
    operations.append(rule_operation)
    raw_proposal = rule_operation.get("rule_proposal")
    if rule_operation.get("ok") is not True or not isinstance(raw_proposal, dict):
        return _case_report(
            evaluation_id=evaluation_id,
            case_id="B01",
            initial_state=needs.state.value,
            terminal_state=CampaignState.APPROVABLE.value,
            context=context_v1,
            package=package_v1,
            validation=validation,
            operations=operations,
            primary=initial,
            started=started,
            extra_ok=False,
        )
    proposal_payload = raw_proposal.get("proposal")
    if not isinstance(proposal_payload, dict):
        raise LiveEvaluationTransportError("rule proposal payload is missing")
    rule_approval = workflow.approve_rule_proposal(
        str(raw_proposal["proposal_id"]),
        RuleApprovalRequest(
            candidate_rules_version=str(proposal_payload["candidate_rules_version"]),
            test_only=True,
        ),
        actor_id="evaluation_test_approver",
    )
    learning = {
        "clarification": validation,
        "package_v1": package_v1,
        "feedback": feedback.model_dump(mode="json"),
        "package_v2": package_v2,
        "diff": diff.model_dump(mode="json"),
        "rule_proposal": raw_proposal,
        "rule_tests": raw_proposal.get("tests", []),
        "rule_approval": rule_approval.model_dump(mode="json"),
        "package_approval": package_approval.model_dump(mode="json"),
        "campaign_export": export.model_dump(mode="json"),
        "campaign_export_container_path": str(workflow.export_path(export.export_id)),
    }
    return _case_report(
        evaluation_id=evaluation_id,
        case_id="B01",
        initial_state=needs.state.value,
        terminal_state=CampaignState.APPROVABLE.value,
        context=context_v1,
        package=package_v1,
        validation=validation,
        operations=operations,
        primary=initial,
        started=started,
        learning=learning,
    )


def _b03(
    workflow: WorkflowStore,
    evaluation_id: str,
    *,
    rule_version_id: str,
    active_rules_version: str,
) -> dict[str, Any]:
    started = time.monotonic()
    created = workflow.create_campaign(brief=None, case_id="B03")
    ready = workflow.validate_campaign(created.campaign_id)
    validation = ready.validation.model_dump(mode="json") if ready.validation else None
    if ready.state is not CampaignState.READY:
        return _case_report(
            evaluation_id=evaluation_id,
            case_id="B03",
            initial_state=ready.state.value,
            terminal_state=ready.state.value,
            context=None,
            package=None,
            validation=validation,
            operations=[],
            primary=None,
            started=started,
            extra_ok=False,
        )
    context = workflow.get_current_context(created.campaign_id).model_dump(mode="json")
    operation = run_operation(workflow, created.campaign_id)
    package = operation.get("package") if isinstance(operation.get("package"), dict) else None
    rollback = workflow.rollback_rule(
        rule_version_id,
        RuleRollbackRequest(
            active_rules_version=active_rules_version,
            reason="Live evaluation test-only cleanup.",
            test_only=True,
        ),
        actor_id="evaluation_test_approver",
    )
    learning = {
        "second_case_application": {
            "context_version": context.get("context_version"),
            "rule_version_id": rule_version_id,
            "package": package,
        },
        "rollback": rollback.model_dump(mode="json"),
    }
    return _case_report(
        evaluation_id=evaluation_id,
        case_id="B03",
        initial_state=ready.state.value,
        terminal_state=workflow.get_campaign(created.campaign_id).state.value,
        context=context,
        package=package,
        validation=validation,
        operations=[operation],
        primary=operation,
        started=started,
        learning=learning,
    )


def _b15(workflow: WorkflowStore, evaluation_id: str) -> dict[str, Any]:
    started = time.monotonic()
    created = workflow.create_campaign(brief=None, case_id="B15")
    ready = workflow.validate_campaign(created.campaign_id)
    validation = ready.validation.model_dump(mode="json") if ready.validation else None
    if ready.state is not CampaignState.READY:
        return _case_report(
            evaluation_id=evaluation_id,
            case_id="B15",
            initial_state=ready.state.value,
            terminal_state=ready.state.value,
            context=None,
            package=None,
            validation=validation,
            operations=[],
            primary=None,
            started=started,
            extra_ok=False,
        )
    context_v1 = workflow.get_current_context(created.campaign_id).model_dump(mode="json")
    initial = run_operation(workflow, created.campaign_id)
    package_v1 = initial.get("package") if isinstance(initial.get("package"), dict) else None
    if initial.get("ok") is not True or package_v1 is None:
        return _case_report(
            evaluation_id=evaluation_id,
            case_id="B15",
            initial_state=ready.state.value,
            terminal_state=workflow.get_campaign(created.campaign_id).state.value,
            context=context_v1,
            package=package_v1,
            validation=validation,
            operations=[initial],
            primary=initial,
            started=started,
            extra_ok=False,
        )
    feedback = workflow.create_feedback(
        str(package_v1["package_id"]),
        FeedbackCreateRequest(
            artifact_path="/email/sections/0/body",
            comment="Добавьте разрешённое понятие concept_online_connection.",
            scope="CURRENT_CHANNEL",
            author_role="editor",
        ),
        author_id="evaluation_test_editor",
    )
    workflow.prepare_revision_context(str(package_v1["package_id"]), feedback.feedback_id)
    revision = run_operation(workflow, created.campaign_id)
    package_v2 = revision.get("package") if isinstance(revision.get("package"), dict) else None
    diff = (
        workflow.get_package_diff(str(package_v2["package_id"])) if package_v2 is not None else None
    )
    learning = {
        "b15_revision": {
            "feedback": feedback.model_dump(mode="json"),
            "package_v1": package_v1,
            "package_v2": package_v2,
            "diff": diff.model_dump(mode="json") if diff is not None else None,
        }
    }
    return _case_report(
        evaluation_id=evaluation_id,
        case_id="B15",
        initial_state=ready.state.value,
        terminal_state=workflow.get_campaign(created.campaign_id).state.value,
        context=revision.get("context")
        if isinstance(revision.get("context"), dict)
        else context_v1,
        package=package_v2,
        validation=validation,
        operations=[initial, revision],
        primary=revision,
        started=started,
        learning=learning,
        extra_ok=package_v2 is not None and diff is not None,
    )


def run_case(
    case_id: str,
    evaluation_id: str,
    *,
    rule_version_id: str = "",
    active_rules_version: str = "",
) -> dict[str, Any]:
    workflow = _workflow()
    if case_id == "B01":
        return _b01(workflow, evaluation_id)
    if case_id == "B03":
        if not rule_version_id or not active_rules_version:
            raise LiveEvaluationTransportError("B03 requires the approved B01 rule identity")
        return _b03(
            workflow,
            evaluation_id,
            rule_version_id=rule_version_id,
            active_rules_version=active_rules_version,
        )
    if case_id == "B15":
        return _b15(workflow, evaluation_id)
    return _standard_case(workflow, case_id, evaluation_id)


def preflight() -> dict[str, Any]:
    settings = get_settings()
    profile = provider_profile(settings.LIVE_PROVIDER_PROFILE)
    workflow = _workflow()
    active_rule_ids, rules_version = workflow.active_rule_state()
    adapter = _adapter()
    try:
        admission = adapter.admit()
        state = adapter.state()
    finally:
        adapter.close()
    spent = float(state.get("spent_usd") or 0.0)
    limit = float(state.get("budget_limit") or 0.0)
    return {
        "schema_version": 1,
        "provider_profile": profile.name,
        "provider": profile.ledger_provider,
        "model": profile.normalized_model,
        "task_timeout_seconds": profile.effective_task_timeout_seconds,
        "terminal_deadline_seconds": profile.effective_terminal_deadline_seconds,
        "require_post_task_summary": profile.require_post_task_summary,
        "ok": bool(
            not active_rule_ids
            and not workflow.active_runs()
            and limit > spent
            and state.get("supervisor_ready")
            and not state.get("supervisor_error")
        ),
        "active_rule_ids": list(active_rule_ids),
        "rules_version": rules_version,
        "active_run_count": len(workflow.active_runs()),
        "runtime_budget": {
            "spent_usd": round(spent, 8),
            "limit_usd": round(limit, 8),
            "remaining_usd": round(max(0.0, limit - spent), 8),
        },
        "runtime": {
            "workers_alive": int(state.get("workers_alive") or 0),
            "workers_total": int(state.get("workers_total") or 0),
            "runtime_mode": state.get("runtime_mode"),
            "context_mode": state.get("context_mode"),
            "safety_mode": state.get("safety_mode"),
        },
        "admission": {
            "prompt_hash": admission.prompt_hash,
            "skill_content_hash": admission.skill_content_hash,
            "tool_inventory_hash": admission.tool_inventory_hash,
            "activation_mode": admission.activation_mode,
            "runtime_image_id": admission.runtime_image_id,
        },
        "provider_calls": 0,
    }


def cleanup_rule(rule_version_id: str, active_rules_version: str) -> dict[str, Any]:
    workflow = _workflow()
    active_ids, current_version = workflow.active_rule_state()
    if not active_ids:
        return {
            "schema_version": 1,
            "ok": True,
            "status": "ALREADY_INACTIVE",
            "provider_calls": 0,
        }
    if rule_version_id not in active_ids or current_version != active_rules_version:
        raise LiveEvaluationTransportError("active rule cleanup identity does not match")
    rollback = workflow.rollback_rule(
        rule_version_id,
        RuleRollbackRequest(
            active_rules_version=active_rules_version,
            reason="Live evaluation early-stop cleanup.",
            test_only=True,
        ),
        actor_id="evaluation_test_approver",
    )
    return {
        "schema_version": 1,
        "ok": True,
        "status": "ROLLED_BACK",
        "rollback": rollback.model_dump(mode="json"),
        "provider_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--preflight", action="store_true")
    source.add_argument("--case-id", choices=CASE_IDS)
    source.add_argument("--cleanup-rule-version-id")
    parser.add_argument("--evaluation-id")
    parser.add_argument("--rule-version-id", default="")
    parser.add_argument("--active-rules-version", default="")
    args = parser.parse_args(argv)
    try:
        if args.preflight:
            report = preflight()
        elif args.cleanup_rule_version_id:
            if not args.active_rules_version:
                raise LiveEvaluationTransportError("active rules version is required for cleanup")
            report = cleanup_rule(
                str(args.cleanup_rule_version_id),
                str(args.active_rules_version),
            )
        else:
            if not args.evaluation_id:
                raise LiveEvaluationTransportError("evaluation identity is required")
            report = run_case(
                str(args.case_id),
                str(args.evaluation_id),
                rule_version_id=str(args.rule_version_id),
                active_rules_version=str(args.active_rules_version),
            )
    except Exception as exc:
        report = {
            "schema_version": 1,
            "ok": False,
            "error_type": type(exc).__name__,
            "provider_calls": 0,
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
