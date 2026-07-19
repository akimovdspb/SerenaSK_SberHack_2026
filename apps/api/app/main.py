from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from apps.api.app.mcp.server import BearerAuthMiddleware, create_mcp_server
from apps.api.app.mcp.service import FactoryMcpService
from apps.api.app.ouroboros_client import OuroborosTaskAdapter
from apps.api.app.settings import Settings, get_settings
from apps.api.app.workflow.api import build_workflow_router
from apps.api.app.workflow.fault_adapter import ControlledRetryFaultAdapter
from apps.api.app.workflow.runs import ManagedTaskAdapter, RunCoordinator
from apps.api.app.workflow.store import WorkflowStore
from provider_profiles import provider_profile


def create_app(
    settings: Settings | None = None,
    *,
    task_adapter_factory: Callable[[FactoryMcpService], ManagedTaskAdapter] | None = None,
) -> FastAPI:
    effective = settings or get_settings()
    live_profile = provider_profile(effective.LIVE_PROVIDER_PROFILE)
    service = FactoryMcpService(effective.DATABASE_URL)
    workflow = WorkflowStore(
        effective.DATABASE_URL,
        data_dir=effective.SYNTHETIC_DATA_DIR,
        artifacts_dir=effective.ARTIFACTS_DIR,
        evidence_dir=effective.EVIDENCE_DIR,
    )
    service.set_draft_processor(workflow)
    mcp_server = create_mcp_server(service)
    if task_adapter_factory is not None:
        task_adapter = task_adapter_factory(service)
    elif effective.CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE != "none":
        task_adapter = ControlledRetryFaultAdapter(
            service,
            profile=effective.CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE,
            provider=live_profile.runtime_provider,
            model=live_profile.normalized_model,
            include_post_task_summary=live_profile.require_post_task_summary,
        )
    else:
        task_adapter = OuroborosTaskAdapter(
            base_url=effective.OUROBOROS_BASE_URL,
            lock_path=effective.CONTRACT_LOCK_PATH,
            skill_path=effective.SKILL_PATH,
            expected_identity_kind=effective.RUNTIME_CONTRACT_IDENTITY_KIND,
            expected_runtime_identity=effective.RUNTIME_CONTRACT_IDENTITY,
        )
    coordinator = RunCoordinator(
        store=workflow,
        mcp_service=service,
        adapter=task_adapter,
        task_timeout_seconds=effective.LIVE_TASK_TIMEOUT_SECONDS,
        terminal_deadline_seconds=effective.LIVE_RUN_TERMINAL_DEADLINE_SECONDS,
        usage_expected_provider=effective.LIVE_USAGE_EXPECTED_PROVIDER,
        usage_require_post_task_summary=effective.LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY,
        controlled_provider_retry_enabled=effective.CONTROLLED_PROVIDER_RETRY_ENABLED,
        provider_identity=live_profile.runtime_provider,
        model_identity=live_profile.normalized_model,
        provider_profile=live_profile.name,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        service.initialize()
        workflow.initialize()
        effective.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        async with mcp_server.session_manager.run():
            coordinator.reconcile_active()
            try:
                yield
            finally:
                coordinator.shutdown()

    app = FastAPI(
        title="Communication Factory API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        ready = effective.RUNTIME_READY_PATH is None or effective.RUNTIME_READY_PATH.is_file()
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "starting"},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "environment": effective.APP_ENV,
            "data_mode": "synthetic_only",
            "external_send_enabled": False,
        }

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Any, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "detail": str(exc)},
            headers={"Cache-Control": "no-store"},
        )

    app.include_router(build_workflow_router(workflow, coordinator, effective))

    mcp_app = BearerAuthMiddleware(
        mcp_server.streamable_http_app(),
        token=effective.MCP_SHARED_TOKEN.get_secret_value(),
        max_payload_bytes=effective.MCP_MAX_PAYLOAD_BYTES,
    )
    app.mount("/", mcp_app)
    return app
