from __future__ import annotations

import json
import pathlib
from typing import Any, cast

import pytest

from apps.api.app.domain.campaigns import ContextBundle
from apps.api.app.domain.models import ContextGetRequest, DraftSaveRequest
from apps.api.app.domain.workflow import CampaignState, RunStatus
from apps.api.app.mcp.service import FactoryMcpService
from apps.api.app.ouroboros_client import TaskAdmission
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.services.deterministic import build_deterministic_bundle
from apps.api.app.workflow.runs import RunCoordinator
from apps.api.app.workflow.store import WorkflowConflict, WorkflowStore


class FakeTaskAdapter:
    def __init__(
        self,
        mcp: FactoryMcpService,
        *,
        behavior: str = "success",
        provider: str = "openai",
        include_post_task_summary: bool = True,
        include_unaccounted_provider_request: bool = False,
    ) -> None:
        self._mcp = mcp
        self._behavior = behavior
        self._provider = provider
        self._include_post_task_summary = include_post_task_summary
        self._include_unaccounted_provider_request = include_unaccounted_provider_request
        self._result: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._running = False
        self.payload: dict[str, Any] = {}

    def admit(self) -> TaskAdmission:
        return TaskAdmission(
            constraints="COMMUNICATION_FACTORY_CONTRACT_V1\nПроверенный тестовый контракт.",  # noqa: RUF001
            disabled_tools=["run_command", "web_search"],
            prompt_hash="a" * 64,
            skill_content_hash="b" * 64,
            tool_inventory_hash="c" * 64,
            activation_mode="adapter_injected",
            runtime_image_id=f"sha256:{'d' * 64}",
        )

    def submit_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payload = payload
        task_id = str(payload["task_id"])
        if self._behavior == "hold":
            self._running = True
            self._result = {"status": "running", "task_id": task_id}
            return {"ok": True, "task_id": task_id}
        if self._behavior == "failed":
            self._result = {"status": "failed", "task_id": task_id}
            self._events = [{"type": "task_done", "data": {"status": "failed"}}]
            return {"ok": True, "task_id": task_id}
        if self._behavior == "failed_task_result":
            self._result = {"status": "failed", "task_id": task_id}
            self._events = [
                {
                    "type": "task_result",
                    "source": "task_result",
                    "data": {"status": "failed"},
                }
            ]
            return {"ok": True, "task_id": task_id}
        metadata = payload["metadata"]
        context_result = self._mcp.context_get(
            ContextGetRequest(
                campaign_id=metadata["campaign_id"],
                operation=metadata["operation"],
                iteration=metadata["iteration"],
                context_version=metadata["context_version"],
                idempotency_key=metadata["idempotency_key"],
            )
        )
        assert context_result.context_bundle is not None
        context = ContextBundle.model_validate(context_result.context_bundle)
        bundle = build_deterministic_bundle(context)
        saved = self._mcp.draft_save(
            DraftSaveRequest.model_validate(
                {
                    "campaign_id": metadata["campaign_id"],
                    "operation": metadata["operation"],
                    "iteration": metadata["iteration"],
                    "context_version": metadata["context_version"],
                    "idempotency_key": metadata["idempotency_key"],
                    "draft": {
                        "kind": "communication_bundle",
                        "schema_version": "1.0",
                        "campaign_id": metadata["campaign_id"],
                        "operation": metadata["operation"],
                        "iteration": metadata["iteration"],
                        "context_version": metadata["context_version"],
                        "payload": bundle.model_dump(mode="json"),
                    },
                }
            )
        )
        assert saved.persisted is True
        self._result = {
            "status": "completed",
            "task_id": task_id,
            "final_answer": json.dumps({"status": "SAVED", "draft_id": saved.draft_id}),
        }
        self._events = [self._tool_event("mcp_factory__cf_context_get", "one")]
        if self._provider == "openrouter":
            self._events.extend(self._provider_correlation("main_generation", "main"))
        self._events.extend(
            [
                self._usage_event("task", 120),
                self._tool_event("mcp_factory__cf_draft_save", "two"),
            ]
        )
        if self._include_post_task_summary:
            if self._provider == "openrouter":
                self._events.extend(self._provider_correlation("post_task_summary", "summary"))
            self._events.append(self._usage_event("post_task_summary", 30))
        if self._include_unaccounted_provider_request:
            self._events.extend(
                [
                    {
                        "type": "provider_request_headers",
                        "source": "events",
                        "data": {
                            "category": "main_generation",
                            "provider_call_id": "cf_provider_rejected",
                            "generation_id": None,
                        },
                    },
                    {
                        "type": "provider_request_terminal",
                        "source": "events",
                        "data": {
                            "category": "main_generation",
                            "provider_call_id": "cf_provider_rejected",
                            "generation_ids": [],
                            "status": "failed",
                            "usage_observed": False,
                            "physical_response_count": 1,
                        },
                    },
                ]
            )
        self._events.append({"type": "task_done", "data": {"status": "completed"}})
        return {"ok": True, "task_id": task_id}

    def task(self, task_id: str) -> dict[str, Any]:
        return self._result

    def tasks(self) -> dict[str, Any]:
        rows = [{"id": self.payload.get("task_id")}] if self._running else []
        return {"queue": {"running": rows, "pending": []}}

    def cancel_task(self, task_id: str) -> None:
        self._running = False
        self._result = {"status": "cancelled", "task_id": task_id}
        self._events = [{"type": "task_done", "data": {"status": "cancelled"}}]

    def task_events_text(self, task_id: str) -> str:
        return "".join(f"data: {json.dumps(event)}\n\n" for event in self._events)

    @staticmethod
    def _tool_event(name: str, timestamp: str) -> dict[str, Any]:
        return {
            "type": "tool_completed",
            "source": "tools",
            "data": {"tool": name, "ts": timestamp, "args": {}},
        }

    def _usage_event(self, category: str, prompt_tokens: int) -> dict[str, Any]:
        return {
            "type": "llm_usage",
            "source": "events",
            "data": {
                "category": category,
                "provider": self._provider,
                "model": "z-ai/glm-5.2" if self._provider == "openrouter" else "gpt-5.4-mini",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 10,
                "cost": 0.001,
                "ts": "2026-07-11T00:00:00+00:00",
            },
        }

    @staticmethod
    def _provider_correlation(category: str, suffix: str) -> list[dict[str, Any]]:
        call_id = f"cf_provider_{suffix}"
        generation_id = f"gen-{suffix}-12345678"
        return [
            {
                "type": "provider_request_headers",
                "source": "events",
                "data": {
                    "category": category,
                    "provider_call_id": call_id,
                    "generation_id": generation_id,
                },
            },
            {
                "type": "provider_request_terminal",
                "source": "events",
                "data": {
                    "category": category,
                    "provider_call_id": call_id,
                    "generation_ids": [generation_id],
                    "status": "completed",
                    "usage_observed": True,
                    "physical_response_count": 1,
                },
            },
        ]


