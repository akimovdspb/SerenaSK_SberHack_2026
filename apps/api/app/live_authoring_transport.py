from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import request_ledger
from apps.api.app.domain.authoring import CustomProductCreateRequest
from apps.api.app.domain.campaigns import CampaignBriefInput, ContextBundle
from apps.api.app.domain.workflow import CampaignState, PackageView, RunStatus
from apps.api.app.live_probe_transport import (
    observed_tool_names,
    parse_sse_events,
    provider_call_ledger,
    queue_contains_task,
    usage_is_complete,
)
from apps.api.app.mcp.service import FactoryMcpService
from apps.api.app.ouroboros_client import ALLOWED_PROVIDER_TOOLS, OuroborosTaskAdapter
from apps.api.app.services.quality import initial_fact_placement_issues
from apps.api.app.settings import get_settings
from apps.api.app.workflow.runs import RunCoordinator
from apps.api.app.workflow.store import WorkflowStore
from provider_profiles import (
    CAMPAIGN_AUTHORING_PROFILE_NAME,
    normalize_provider_model,
    provider_profile,
)

REFERENCE_IDS = (
    "editorial_dq01",
    "editorial_dq03",
    "editorial_dq06",
    "editorial_dq07",
    "editorial_dq09",
    "editorial_dq11",
    "editorial_dq12",
)


class LiveAuthoringTransportError(RuntimeError):
    pass


def _components() -> tuple[WorkflowStore, FactoryMcpService, OuroborosTaskAdapter]:
    settings = get_settings()
    workflow = WorkflowStore(
        settings.DATABASE_URL,
        data_dir=settings.SYNTHETIC_DATA_DIR,
        artifacts_dir=settings.ARTIFACTS_DIR,
    )
    mcp = FactoryMcpService(settings.DATABASE_URL, draft_processor=workflow)
    workflow.initialize()
    mcp.initialize()
    adapter = OuroborosTaskAdapter(
        base_url=settings.OUROBOROS_BASE_URL,
        lock_path=settings.CONTRACT_LOCK_PATH,
        skill_path=settings.SKILL_PATH,
    )
    return workflow, mcp, adapter


def _reference_brief(
    workflow: WorkflowStore,
    reference_id: str,
) -> tuple[str, CampaignBriefInput]:
    reference = next(
        (
            item
            for item in workflow.authoring_catalog().references
            if item.reference_id == reference_id
        ),
        None,
    )
    if reference is None:
        raise LiveAuthoringTransportError("editorial reference is unavailable")
    case_id = reference_id.removeprefix("editorial_").upper()
    brief = reference.brief
    if reference.custom_product is not None:
        request = CustomProductCreateRequest.model_validate(
            {
                **reference.custom_product,
                "synthetic_confirmed": True,
                "no_pii_confirmed": True,
            }
        )
        product = workflow.create_custom_product(request)
        brief = brief.model_copy(update={"product_id": product.product_id})
    return case_id, brief


