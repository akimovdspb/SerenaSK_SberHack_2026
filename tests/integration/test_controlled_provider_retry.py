from __future__ import annotations

import hashlib
import json
import pathlib
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.app.domain.campaigns import ContextBundle
from apps.api.app.domain.models import ContextGetRequest, DraftSaveRequest
from apps.api.app.domain.workflow import (
    ApprovalDecision,
    ApprovalRequest,
    CampaignState,
    RunAttemptStatus,
    RunStatus,
)
from apps.api.app.main import create_app
from apps.api.app.mcp.service import FactoryMcpService
from apps.api.app.ouroboros_client import (
    ManagedTaskTransportError,
    TaskAdmission,
    TaskAdmissionError,
    TaskTransportFailure,
    build_campaign_task,
)
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.services.deterministic import build_deterministic_bundle
from apps.api.app.settings import Settings
from apps.api.app.workflow.retry import AttemptOutcome
from apps.api.app.workflow.runs import RunCoordinator, campaign_request_digest
from apps.api.app.workflow.store import WorkflowStore


class ScriptedTaskAdapter:
    def __init__(self, mcp: FactoryMcpService, steps: list[str]) -> None:
        self._mcp = mcp
        self._steps = steps
        self.payloads: list[dict[str, Any]] = []
        self._tasks: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._queued: set[str] = set()
        self._step_by_task: dict[str, str] = {}
        self.admission_drift: str | None = None
        self.transient_admission_failures = 0

    def admit(self) -> TaskAdmission:
        admission = TaskAdmission(
            constraints="COMMUNICATION_FACTORY_CONTRACT_V1\nКонтракт fault-теста.",  # noqa: RUF001
            disabled_tools=["run_command", "web_search"],
            prompt_hash="a" * 64,
            skill_content_hash="b" * 64,
            tool_inventory_hash="c" * 64,
            activation_mode="adapter_injected",
            runtime_image_id=f"sha256:{'d' * 64}",
        )
        if not self.payloads or self.admission_drift is None:
            if self.payloads and self.transient_admission_failures > 0:
                self.transient_admission_failures -= 1
                raise TaskAdmissionError("runtime readiness or mode profile drifted")
            return admission
        if self.admission_drift == "prompt_hash":
            return replace(admission, prompt_hash="e" * 64)
        if self.admission_drift == "skill_content_hash":
            return replace(admission, skill_content_hash="e" * 64)
        if self.admission_drift == "tool_inventory_hash":
            return replace(admission, tool_inventory_hash="e" * 64)
        if self.admission_drift == "disabled_tools":
            return replace(admission, disabled_tools=[*admission.disabled_tools, "shell"])
        raise AssertionError(f"unknown admission drift: {self.admission_drift}")

    def submit_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        ordinal = len(self.payloads) - 1
        step = self._steps[min(ordinal, len(self._steps) - 1)]
        task_id = str(payload["task_id"])
        self._step_by_task[task_id] = step
        if step.startswith("http_"):
            status = int(step.removeprefix("http_").split("_", 1)[0])
            retry_after = 10.0 if step.endswith("retry_after") else None
            raise ManagedTaskTransportError(
                "synthetic HTTP failure",
                phase="submit",
                failure=TaskTransportFailure.HTTP_STATUS,
                http_status=status,
                retry_after_seconds=retry_after,
            )
        if step == "ambiguous_success":
            self._save_success(payload, task_status="completed", include_tools=True)
            raise ManagedTaskTransportError(
                "synthetic lost submit response",
                phase="submit",
                failure=TaskTransportFailure.READ_TIMEOUT,
                acceptance_ambiguous=True,
            )
        if step == "success":
            self._save_success(payload, task_status="completed", include_tools=True)
        elif step == "success_no_usage":
            self._save_success(payload, task_status="completed", include_tools=True)
            self._events[task_id] = [
                event for event in self._events[task_id] if event.get("type") != "llm_usage"
            ]
        elif step == "saved_response_lost":
            self._save_success(payload, task_status="failed", include_tools=False)
            self._tasks[task_id]["reason_code"] = "provider_unavailable"
        elif step == "hold":
            self._tasks[task_id] = {"status": "running", "task_id": task_id}
            self._events[task_id] = []
            self._queued.add(task_id)
        elif step == "unreleased":
            self._tasks[task_id] = {
                "status": "failed",
                "task_id": task_id,
                "reason_code": "provider_unavailable",
            }
            self._events[task_id] = [{"type": "task_done", "data": {"status": "failed"}}]
            self._queued.add(task_id)
        elif step == "completed_bad_tools":
            self._tasks[task_id] = {"status": "completed", "task_id": task_id}
            self._events[task_id] = [
                self._tool_event("mcp_factory__cf_context_get", "one"),
                self._usage_event("task"),
                self._usage_event("post_task_summary"),
                {"type": "task_done", "data": {"status": "completed"}},
            ]
        elif step.startswith("failed_"):
            reason = step.removeprefix("failed_")
            self._tasks[task_id] = {
                "status": "failed",
                "task_id": task_id,
                "reason_code": reason,
            }
            self._events[task_id] = [{"type": "task_done", "data": {"status": "failed"}}]
        else:
            raise AssertionError(f"unknown scripted step: {step}")
        return {"ok": True, "task_id": task_id}

    def task(self, task_id: str) -> dict[str, Any]:
        if task_id not in self._tasks:
            raise ManagedTaskTransportError(
                "synthetic task not found",
                phase="lookup",
                failure=TaskTransportFailure.HTTP_STATUS,
                http_status=404,
                task_not_found=True,
            )
        return self._tasks[task_id]

    def tasks(self) -> dict[str, Any]:
        return {
            "queue": {
                "running": [{"id": task_id} for task_id in sorted(self._queued)],
                "pending": [],
            }
        }

    def cancel_task(self, task_id: str) -> None:
        if self._step_by_task.get(task_id) == "unreleased":
            return
        self._queued.discard(task_id)
        self._tasks[task_id] = {"status": "cancelled", "task_id": task_id}
        self._events[task_id] = [{"type": "task_done", "data": {"status": "cancelled"}}]

    def task_events_text(self, task_id: str) -> str:
        return "".join(f"data: {json.dumps(event)}\n\n" for event in self._events.get(task_id, []))

    def _save_success(
        self,
        payload: dict[str, Any],
        *,
        task_status: str,
        include_tools: bool,
    ) -> None:
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
        task_id = str(payload["task_id"])
        self._tasks[task_id] = {
            "status": task_status,
            "task_id": task_id,
            "final_answer": json.dumps({"status": "SAVED", "draft_id": saved.draft_id}),
        }
        events: list[dict[str, Any]] = []
        if include_tools:
            events.append(self._tool_event("mcp_factory__cf_context_get", "one"))
        events.extend([self._usage_event("task"), self._usage_event("post_task_summary")])
        if include_tools:
            events.append(self._tool_event("mcp_factory__cf_draft_save", "two"))
        events.append({"type": "task_done", "data": {"status": task_status}})
        self._events[task_id] = events

    @staticmethod
    def _tool_event(name: str, timestamp: str) -> dict[str, Any]:
        return {
            "type": "tool_completed",
            "source": "tools",
            "data": {"tool": name, "ts": timestamp, "args": {}},
        }

    @staticmethod
    def _usage_event(category: str) -> dict[str, Any]:
        return {
            "type": "llm_usage",
            "source": "events",
            "data": {
                "category": category,
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "cost": 0.0,
                "ts": "2026-07-17T00:00:00+00:00",
            },
        }