def _ready_services(
    tmp_path: pathlib.Path,
    *,
    behavior: str = "success",
    provider: str = "openai",
    include_post_task_summary: bool = True,
    include_unaccounted_provider_request: bool = False,
) -> tuple[WorkflowStore, FactoryMcpService, FakeTaskAdapter, str]:
    database_url = f"sqlite:///{tmp_path / 'factory.db'}"
    store = WorkflowStore(
        database_url,
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "artifacts",
    )
    mcp = FactoryMcpService(database_url, draft_processor=store)
    store.initialize()
    mcp.initialize()
    campaign = store.create_campaign(brief=None, case_id="B04")
    ready = store.validate_campaign(campaign.campaign_id)
    assert ready.state is CampaignState.READY
    adapter = FakeTaskAdapter(
        mcp,
        behavior=behavior,
        provider=provider,
        include_post_task_summary=include_post_task_summary,
        include_unaccounted_provider_request=include_unaccounted_provider_request,
    )
    return store, mcp, adapter, campaign.campaign_id


def test_successful_managed_run_persists_live_package_usage_and_monotonic_events(
    tmp_path: pathlib.Path,
) -> None:
    store, mcp, adapter, campaign_id = _ready_services(tmp_path)
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        terminal_deadline_seconds=2,
        poll_interval_seconds=0.01,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(accepted.run_id, timeout=3)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED
    assert completed.mode == "live_ouroboros"
    assert completed.reason_code is None
    assert completed.package_id is not None
    assert completed.tool_receipts == (
        "mcp_factory__cf_context_get",
        "mcp_factory__cf_draft_save",
    )
    assert completed.provider_call_ledger["main_generation"]["call_count"] == 1  # type: ignore[index]
    assert store.get_package(completed.package_id).mode == "live_ouroboros"
    assert adapter.payload["memory_mode"] == "forked"
    assert "operation-" in adapter.payload["description"]
    assert adapter.payload["timeout_sec"] == 25
    events = store.run_events(completed.run_id)
    assert [event.event_id for event in events] == sorted(event.event_id for event in events)
    assert {event.event_type for event in events}.issuperset(
        {"run.accepted", "run.started", "run.task_bound", "run.qa_completed", "run.terminal"}
    )