def _providers(ledger: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted(
        {
            str(provider)
            for row in ledger.values()
            for provider in row.get("providers") or []
            if provider
        }
    )


def _models(ledger: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted(
        {
            normalize_provider_model(str(model))
            for row in ledger.values()
            for model in row.get("models") or []
            if model
        }
    )


def _content_checks(
    package: PackageView | None,
    context: ContextBundle,
) -> dict[str, bool]:
    if package is None:
        return {
            "sms_present": False,
            "email_present": False,
            "sms_cta_exact": False,
            "email_cta_exact": False,
            "no_internal_scaffolding": False,
        }
    sms = package.bundle.sms
    email = package.bundle.email
    surfaces = [
        sms.text if sms is not None else "",
        email.subject if email is not None else "",
        email.preheader if email is not None else "",
        email.headline if email is not None else "",
        email.plain_text if email is not None else "",
    ]
    if email is not None:
        surfaces.extend(section.body for section in email.sections)
    combined = " ".join(surfaces).casefold()
    expected_cta_url = str(context.brief_snapshot.cta_url)
    forbidden = (
        "system:",
        "content_plan",
        "context_version",
        "mcp_factory__",
        "<placeholder>",
    )
    return {
        "sms_present": sms is not None and bool(sms.text.strip()),
        "email_present": email is not None and 2 <= len(email.sections) <= 4,
        "sms_cta_exact": sms is not None and sms.cta_url == expected_cta_url,
        "email_cta_exact": email is not None
        and email.cta_url == expected_cta_url
        and email.cta_label == context.brief_snapshot.cta_label,
        "no_internal_scaffolding": all(marker not in combined for marker in forbidden),
    }


def _report(
    *,
    workflow: WorkflowStore,
    mcp: FactoryMcpService,
    adapter: OuroborosTaskAdapter,
    run_id: str,
    evaluation_id: str,
    qualification_attempt_id: str,
    reference_id: str,
    case_id: str,
    ledger_path: Path,
    elapsed_ms: int,
) -> dict[str, Any]:
    settings = get_settings()
    profile = provider_profile(settings.LIVE_PROVIDER_PROFILE)
    completed = workflow.get_run(run_id)
    task = adapter.task(completed.task_id) if completed.task_id is not None else {}
    events = (
        parse_sse_events(adapter.task_events_text(completed.task_id))
        if completed.task_id is not None
        else []
    )
    provider_ledger = provider_call_ledger(events)
    tool_receipts = observed_tool_names(events)
    snapshot = mcp.probe_snapshot(completed.campaign_id)
    raw_mcp_events = snapshot.get("events")
    mcp_events = list(raw_mcp_events) if isinstance(raw_mcp_events, list) else []
    audit_types = [
        str(item.get("event_type") or "") for item in mcp_events if isinstance(item, dict)
    ]
    package = (
        workflow.get_package(completed.package_id) if completed.package_id is not None else None
    )
    context = workflow.get_current_context(completed.campaign_id)
    physical_document = request_ledger.read_ledger(ledger_path)
    physical_rows = [
        dict(row)
        for row in physical_document.get("requests") or []
        if isinstance(row, dict) and row.get("case_id") == case_id
    ]
    task_in_queue = (
        queue_contains_task(adapter.tasks(), completed.task_id)
        if completed.task_id is not None
        else False
    )
    checks = {
        "run_completed_live": completed.status is RunStatus.COMPLETED
        and completed.mode == "live_ouroboros",
        "package_is_live": package is not None and package.mode == "live_ouroboros",
        "qa_approvable": package is not None and package.quality_report.approvable,
        "qa_has_no_findings": package is not None and not package.quality_report.findings,
        "qa_registry_complete": package is not None
        and len(package.quality_report.checked_ids) == 22,
        "initial_fact_placement_exact": package is not None
        and not initial_fact_placement_issues(package.bundle, context),
        "exact_two_tool_receipts": tool_receipts == ALLOWED_PROVIDER_TOOLS,
        "context_completed_once": audit_types.count("context_tool_completed") == 1,
        "draft_saved_once": audit_types.count("draft_saved") == 1,
        "one_persisted_draft": isinstance(snapshot.get("draft"), dict),
        "usage_complete": usage_is_complete(
            provider_ledger,
            expected_provider=profile.ledger_provider,
            require_post_task_summary=profile.require_post_task_summary,
        ),
        "provider_route_exact": _providers(provider_ledger) == [profile.ledger_provider],
        "model_route_exact": _models(provider_ledger) == [profile.normalized_model],
        "provider_retry_absent": int(
            (provider_ledger.get("provider_retry") or {}).get("call_count") or 0
        )
        == 0,
        "worker_released": completed.worker_released_at is not None and not task_in_queue,
        "single_managed_attempt": completed.physical_attempt_count == 1,
        "physical_requests_present": bool(physical_rows),
        "physical_requests_terminal": bool(physical_rows)
        and all(row.get("status") != "RESERVED" for row in physical_rows),
        **_content_checks(package, context),
    }
    mechanically_valid = all(checks.values())
    return {
        "schema_version": 1,
        "kind": "campaign_authoring_quality_case",
        "evaluation_id": evaluation_id,
        "qualification_attempt_id": qualification_attempt_id,
        "reference_id": reference_id,
        "case_id": case_id,
        "ok": mechanically_valid,
        "mechanically_valid": mechanically_valid,
        "elapsed_ms": elapsed_ms,
        "provider_profile": profile.name,
        "runtime_image_id": adapter.admit().runtime_image_id,
        "run": completed.model_dump(mode="json"),
        "task": {
            key: task.get(key)
            for key in (
                "task_id",
                "status",
                "reason_code",
                "total_rounds",
                "project_id",
                "memory_mode",
            )
        },
        "context": context.model_dump(mode="json"),
        "package": package.model_dump(mode="json") if package is not None else None,
        "provider_call_ledger": provider_ledger,
        "physical_request_rows": physical_rows,
        "tool_receipts": tool_receipts,
        "mcp": {
            "audit_event_types": audit_types,
            "authorization_attempts": mcp.authorization_attempts(completed.run_id),
        },
        "checks": checks,
    }


def run_reference(
    *,
    reference_id: str,
    evaluation_id: str,
    qualification_attempt_id: str,
    ledger_path: Path,
) -> dict[str, Any]:
    settings = get_settings()
    if settings.LIVE_PROVIDER_PROFILE != CAMPAIGN_AUTHORING_PROFILE_NAME:
        raise LiveAuthoringTransportError("campaign authoring profile is not active")
    profile = provider_profile(settings.LIVE_PROVIDER_PROFILE)
    ledger = request_ledger.read_ledger(ledger_path)
    if ledger.get("evaluation_id") != evaluation_id:
        raise LiveAuthoringTransportError("request ledger evaluation identity differs")
    workflow, mcp, adapter = _components()
    case_id, brief = _reference_brief(workflow, reference_id)

    def before_submit(run: Any, attempt: Any, payload: dict[str, Any]) -> None:
        del run, payload
        request_ledger.bind_task(
            ledger_path,
            task_id=attempt.task_id,
            case_id=case_id,
            attempt_id=qualification_attempt_id,
            request_digest=attempt.request_digest,
        )

    coordinator = RunCoordinator(
        store=workflow,
        mcp_service=mcp,
        adapter=adapter,
        task_timeout_seconds=profile.effective_task_timeout_seconds,
        terminal_deadline_seconds=profile.effective_terminal_deadline_seconds,
        usage_expected_provider=profile.ledger_provider,
        usage_require_post_task_summary=profile.require_post_task_summary,
        controlled_provider_retry_enabled=False,
        provider_identity=profile.runtime_provider,
        model_identity=profile.normalized_model,
        provider_profile=profile.name,
        before_task_submit=before_submit,
    )
    started = time.monotonic()
    try:
        campaign = workflow.create_campaign(brief=brief, case_id=None)
        validated = workflow.validate_campaign(campaign.campaign_id)
        if validated.state is not CampaignState.READY:
            raise LiveAuthoringTransportError("editorial reference did not promote to READY")
        accepted = coordinator.start_live(campaign.campaign_id)
        coordinator.wait(
            accepted.run_id,
            timeout=profile.effective_terminal_deadline_seconds + 10,
        )
        elapsed_ms = max(0, round((time.monotonic() - started) * 1_000))
        return _report(
            workflow=workflow,
            mcp=mcp,
            adapter=adapter,
            run_id=accepted.run_id,
            evaluation_id=evaluation_id,
            qualification_attempt_id=qualification_attempt_id,
            reference_id=reference_id,
            case_id=case_id,
            ledger_path=ledger_path,
            elapsed_ms=elapsed_ms,
        )
    finally:
        coordinator.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-id", choices=REFERENCE_IDS, required=True)
    parser.add_argument("--evaluation-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--ledger-path", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_reference(
            reference_id=args.reference_id,
            evaluation_id=args.evaluation_id,
            qualification_attempt_id=args.attempt_id,
            ledger_path=args.ledger_path,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "mechanically_valid": False,
                    "reference_id": args.reference_id,
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