def _services(
    tmp_path: pathlib.Path,
    steps: list[str],
) -> tuple[WorkflowStore, FactoryMcpService, ScriptedTaskAdapter, str]:
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
    return store, mcp, ScriptedTaskAdapter(mcp, steps), campaign.campaign_id


def _run(
    tmp_path: pathlib.Path,
    steps: list[str],
    *,
    retry_enabled: bool = True,
    terminal_deadline_seconds: float = 0.2,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[WorkflowStore, FactoryMcpService, ScriptedTaskAdapter, Any]:
    store, mcp, adapter, campaign_id = _services(tmp_path, steps)
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        controlled_provider_retry_enabled=retry_enabled,
        terminal_deadline_seconds=terminal_deadline_seconds,
        poll_interval_seconds=0.005,
        sleeper=sleeper,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(accepted.run_id, timeout=3)
    finally:
        coordinator.shutdown()
    return store, mcp, adapter, completed


def test_normal_success_keeps_one_physical_attempt(tmp_path: pathlib.Path) -> None:
    store, _, adapter, completed = _run(tmp_path, ["success"])

    assert completed.status is RunStatus.COMPLETED
    assert completed.physical_attempt_count == 1
    assert len(completed.attempts) == len(adapter.payloads) == 1
    assert len(store.workspace(completed.campaign_id).package_history) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_transient_http_then_success_creates_exactly_two_attempts_and_one_result(
    tmp_path: pathlib.Path,
    status: int,
) -> None:
    store, mcp, adapter, completed = _run(tmp_path, [f"http_{status}", "success"])

    assert completed.status is RunStatus.COMPLETED
    assert completed.physical_attempt_count == len(completed.attempts) == 2
    assert len(adapter.payloads) == 2
    assert len(store.workspace(completed.campaign_id).package_history) == 1
    assert [row["status"] for row in mcp.authorization_attempts(completed.run_id)] == [
        "CLOSED",
        "CONSUMED",
    ]


def test_terminal_deadline_release_then_success_retries_once(tmp_path: pathlib.Path) -> None:
    _, _, adapter, completed = _run(
        tmp_path,
        ["hold", "success"],
        terminal_deadline_seconds=0.1,
    )

    assert completed.status is RunStatus.COMPLETED
    assert len(adapter.payloads) == completed.physical_attempt_count == 2
    assert completed.attempts[0].reason_code == "TERMINAL_DEADLINE"
    assert completed.attempts[0].released_at is not None


def test_second_attempt_waits_once_for_runtime_admission_to_settle(
    tmp_path: pathlib.Path,
) -> None:
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        time.sleep(0)

    store, mcp, adapter, campaign_id = _services(tmp_path, ["http_503", "success"])
    adapter.transient_admission_failures = 1
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        controlled_provider_retry_enabled=True,
        terminal_deadline_seconds=0.2,
        poll_interval_seconds=0.005,
        retry_backoff_seconds=0,
        retry_admission_settle_seconds=2.0,
        sleeper=sleeper,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(accepted.run_id, timeout=2)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED
    assert completed.physical_attempt_count == len(adapter.payloads) == 2
    assert 2.0 in sleeps
    assert any(
        event.event_type == "run.stage"
        and event.data.get("stage") == "retry_admission_settle"
        for event in store.run_events(completed.run_id)
    )


def test_normalized_provider_unavailable_reason_then_success_retries_once(
    tmp_path: pathlib.Path,
) -> None:
    _, _, adapter, completed = _run(
        tmp_path,
        ["failed_provider_unavailable", "success"],
    )

    assert completed.status is RunStatus.COMPLETED
    assert len(adapter.payloads) == completed.physical_attempt_count == 2
    assert completed.attempts[0].reason_code == "TRANSIENT_RUNTIME_PROVIDER_UNAVAILABLE"


def test_ambiguous_submit_with_created_task_recovers_without_second_submit(
    tmp_path: pathlib.Path,
) -> None:
    store, _, adapter, completed = _run(tmp_path, ["ambiguous_success"])

    assert completed.status is RunStatus.COMPLETED
    assert len(adapter.payloads) == completed.physical_attempt_count == 1
    assert any(
        event.event_type == "run.stage" and event.data.get("stage") == "ambiguous_submit_recovered"
        for event in store.run_events(completed.run_id)
    )


def test_unconfirmed_worker_release_fails_closed_without_retry(tmp_path: pathlib.Path) -> None:
    _, _, adapter, completed = _run(
        tmp_path,
        ["unreleased", "success"],
        terminal_deadline_seconds=0.03,
    )

    assert completed.status is RunStatus.FAILED
    assert completed.reason_code == "WORKER_RELEASE_UNCONFIRMED"
    assert completed.worker_released_at is None
    assert len(adapter.payloads) == completed.physical_attempt_count == 1


def test_saved_draft_with_lost_terminal_response_is_recovered_without_retry(
    tmp_path: pathlib.Path,
) -> None:
    store, _, adapter, completed = _run(tmp_path, ["saved_response_lost", "success"])

    assert completed.status is RunStatus.COMPLETED
    assert completed.physical_attempt_count == len(adapter.payloads) == 1
    assert completed.attempts[0].reason_code == "LIVE_RESULT_RECOVERED"
    assert completed.attempts[0].result_present is True
    assert len(store.workspace(completed.campaign_id).package_history) == 1


def test_incomplete_usage_alone_does_not_retry_or_discard_a_saved_result(
    tmp_path: pathlib.Path,
) -> None:
    _, _, adapter, completed = _run(tmp_path, ["success_no_usage", "success"])

    assert completed.status is RunStatus.COMPLETED
    assert len(adapter.payloads) == completed.physical_attempt_count == 1
    assert completed.attempts[0].usage_status == "UNKNOWN"
    assert completed.attempts[0].result_present is True


@pytest.mark.parametrize(
    "step",
    [
        "completed_bad_tools",
        "failed_qa_failure",
        "failed_schema_failure",
        "failed_fact_failure",
        "failed_product_name_failure",
        "failed_content_failure",
        "failed_safety_rejection",
        "failed_policy_rejection",
        "failed_invalid_credential",
        "failed_contract_drift",
        "failed_model_drift",
        "failed_tool_drift",
        "http_400",
    ],
)
def test_content_contract_tool_and_safety_failures_never_retry(
    tmp_path: pathlib.Path,
    step: str,
) -> None:
    _, _, adapter, completed = _run(tmp_path, [step, "success"])

    assert completed.physical_attempt_count == len(adapter.payloads) == 1
    assert completed.attempts[0].retry_allowed is False


@pytest.mark.parametrize(
    "drift",
    ["prompt_hash", "skill_content_hash", "tool_inventory_hash", "disabled_tools"],
)
def test_second_attempt_identity_drift_fails_before_another_submit(
    tmp_path: pathlib.Path,
    drift: str,
) -> None:
    store, mcp, adapter, campaign_id = _services(tmp_path, ["http_503", "success"])
    adapter.admission_drift = drift
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        controlled_provider_retry_enabled=True,
        terminal_deadline_seconds=0.2,
        poll_interval_seconds=0.005,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(accepted.run_id, timeout=2)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.FAILED
    assert completed.reason_code == "TASK_ADMISSION_FAILED"
    assert completed.physical_attempt_count == 2
    assert len(adapter.payloads) == 1
    assert completed.attempts[1].retry_allowed is False


def test_explicit_user_cancel_never_retries(tmp_path: pathlib.Path) -> None:
    store, mcp, adapter, campaign_id = _services(tmp_path, ["hold", "success"])
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        controlled_provider_retry_enabled=True,
        terminal_deadline_seconds=0.2,
        poll_interval_seconds=0.005,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        coordinator.cancel(accepted.run_id)
        completed = coordinator.wait(accepted.run_id, timeout=2)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.CANCELLED
    assert len(adapter.payloads) == completed.physical_attempt_count == 1


def test_two_transient_failures_end_without_a_third_attempt(tmp_path: pathlib.Path) -> None:
    _, _, adapter, completed = _run(tmp_path, ["http_503", "http_503", "success"])

    assert completed.status is RunStatus.FAILED
    assert completed.reason_code == "CONTROLLED_RETRY_EXHAUSTED"
    assert len(adapter.payloads) == completed.physical_attempt_count == 2


def _persist_retryable_first_attempt(
    store: WorkflowStore,
    mcp: FactoryMcpService,
    adapter: ScriptedTaskAdapter,
    campaign_id: str,
) -> str:
    context = store.get_current_context(campaign_id)
    admission = adapter.admit()
    run_id = "run_restart_retry_0001"
    task_id = "task_restart_retry_0001"
    attempt_id = "attempt_restart_retry_0001"
    project_id = "project_restart_retry_0001"
    key = f"operation-{hashlib.sha256(run_id.encode()).hexdigest()}"
    payload = build_campaign_task(
        task_id=task_id,
        run_id=run_id,
        campaign_id=campaign_id,
        operation="initial",
        iteration=1,
        idempotency_key=key,
        context_version=context.context_version,
        project_id=project_id,
        admission=admission,
    )
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
        attempt_id=attempt_id,
        provider="openai",
        model="gpt-5.4-mini",
        provider_profile="openai-gpt-5.4-mini",
        request_digest=campaign_request_digest(payload),
    )
    mcp.prepare_operation(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        project_id=project_id,
        campaign_id=campaign_id,
        operation="initial",
        iteration=1,
        idempotency_key=key,
        context=context.model_dump(mode="json"),
    )
    mcp.close_operation(run_id)
    store.finish_attempt(
        attempt_id,
        outcome=AttemptOutcome.TRANSIENT_FAILURE.value,
        reason_code="TRANSIENT_TASK_TRANSPORT_503",
        failure_kind="http_status",
        retry_allowed=True,
        tool_receipts=[],
        provider_call_ledger={},
        usage_status="UNKNOWN",
        draft_present=False,
        result_present=False,
        released=True,
    )
    return run_id