def test_task_binding_callback_runs_before_physical_task_submission(
    tmp_path: pathlib.Path,
) -> None:
    store, mcp, adapter, campaign_id = _ready_services(tmp_path)
    observed: list[tuple[str, str, str]] = []

    def before_submit(run: Any, attempt: Any, payload: dict[str, Any]) -> None:
        assert adapter.payload == {}
        observed.append((run.run_id, attempt.task_id, str(payload["task_id"])))

    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        terminal_deadline_seconds=2,
        poll_interval_seconds=0.01,
        before_task_submit=before_submit,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(accepted.run_id, timeout=3)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED
    assert observed == [(accepted.run_id, completed.task_id, completed.task_id)]


def test_openrouter_run_accepts_complete_usage_when_summary_is_disabled(
    tmp_path: pathlib.Path,
) -> None:
    store, mcp, adapter, campaign_id = _ready_services(
        tmp_path,
        provider="openrouter",
        include_post_task_summary=False,
    )
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        terminal_deadline_seconds=2,
        usage_expected_provider="openrouter",
        usage_require_post_task_summary=False,
        poll_interval_seconds=0.01,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(accepted.run_id, timeout=3)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED
    assert completed.mode == "live_ouroboros"
    assert completed.reason_code is None
    assert completed.package_id is not None
    assert store.get_package(completed.package_id).mode == "live_ouroboros"
    assert completed.provider_call_ledger["main_generation"]["providers"] == [  # type: ignore[index]
        "openrouter"
    ]
    assert completed.provider_call_ledger["post_task_summary"]["call_count"] == 0  # type: ignore[index]


def test_pre_generation_no_id_anomaly_preserves_existing_live_result(
    tmp_path: pathlib.Path,
) -> None:
    store, mcp, adapter, campaign_id = _ready_services(
        tmp_path,
        provider="openrouter",
        include_post_task_summary=False,
        include_unaccounted_provider_request=True,
    )
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        terminal_deadline_seconds=2,
        usage_expected_provider="openrouter",
        usage_require_post_task_summary=False,
        poll_interval_seconds=0.01,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(accepted.run_id, timeout=3)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED
    assert completed.mode == "live_ouroboros"
    assert completed.reason_code is None
    assert completed.worker_released_at is not None
    assert completed.package_id is not None
    assert store.get_package(completed.package_id).mode == "live_ouroboros"
    main = cast(dict[str, Any], completed.provider_call_ledger["main_generation"])
    assert main["provider_request_count"] == 1
    assert main["provider_request_completed_count"] == 1


def test_confirmed_live_failure_falls_back_only_after_worker_release(
    tmp_path: pathlib.Path,
) -> None:
    store, mcp, adapter, campaign_id = _ready_services(tmp_path, behavior="failed")
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        terminal_deadline_seconds=2,
        poll_interval_seconds=0.01,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(accepted.run_id, timeout=3)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED_FALLBACK
    assert completed.mode == "deterministic_template"
    assert completed.reason_code == "LIVE_TASK_FAILED"
    assert completed.worker_released_at is not None
    assert completed.package_id is not None
    assert store.get_package(completed.package_id).mode == "deterministic_template"


