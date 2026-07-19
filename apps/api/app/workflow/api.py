from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from apps.api.app.domain.authoring import (
    AuthoringCatalogView,
    AuthoringProductView,
    CustomProductCreateRequest,
    RecentCampaignView,
)
from apps.api.app.domain.campaigns import CampaignBriefInput
from apps.api.app.domain.learning import (
    FeedbackCreateRequest,
    FeedbackView,
    PackageDiffView,
    RevisionStartRequest,
    RuleApprovalRequest,
    RuleProposalStartRequest,
    RuleProposalView,
    RuleRejectionRequest,
    RuleRollbackRequest,
    RuleVersionView,
)
from apps.api.app.domain.presentation import (
    DashboardView,
    DiagnosticComponent,
    DiagnosticsView,
    EvaluationRunSummary,
    EvaluationRunView,
    MvpResultsView,
    WorkspaceView,
)
from apps.api.app.domain.workflow import (
    ApprovalRecord,
    ApprovalRequest,
    CampaignCreateRequest,
    CampaignView,
    CaseView,
    DemoResetRequest,
    DemoResetResult,
    DeterministicRunRequest,
    ExportRecord,
    PackageView,
    RunStatus,
    RunView,
)
from apps.api.app.ouroboros_client import ALLOWED_PROVIDER_TOOLS, TaskAdmissionError
from apps.api.app.services.mvp_results import (
    MVP_ARTIFACT_FILES,
    MvpResultsCatalog,
    MvpResultsError,
)
from apps.api.app.settings import Settings
from apps.api.app.workflow.runs import RunCoordinator
from apps.api.app.workflow.store import (
    WorkflowConflict,
    WorkflowInvalidState,
    WorkflowNotFound,
    WorkflowStore,
)

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=16, max_length=128),
]
HumanActor = Annotated[
    str,
    Header(alias="X-CF-Actor", min_length=3, max_length=128),
]
HumanRole = Annotated[str, Header(alias="X-CF-Actor-Role")]


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, WorkflowNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, WorkflowConflict | WorkflowInvalidState):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise exc


def _idempotent(
    store: WorkflowStore,
    *,
    scope: str,
    key: str,
    payload: Any,
    operation: Any,
) -> dict[str, Any]:
    try:
        return store.execute_idempotent(
            scope=scope,
            key=key,
            payload=payload,
            operation=operation,
        )
    except (WorkflowNotFound, WorkflowConflict, WorkflowInvalidState) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable") from exc