def test_restart_reconcile_continues_from_durable_retry_decision_without_duplicate(
    tmp_path: pathlib.Path,
) -> None:
    store, mcp, adapter, campaign_id = _services(tmp_path, ["success"])
    run_id = _persist_retryable_first_attempt(store, mcp, adapter, campaign_id)
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        controlled_provider_retry_enabled=True,
        terminal_deadline_seconds=0.2,
        poll_interval_seconds=0.005,
        retry_backoff_seconds=0,
    )
    try:
        coordinator.reconcile_active()
        completed = coordinator.wait(run_id, timeout=2)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED
    assert len(adapter.payloads) == 1
    assert completed.physical_attempt_count == 2


def test_concurrent_reconcile_calls_create_only_one_second_task(tmp_path: pathlib.Path) -> None:
    store, mcp, adapter, campaign_id = _services(tmp_path, ["success"])
    run_id = _persist_retryable_first_attempt(store, mcp, adapter, campaign_id)
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        controlled_provider_retry_enabled=True,
        terminal_deadline_seconds=0.2,
        poll_interval_seconds=0.005,
        retry_backoff_seconds=0,
    )
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: coordinator.reconcile_active(), range(12)))
        completed = coordinator.wait(run_id, timeout=2)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED
    assert len(adapter.payloads) == 1
    assert completed.physical_attempt_count == 2