def test_pinned_task_result_event_confirms_worker_release_without_legacy_task_done(
    tmp_path: pathlib.Path,
) -> None:
    store, mcp, adapter, campaign_id = _ready_services(tmp_path, behavior="failed_task_result")
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        terminal_deadline_seconds=2,
        poll_interval_seconds=0.01,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(accepted.run_id, timeout=3)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED_FALLBACK
    assert completed.reason_code == "LIVE_TASK_FAILED"
    assert completed.worker_released_at is not None


def test_cancelled_worker_is_reconciled_before_deterministic_fallback(
    tmp_path: pathlib.Path,
) -> None:
    store, mcp, adapter, campaign_id = _ready_services(tmp_path, behavior="hold")
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        terminal_deadline_seconds=2,
        poll_interval_seconds=0.01,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        coordinator.cancel(accepted.run_id)
        completed = coordinator.wait(accepted.run_id, timeout=3)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED_FALLBACK
    assert completed.reason_code == "LIVE_TASK_CANCELLED"
    events = store.run_events(completed.run_id)
    terminal = next(event for event in events if event.event_type == "run.terminal")
    released = next(event for event in events if event.data.get("stage") == "worker_released")
    assert terminal.event_id < released.event_id


def test_deadline_cancel_is_reobserved_before_release_is_declared_unconfirmed(
    tmp_path: pathlib.Path,
) -> None:
    store, mcp, adapter, campaign_id = _ready_services(tmp_path, behavior="hold")
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        terminal_deadline_seconds=0.02,
        poll_interval_seconds=0.02,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(accepted.run_id, timeout=1)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED_FALLBACK
    assert completed.reason_code == "LIVE_TASK_CANCELLED"
    assert completed.worker_released_at is not None


def test_one_active_run_per_campaign_is_enforced_before_second_task_submission(
    tmp_path: pathlib.Path,
) -> None:
    store, mcp, adapter, campaign_id = _ready_services(tmp_path, behavior="hold")
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        terminal_deadline_seconds=2,
        poll_interval_seconds=0.01,
    )
    try:
        first = coordinator.start_live(campaign_id)
        with pytest.raises(WorkflowConflict, match="active run"):
            coordinator.start_live(campaign_id)
        coordinator.cancel(first.run_id)
        coordinator.wait(first.run_id, timeout=3)
    finally:
        coordinator.shutdown()


def test_startup_reconciler_resumes_stale_running_task_without_duplicate_submission(
    tmp_path: pathlib.Path,
) -> None:
    store, mcp, adapter, campaign_id = _ready_services(tmp_path, behavior="failed")
    context = store.get_current_context(campaign_id)
    admission = adapter.admit()
    run_id = "run_reconcile_0001"
    task_id = "task_reconcile_0001"
    project_id = "project_reconcile_0001"
    key = "operation-reconcile-0001"
    store.create_live_run(
        run_id=run_id,
        campaign_id=campaign_id,
        operation="initial",
        iteration=1,
        task_id=task_id,
        project_id=project_id,
        context_version=context.context_version,
        prompt_hash=admission.prompt_hash,
        skill_content_hash=admission.skill_content_hash,
        tool_inventory_hash=admission.tool_inventory_hash,
    )
    mcp.prepare_operation(
        run_id=run_id,
        task_id=task_id,
        project_id=project_id,
        campaign_id=campaign_id,
        operation="initial",
        iteration=1,
        idempotency_key=key,
        context=context.model_dump(mode="json"),
    )
    adapter.submit_task({"task_id": task_id})
    store.mark_run_started(run_id)
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        terminal_deadline_seconds=2,
        poll_interval_seconds=0.01,
    )
    try:
        coordinator.reconcile_active()
        completed = coordinator.wait(run_id, timeout=3)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED_FALLBACK
    assert completed.reason_code == "LIVE_TASK_FAILED"
    assert any(event.data.get("stage") == "startup_reconcile" for event in store.run_events(run_id))