def _runtime_diagnostics(store: WorkflowStore, settings: Settings) -> DiagnosticsView:
    lock: dict[str, Any] = {}
    try:
        candidate = json.loads(settings.CONTRACT_LOCK_PATH.read_text(encoding="utf-8"))
        if isinstance(candidate, dict) and candidate.get("schema_version") == 1:
            lock = candidate
    except (OSError, json.JSONDecodeError):
        pass
    runtime_value = lock.get("runtime")
    skill_value = lock.get("skill")
    tools_value = lock.get("tools")
    runtime: dict[str, Any] = (
        {str(key): value for key, value in runtime_value.items()}
        if isinstance(runtime_value, dict)
        else {}
    )
    skill: dict[str, Any] = (
        {str(key): value for key, value in skill_value.items()}
        if isinstance(skill_value, dict)
        else {}
    )
    tools: dict[str, Any] = (
        {str(key): value for key, value in tools_value.items()}
        if isinstance(tools_value, dict)
        else {}
    )
    provider_tools = tools.get("post_deny_tool_names")
    discovered_tools = (
        tuple(str(item) for item in provider_tools) if isinstance(provider_tools, list) else ()
    )
    tools_ready = list(discovered_tools) == ALLOWED_PROVIDER_TOOLS
    skill_ready = bool(skill.get("ready"))
    runtime_ready = bool(runtime.get("tag") and runtime.get("commit"))
    try:
        contract_generated_at = datetime.fromisoformat(str(lock.get("generated_at")))
    except ValueError:
        contract_generated_at = None
    active_runs = store.active_runs()
    components = (
        DiagnosticComponent(
            component_id="app",
            label="Приложение",
            status="READY",
            detail=(
                "Программный интерфейс, доменная база данных и каталог "
                "синтетических кейсов доступны."
            ),
        ),
        DiagnosticComponent(
            component_id="database",
            label="Доменная БД",
            status="READY",
            detail=f"SQLite отвечает; синтетических кейсов: {len(store.list_cases())}.",
        ),
        DiagnosticComponent(
            component_id="ouroboros_contract",
            label="Контракт Ouroboros",
            status="READY" if runtime_ready else "DEGRADED",
            detail=(
                "Закреплённая среда исполнения подтверждена контрактом развёртывания."
                if runtime_ready
                else "Контракт развёртывания недоступен; допуск к живой генерации закрыт."
            ),
        ),
        DiagnosticComponent(
            component_id="mcp",
            label="Изоляция MCP",
            status="READY" if tools_ready else "DEGRADED",
            detail=(
                "Lock подтверждает ровно два инструмента фабрики."
                if tools_ready
                else "Точный набор из двух инструментов фабрики не подтверждён."
            ),
        ),
        DiagnosticComponent(
            component_id="skill",
            label="Инструкция агента",
            status="READY" if skill_ready else "DEGRADED",
            detail=(
                "Хэш проверенной инструкции подтверждён."
                if skill_ready
                else "Контракт проверенной инструкции не подтверждён."
            ),
        ),
        DiagnosticComponent(
            component_id="provider",
            label="Граница провайдера",
            status="ISOLATED",
            detail=(
                "Ключ недоступен приложению и интерфейсу; маршрут к провайдеру "
                "принадлежит только Ouroboros."
            ),
        ),
    )
    return DiagnosticsView(
        generated_at=datetime.now(UTC),
        components=components,
        runtime_tag=str(runtime.get("tag")) if runtime.get("tag") else None,
        runtime_commit=str(runtime.get("commit")) if runtime.get("commit") else None,
        skill_hash=str(skill.get("skill_content_hash"))
        if skill.get("skill_content_hash")
        else None,
        prompt_hash=str(skill.get("prompt_hash")) if skill.get("prompt_hash") else None,
        tool_inventory_hash=str(tools.get("inventory_hash"))
        if tools.get("inventory_hash")
        else None,
        discovered_tools=discovered_tools,
        contract_generated_at=contract_generated_at,
        active_run_count=len(active_runs),
        queue_state="ACTIVE" if active_runs else "IDLE",
        admission_state="CLOSED" if runtime_ready and skill_ready and tools_ready else "OPEN",
        latest_errors=tuple(store.latest_run_errors()),
    )