def test_submission_claim_is_atomic_across_store_instances(tmp_path: pathlib.Path) -> None:
    store, mcp, adapter, campaign_id = _services(tmp_path, ["success"])
    run_id = _persist_retryable_first_attempt(store, mcp, adapter, campaign_id)
    first = store.get_run(run_id).attempts[0]
    second = store.prepare_retry_attempt(
        run_id,
        attempt_id="attempt_atomic_claim_0001",
        task_id="task_atomic_claim_0001",
        request_digest=first.request_digest,
    )
    database_url = f"sqlite:///{tmp_path / 'factory.db'}"
    other = WorkflowStore(
        database_url,
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "other-artifacts",
    )
    other.initialize()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda owner: owner.claim_attempt_submission(second.attempt_id),
                (store, other),
            )
        )

    assert sorted(claims) == [False, True]
    assert store.run_attempts(run_id)[1].status is RunAttemptStatus.SUBMITTING


@pytest.mark.parametrize(
    ("first_step", "expected_delay"),
    [("http_429_retry_after", 0.5), ("http_503", 0.25)],
)
def test_retry_after_and_default_backoff_use_injected_bounded_sleeper(
    tmp_path: pathlib.Path,
    first_step: str,
    expected_delay: float,
) -> None:
    calls: list[float] = []

    def sleeper(seconds: float) -> None:
        calls.append(seconds)
        time.sleep(0)

    store, mcp, adapter, campaign_id = _services(tmp_path, [first_step, "success"])
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        controlled_provider_retry_enabled=True,
        terminal_deadline_seconds=0.2,
        poll_interval_seconds=0.005,
        retry_after_cap_seconds=0.5,
        retry_backoff_seconds=0.25,
        sleeper=sleeper,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(accepted.run_id, timeout=2)
    finally:
        coordinator.shutdown()

    assert completed.status is RunStatus.COMPLETED
    assert expected_delay in calls


def test_flag_off_preserves_single_attempt_admission_failure(tmp_path: pathlib.Path) -> None:
    _, _, adapter, completed = _run(
        tmp_path,
        ["http_503", "success"],
        retry_enabled=False,
    )

    assert completed.status is RunStatus.FAILED
    assert completed.reason_code == "TASK_ADMISSION_FAILED"
    assert len(adapter.payloads) == completed.physical_attempt_count == 1


def test_retry_keeps_request_provider_hash_context_and_tool_identity_constant(
    tmp_path: pathlib.Path,
) -> None:
    _, _, adapter, completed = _run(tmp_path, ["http_503", "success"])

    first, second = completed.attempts
    assert first.task_id != second.task_id
    assert first.request_digest == second.request_digest
    assert first.context_digest == second.context_digest == completed.context_version
    assert (first.provider, first.model, first.provider_profile) == (
        second.provider,
        second.model,
        second.provider_profile,
    )
    invariant_payloads = [
        {key: value for key, value in payload.items() if key != "task_id"}
        for payload in adapter.payloads
    ]
    assert invariant_payloads[0] == invariant_payloads[1]
    assert adapter.payloads[0]["disabled_tools"] == adapter.payloads[1]["disabled_tools"]


def test_api_and_workspace_read_models_show_both_attempts_and_aggregate_ledger(
    tmp_path: pathlib.Path,
) -> None:
    adapters: list[ScriptedTaskAdapter] = []

    def factory(mcp: FactoryMcpService) -> ScriptedTaskAdapter:
        adapter = ScriptedTaskAdapter(mcp, ["http_503", "success"])
        adapters.append(adapter)
        return adapter

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{tmp_path / 'factory.db'}",
        ARTIFACTS_DIR=tmp_path / "artifacts",
        SYNTHETIC_DATA_DIR=DEFAULT_DATA_DIR,
        MCP_SHARED_TOKEN="controlled-retry-test-token-is-long-enough",
        CONTROLLED_PROVIDER_RETRY_ENABLED=True,
        LIVE_RUN_TERMINAL_DEADLINE_SECONDS=6,
    )
    app = create_app(settings, task_adapter_factory=factory)
    run: dict[str, Any] = {}
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/campaigns",
            json={"case_id": "B04"},
            headers={"Idempotency-Key": "retry-api-create-0001"},
        ).json()
        campaign_id = created["campaign_id"]
        client.post(
            f"/api/v1/campaigns/{campaign_id}/validate",
            headers={"Idempotency-Key": "retry-api-validate-0001"},
        )
        started = client.post(
            f"/api/v1/campaigns/{campaign_id}/runs",
            json={"mode": "live_ouroboros"},
            headers={"Idempotency-Key": "retry-api-run-0000001"},
        ).json()
        for _ in range(200):
            run = client.get(f"/api/v1/runs/{started['run_id']}").json()
            if run["terminal_at"] is not None:
                break
            time.sleep(0.01)
        workspace = client.get(f"/api/v1/campaigns/{campaign_id}/workspace").json()

    assert run["status"] == "COMPLETED"
    assert run["physical_attempt_count"] == len(run["attempts"]) == 2
    assert len(run["provider_call_ledger"]["attempts"]) == 2
    assert workspace["runs"][-1]["attempts"] == run["attempts"]
    assert len(adapters[0].payloads) == 2


def test_export_preserves_both_attempts_and_unknown_first_usage(
    tmp_path: pathlib.Path,
) -> None:
    store, _, _, completed = _run(tmp_path, ["http_503", "success"])
    assert completed.package_id is not None
    package = store.get_package(completed.package_id)
    store.approve_package(
        package.package_id,
        ApprovalRequest(
            package_hash=package.package_hash,
            decision=ApprovalDecision.APPROVED,
            test_only=True,
        ),
        actor_id="controlled_retry_test_editor",
    )

    exported = store.export_package(package.package_id)
    with zipfile.ZipFile(store.export_path(exported.export_id)) as archive:
        run_document = json.loads(archive.read("run.json"))
        usage_document = json.loads(archive.read("trace/model-usage.json"))

    assert run_document["physical_attempt_count"] == 2
    assert [row["attempt_number"] for row in run_document["attempts"]] == [1, 2]
    assert run_document["attempts"][0]["failure_kind"] == "http_status"
    assert [row["usage_status"] for row in usage_document["attempt_usage"]] == [
        "UNKNOWN",
        "EXACT",
    ]
    assert len(usage_document["provider_call_ledger"]["attempts"]) == 2