def build_workflow_router(
    store: WorkflowStore,
    coordinator: RunCoordinator,
    settings: Settings,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    mvp_results = MvpResultsCatalog(settings.MVP_REPORT_DIR)

    @router.get("/ready")
    async def ready() -> dict[str, Any]:
        return {
            "status": "ready",
            "database": "ready",
            "synthetic_case_count": len(store.list_cases()),
            "external_send_enabled": False,
        }

    @router.get("/config/public")
    async def public_config() -> dict[str, Any]:
        return {
            "data_mode": "synthetic_only",
            "external_send_enabled": False,
            "approval_requires_human_session": True,
            "runtime_modes": ["deterministic_template", "live_ouroboros", "replay"],
            "default_execution_mode": settings.DEFAULT_EXECUTION_MODE,
            "human_actions_test_only": settings.HUMAN_ACTIONS_TEST_ONLY,
            "demo_reset_enabled": settings.DEMO_RESET_ENABLED,
            "session_auth_enabled": settings.SESSION_AUTH_ENABLED,
        }

    @router.post("/admin/demo-reset", response_model=DemoResetResult)
    async def reset_demo(
        request: DemoResetRequest,
        idempotency_key: IdempotencyKey,
        actor_id: HumanActor,
        actor_role: HumanRole,
    ) -> dict[str, Any]:
        if not settings.DEMO_RESET_ENABLED:
            raise HTTPException(status_code=404, detail="demo reset is not enabled")
        if actor_role != "human":
            raise HTTPException(status_code=403, detail="human web session is required")
        try:
            return _idempotent(
                store,
                scope="POST:/api/v1/admin/demo-reset",
                key=idempotency_key,
                payload={"confirmation": request.confirmation, "actor_id": actor_id},
                operation=coordinator.reset_demo_state,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/cases", response_model=list[CaseView])
    async def cases() -> list[CaseView]:
        return store.list_cases()

    @router.get("/dashboard", response_model=DashboardView)
    async def dashboard() -> DashboardView:
        return store.dashboard()

    @router.get("/authoring/catalog", response_model=AuthoringCatalogView)
    async def authoring_catalog() -> AuthoringCatalogView:
        return store.authoring_catalog()

    @router.post(
        "/authoring/products",
        response_model=AuthoringProductView,
        status_code=201,
    )
    async def create_authoring_product(
        request: CustomProductCreateRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _idempotent(
            store,
            scope="POST:/api/v1/authoring/products",
            key=idempotency_key,
            payload=request.model_dump(mode="json"),
            operation=lambda: store.create_custom_product(request),
        )

    @router.get("/campaigns", response_model=list[RecentCampaignView])
    async def recent_campaigns() -> list[RecentCampaignView]:
        return store.recent_authoring_campaigns()

    @router.post("/campaigns", response_model=CampaignView, status_code=201)
    async def create_campaign(
        request: CampaignCreateRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _idempotent(
            store,
            scope="POST:/api/v1/campaigns",
            key=idempotency_key,
            payload=request.model_dump(mode="json"),
            operation=lambda: store.create_campaign(brief=request.brief, case_id=request.case_id),
        )

    @router.get("/campaigns/{campaign_id}", response_model=CampaignView)
    async def campaign(campaign_id: str) -> CampaignView:
        try:
            return store.get_campaign(campaign_id)
        except (WorkflowNotFound, WorkflowConflict, WorkflowInvalidState) as exc:
            _raise_http(exc)
            raise AssertionError("unreachable") from exc

    @router.get("/campaigns/{campaign_id}/workspace", response_model=WorkspaceView)
    async def campaign_workspace(campaign_id: str) -> WorkspaceView:
        try:
            return store.workspace(campaign_id)
        except (WorkflowNotFound, WorkflowConflict, WorkflowInvalidState) as exc:
            _raise_http(exc)
            raise AssertionError("unreachable") from exc

    @router.patch("/campaigns/{campaign_id}/brief", response_model=CampaignView)
    async def patch_brief(
        campaign_id: str,
        request: CampaignBriefInput,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        fields_set = set(request.model_fields_set)
        return _idempotent(
            store,
            scope=f"PATCH:/api/v1/campaigns/{campaign_id}/brief",
            key=idempotency_key,
            payload=request.model_dump(mode="json", include=fields_set),
            operation=lambda: store.patch_brief(
                campaign_id,
                request,
                fields_set=fields_set,
            ),
        )

    @router.post("/campaigns/{campaign_id}/validate", response_model=CampaignView)
    async def validate_campaign(
        campaign_id: str,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _idempotent(
            store,
            scope=f"POST:/api/v1/campaigns/{campaign_id}/validate",
            key=idempotency_key,
            payload={"campaign_id": campaign_id},
            operation=lambda: store.validate_campaign(campaign_id),
        )

    @router.post("/campaigns/{campaign_id}/answers", response_model=CampaignView)
    async def answer_campaign(
        campaign_id: str,
        request: CampaignBriefInput,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        fields_set = set(request.model_fields_set)

        def apply_answers() -> CampaignView:
            store.patch_brief(campaign_id, request, fields_set=fields_set)
            return store.validate_campaign(campaign_id)

        return _idempotent(
            store,
            scope=f"POST:/api/v1/campaigns/{campaign_id}/answers",
            key=idempotency_key,
            payload=request.model_dump(mode="json", include=fields_set),
            operation=apply_answers,
        )

    @router.post(
        "/campaigns/{campaign_id}/runs",
        response_model=PackageView | RunView,
        status_code=201,
    )
    async def deterministic_run(
        campaign_id: str,
        request: DeterministicRunRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        def start_run() -> PackageView | RunView:
            if request.mode == "live_ouroboros":
                return coordinator.start_live(campaign_id)
            return store.run_deterministic(campaign_id)

        try:
            return _idempotent(
                store,
                scope=f"POST:/api/v1/campaigns/{campaign_id}/runs",
                key=idempotency_key,
                payload=request.model_dump(mode="json"),
                operation=start_run,
            )
        except TaskAdmissionError as exc:
            raise HTTPException(
                status_code=503,
                detail="Управляемая среда исполнения недоступна",
            ) from exc

    @router.get("/runs/{run_id}", response_model=RunView)
    async def run(run_id: str) -> RunView:
        try:
            return store.get_run(run_id)
        except (WorkflowNotFound, WorkflowConflict, WorkflowInvalidState) as exc:
            _raise_http(exc)
            raise AssertionError("unreachable") from exc

    @router.get("/runs/{run_id}/events", response_class=StreamingResponse)
    async def run_events(
        run_id: str,
        last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        try:
            store.get_run(run_id)
        except (WorkflowNotFound, WorkflowConflict, WorkflowInvalidState) as exc:
            _raise_http(exc)
            raise AssertionError("unreachable") from exc

        async def stream() -> AsyncIterator[str]:
            cursor = max(0, last_event_id or 0)
            elapsed = 0.0
            heartbeat_elapsed = 0.0
            interval = 0.1
            while elapsed < coordinator.max_logical_duration_seconds + 6.0:
                events = store.run_events(run_id, after_id=cursor)
                for event in events:
                    cursor = event.event_id
                    payload = json.dumps(
                        {
                            "event_id": event.event_id,
                            "run_id": event.run_id,
                            "type": event.event_type,
                            "data": event.data,
                            "created_at": event.created_at.isoformat(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    yield f"id: {event.event_id}\nevent: {event.event_type}\ndata: {payload}\n\n"
                current = store.get_run(run_id)
                if current.status not in {
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                    RunStatus.CANCEL_REQUESTED,
                } and not store.run_events(run_id, after_id=cursor):
                    break
                if heartbeat_elapsed >= 1.0:
                    yield ": heartbeat\n\n"
                    heartbeat_elapsed = 0.0
                await asyncio.sleep(interval)
                elapsed += interval
                heartbeat_elapsed += interval

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/runs/{run_id}/cancel", response_model=RunView)
    async def cancel_run(
        run_id: str,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _idempotent(
            store,
            scope=f"POST:/api/v1/runs/{run_id}/cancel",
            key=idempotency_key,
            payload={"run_id": run_id},
            operation=lambda: coordinator.cancel(run_id),
        )

    @router.get("/packages/{package_id}", response_model=PackageView)
    async def package(package_id: str) -> PackageView:
        try:
            return store.get_package(package_id)
        except (WorkflowNotFound, WorkflowConflict, WorkflowInvalidState) as exc:
            _raise_http(exc)
            raise AssertionError("unreachable") from exc

    @router.post("/packages/{package_id}/feedback", response_model=FeedbackView, status_code=201)
    async def create_feedback(
        package_id: str,
        request: FeedbackCreateRequest,
        idempotency_key: IdempotencyKey,
        actor_id: HumanActor,
        actor_role: HumanRole,
    ) -> dict[str, Any]:
        if actor_role != "human":
            raise HTTPException(status_code=403, detail="human web session is required")
        return _idempotent(
            store,
            scope=f"POST:/api/v1/packages/{package_id}/feedback",
            key=idempotency_key,
            payload={"request": request.model_dump(mode="json"), "actor_id": actor_id},
            operation=lambda: store.create_feedback(package_id, request, author_id=actor_id),
        )

    @router.post(
        "/packages/{package_id}/revision",
        response_model=PackageView | RunView,
        status_code=201,
    )
    async def revise_package(
        package_id: str,
        request: RevisionStartRequest,
        idempotency_key: IdempotencyKey,
        actor_id: HumanActor,
        actor_role: HumanRole,
    ) -> dict[str, Any]:
        if actor_role != "human":
            raise HTTPException(status_code=403, detail="human web session is required")

        def start_revision() -> PackageView | RunView:
            if request.mode == "live_ouroboros":
                context = store.prepare_revision_context(package_id, request.feedback_id)
                return coordinator.start_live(context.brief_snapshot.campaign_id)
            return store.run_deterministic_revision(package_id, request.feedback_id)

        return _idempotent(
            store,
            scope=f"POST:/api/v1/packages/{package_id}/revision",
            key=idempotency_key,
            payload={"request": request.model_dump(mode="json"), "actor_id": actor_id},
            operation=start_revision,
        )

    @router.get("/packages/{package_id}/diff", response_model=PackageDiffView)
    async def package_diff(package_id: str) -> PackageDiffView:
        try:
            return store.get_package_diff(package_id)
        except (WorkflowNotFound, WorkflowConflict, WorkflowInvalidState) as exc:
            _raise_http(exc)
            raise AssertionError("unreachable") from exc

    @router.post(
        "/feedback/{feedback_id}/rule-proposals",
        response_model=RuleProposalView | RunView,
        status_code=201,
    )
    async def create_rule_proposal(
        feedback_id: str,
        request: RuleProposalStartRequest,
        idempotency_key: IdempotencyKey,
        actor_id: HumanActor,
        actor_role: HumanRole,
    ) -> dict[str, Any]:
        if actor_role != "human":
            raise HTTPException(status_code=403, detail="human web session is required")

        def start_proposal() -> RuleProposalView | RunView:
            if request.mode == "live_ouroboros":
                context = store.prepare_rule_proposal_context(feedback_id, request.selected_scope)
                return coordinator.start_live(context.brief_snapshot.campaign_id)
            return store.run_deterministic_rule_proposal(feedback_id, request.selected_scope)

        return _idempotent(
            store,
            scope=f"POST:/api/v1/feedback/{feedback_id}/rule-proposals",
            key=idempotency_key,
            payload={"request": request.model_dump(mode="json"), "actor_id": actor_id},
            operation=start_proposal,
        )

    @router.get("/rule-proposals/{proposal_id}", response_model=RuleProposalView)
    async def rule_proposal(proposal_id: str) -> RuleProposalView:
        try:
            return store.get_rule_proposal(proposal_id)
        except (WorkflowNotFound, WorkflowConflict, WorkflowInvalidState) as exc:
            _raise_http(exc)
            raise AssertionError("unreachable") from exc

    @router.post("/rule-proposals/{proposal_id}/approve", response_model=RuleVersionView)
    async def approve_rule_proposal(
        proposal_id: str,
        request: RuleApprovalRequest,
        idempotency_key: IdempotencyKey,
        actor_id: HumanActor,
        actor_role: HumanRole,
    ) -> dict[str, Any]:
        if actor_role != "human":
            raise HTTPException(status_code=403, detail="human web session is required")
        return _idempotent(
            store,
            scope=f"POST:/api/v1/rule-proposals/{proposal_id}/approve",
            key=idempotency_key,
            payload={"request": request.model_dump(mode="json"), "actor_id": actor_id},
            operation=lambda: store.approve_rule_proposal(proposal_id, request, actor_id=actor_id),
        )

    @router.post("/rule-proposals/{proposal_id}/reject", response_model=RuleProposalView)
    async def reject_rule_proposal(
        proposal_id: str,
        request: RuleRejectionRequest,
        idempotency_key: IdempotencyKey,
        actor_id: HumanActor,
        actor_role: HumanRole,
    ) -> dict[str, Any]:
        if actor_role != "human":
            raise HTTPException(status_code=403, detail="human web session is required")
        return _idempotent(
            store,
            scope=f"POST:/api/v1/rule-proposals/{proposal_id}/reject",
            key=idempotency_key,
            payload={"request": request.model_dump(mode="json"), "actor_id": actor_id},
            operation=lambda: store.reject_rule_proposal(proposal_id, request, actor_id=actor_id),
        )

    @router.post("/rules/{rule_version_id}/rollback", response_model=RuleVersionView)
    async def rollback_rule(
        rule_version_id: str,
        request: RuleRollbackRequest,
        idempotency_key: IdempotencyKey,
        actor_id: HumanActor,
        actor_role: HumanRole,
    ) -> dict[str, Any]:
        if actor_role != "human":
            raise HTTPException(status_code=403, detail="human web session is required")
        return _idempotent(
            store,
            scope=f"POST:/api/v1/rules/{rule_version_id}/rollback",
            key=idempotency_key,
            payload={"request": request.model_dump(mode="json"), "actor_id": actor_id},
            operation=lambda: store.rollback_rule(rule_version_id, request, actor_id=actor_id),
        )

    @router.post("/packages/{package_id}/approve", response_model=ApprovalRecord)
    async def approve_package(
        package_id: str,
        request: ApprovalRequest,
        idempotency_key: IdempotencyKey,
        actor_id: HumanActor,
        actor_role: HumanRole,
    ) -> dict[str, Any]:
        if actor_role != "human":
            raise HTTPException(status_code=403, detail="human web session is required")
        return _idempotent(
            store,
            scope=f"POST:/api/v1/packages/{package_id}/approve",
            key=idempotency_key,
            payload={
                "request": request.model_dump(mode="json"),
                "actor_id": actor_id,
                "actor_role": actor_role,
            },
            operation=lambda: store.approve_package(
                package_id,
                request,
                actor_id=actor_id,
            ),
        )

    @router.post("/packages/{package_id}/export", response_model=ExportRecord, status_code=201)
    async def export_package(
        package_id: str,
        idempotency_key: IdempotencyKey,
        actor_id: HumanActor,
        actor_role: HumanRole,
    ) -> dict[str, Any]:
        if actor_role != "human":
            raise HTTPException(status_code=403, detail="human web session is required")
        return _idempotent(
            store,
            scope=f"POST:/api/v1/packages/{package_id}/export",
            key=idempotency_key,
            payload={"package_id": package_id, "actor_id": actor_id},
            operation=lambda: store.export_package(package_id),
        )

    @router.get("/exports/{export_id}", response_model=ExportRecord)
    async def export(export_id: str) -> ExportRecord:
        try:
            return store.get_export(export_id)
        except (WorkflowNotFound, WorkflowConflict, WorkflowInvalidState) as exc:
            _raise_http(exc)
            raise AssertionError("unreachable") from exc

    @router.get("/exports/{export_id}/download", response_class=FileResponse)
    async def download_export(export_id: str) -> FileResponse:
        try:
            path = store.export_path(export_id)
        except (WorkflowNotFound, WorkflowConflict, WorkflowInvalidState) as exc:
            _raise_http(exc)
            raise AssertionError("unreachable") from exc
        return FileResponse(
            path,
            media_type="application/zip",
            filename=f"{export_id}.zip",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/evaluation/runs", response_model=list[EvaluationRunSummary])
    async def evaluation_runs() -> list[EvaluationRunSummary]:
        return store.evaluation_runs()

    @router.get("/evaluation/runs/{evaluation_id}", response_model=EvaluationRunView)
    async def evaluation_run(evaluation_id: str) -> EvaluationRunView:
        try:
            return store.evaluation_run(evaluation_id)
        except WorkflowNotFound as exc:
            raise HTTPException(status_code=404, detail="evaluation run was not found") from exc

    @router.get("/evaluation/artifacts/{evaluation_id}/{filename}", response_class=FileResponse)
    async def evaluation_artifact(evaluation_id: str, filename: str) -> FileResponse:
        try:
            path, media_type = store.evaluation_artifact(evaluation_id, filename)
        except WorkflowNotFound as exc:
            raise HTTPException(
                status_code=404, detail="evaluation artifact was not found"
            ) from exc
        return FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/results/mvp", response_model=MvpResultsView)
    async def mvp_result_view() -> MvpResultsView:
        try:
            return mvp_results.view()
        except MvpResultsError as exc:
            raise HTTPException(
                status_code=503,
                detail="подтверждённые результаты временно недоступны",
            ) from exc

    @router.get("/results/mvp/artifacts/{filename}", response_class=FileResponse)
    async def mvp_result_artifact(filename: str) -> FileResponse:
        if filename not in MVP_ARTIFACT_FILES:
            raise HTTPException(status_code=404, detail="файл отчёта не найден")
        try:
            path, media_type = mvp_results.artifact(filename)
        except MvpResultsError as exc:
            raise HTTPException(
                status_code=503,
                detail="подтверждённые результаты временно недоступны",
            ) from exc
        return FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/diagnostics", response_model=DiagnosticsView)
    async def diagnostics() -> DiagnosticsView:
        return _runtime_diagnostics(store, settings)

    return router
