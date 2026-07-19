from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from typing import Any, Literal, Protocol

from apps.api.app.domain.workflow import (
    DemoResetResult,
    RunAttemptStatus,
    RunAttemptView,
    RunStatus,
    RunView,
)
from apps.api.app.live_probe_transport import (
    observed_tool_names,
    parse_sse_events,
    provider_call_ledger,
    queue_contains_task,
    terminal_event_observed,
    usage_is_complete,
)
from apps.api.app.mcp.service import FactoryMcpService
from apps.api.app.ouroboros_client import (
    ALLOWED_PROVIDER_TOOLS,
    ManagedTaskTransportError,
    OuroborosTaskAdapter,
    TaskAdmission,
    TaskAdmissionError,
    TaskTransportFailure,
    build_campaign_task,
    hash_json,
)
from apps.api.app.workflow.retry import (
    AttemptOutcome,
    RetryAssessment,
    assess_terminal_failure,
    assess_transport_failure,
)
from apps.api.app.workflow.store import WorkflowConflict, WorkflowStore

TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled", "rejected_duplicate"}
MAX_CONTROLLED_PROVIDER_RETRIES = 1
DEFAULT_RETRY_BACKOFF_SECONDS = 0.25
MAX_RETRY_AFTER_SECONDS = 2.0
DEFAULT_RETRY_ADMISSION_SETTLE_SECONDS = 2.0
TRANSIENT_RETRY_ADMISSION_ERRORS = frozenset(
    {
        "private Ouroboros readiness request failed",
        "private Ouroboros readiness response is invalid",
        "runtime readiness or mode profile drifted",
    }
)


class ManagedTaskAdapter(Protocol):
    def admit(self) -> TaskAdmission: ...

    def submit_task(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def task(self, task_id: str) -> dict[str, Any]: ...

    def tasks(self) -> dict[str, Any]: ...

    def cancel_task(self, task_id: str) -> None: ...

    def task_events_text(self, task_id: str) -> str: ...


def campaign_request_digest(payload: dict[str, Any]) -> str:
    """Digest the generation contract while excluding only the physical task id."""

    projection = {str(key): value for key, value in payload.items() if key != "task_id"}
    return hash_json(projection)


class RunCoordinator:
    def __init__(
        self,
        *,
        store: WorkflowStore,
        mcp_service: FactoryMcpService,
        adapter: ManagedTaskAdapter,
        terminal_deadline_seconds: float = 29.0,
        task_timeout_seconds: int = 25,
        usage_expected_provider: str = "openai",
        usage_require_post_task_summary: bool = True,
        poll_interval_seconds: float = 0.25,
        controlled_provider_retry_enabled: bool = False,
        provider_identity: str = "openai",
        model_identity: str = "gpt-5.4-mini",
        provider_profile: str = "openai-gpt-5.4-mini",
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        retry_after_cap_seconds: float = MAX_RETRY_AFTER_SECONDS,
        retry_admission_settle_seconds: float = DEFAULT_RETRY_ADMISSION_SETTLE_SECONDS,
        before_task_submit: (
            Callable[[RunView, RunAttemptView, dict[str, Any]], None] | None
        ) = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if retry_backoff_seconds < 0:
            raise ValueError("controlled retry backoff cannot be negative")
        if retry_after_cap_seconds <= 0:
            raise ValueError("controlled Retry-After cap must be positive")
        if retry_admission_settle_seconds < 0:
            raise ValueError("controlled retry admission settle cannot be negative")
        self._store = store
        self._mcp = mcp_service
        self._adapter = adapter
        self._terminal_deadline_seconds = terminal_deadline_seconds
        self._task_timeout_seconds = task_timeout_seconds
        self._usage_expected_provider = usage_expected_provider
        self._usage_require_post_task_summary = usage_require_post_task_summary
        self._poll_interval_seconds = poll_interval_seconds
        self._controlled_retry_enabled = controlled_provider_retry_enabled
        self._provider_identity = provider_identity
        self._model_identity = model_identity
        self._provider_profile = provider_profile
        self._retry_backoff_seconds = retry_backoff_seconds
        self._retry_after_cap_seconds = retry_after_cap_seconds
        self._retry_admission_settle_seconds = retry_admission_settle_seconds
        self._before_task_submit = before_task_submit
        self._monotonic = monotonic
        self._sleep = sleeper
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cf-run")
        self._futures: dict[str, Future[None]] = {}
        self._future_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()

    def start_live(self, campaign_id: str) -> RunView:
        with self._lifecycle_lock:
            return self._start_live(campaign_id)

    def _start_live(self, campaign_id: str) -> RunView:
        context = self._store.get_current_context(campaign_id)
        operation = context.operation.value
        admission = self._adapter.admit()
        iteration = self._store.next_run_iteration(campaign_id, operation)
        run_id = f"run_{uuid.uuid4().hex}"
        attempt_id = f"attempt_{uuid.uuid4().hex}"
        task_id = f"task_{uuid.uuid4().hex}"
        project_id = f"project_{hashlib.sha256(campaign_id.encode()).hexdigest()[:32]}"
        idempotency_key = self._idempotency_key(run_id)
        payload = build_campaign_task(
            task_id=task_id,
            run_id=run_id,
            campaign_id=campaign_id,
            operation=operation,
            iteration=iteration,
            idempotency_key=idempotency_key,
            context_version=context.context_version,
            project_id=project_id,
            admission=admission,
            timeout_sec=self._task_timeout_seconds,
        )
        request_digest = campaign_request_digest(payload)
        created = self._store.create_live_run(
            run_id=run_id,
            campaign_id=campaign_id,
            operation=operation,
            iteration=iteration,
            task_id=task_id,
            project_id=project_id,
            context_version=context.context_version,
            prompt_hash=admission.prompt_hash,
            skill_content_hash=admission.skill_content_hash,
            tool_inventory_hash=admission.tool_inventory_hash,
            attempt_id=attempt_id,
            provider=self._provider_identity,
            model=self._model_identity,
            provider_profile=self._provider_profile,
            request_digest=request_digest,
        )
        try:
            self._mcp.prepare_operation(
                run_id=run_id,
                task_id=task_id,
                project_id=project_id,
                campaign_id=campaign_id,
                operation=operation,
                iteration=iteration,
                idempotency_key=idempotency_key,
                context=context.model_dump(mode="json"),
                attempt_id=attempt_id,
            )
            self._store.append_run_event(
                run_id,
                event_key="run.context_bound",
                event_type="run.stage",
                data={
                    "stage": "context_version_bound",
                    "context_version": context.context_version,
                },
            )
            self._submit_attempt(
                created,
                created.attempts[0],
                admission=admission,
                inline_monitor=False,
            )
        except Exception as exc:
            self._fail_before_task(run_id, created.attempts[0], exc)
        return self._store.get_run(run_id)

    def reset_demo_state(self) -> DemoResetResult:
        with self._lifecycle_lock:
            with self._future_lock:
                if any(not future.done() for future in self._futures.values()):
                    raise RuntimeError("demo reset is unavailable while a run monitor is active")
            if self._store.active_runs():
                raise RuntimeError("demo reset is unavailable while a run is active")
            self._mcp.reset_demo_state()
            return self._store.reset_demo_state()

    def cancel(self, run_id: str) -> RunView:
        run = self._store.request_run_cancel(run_id)
        if run.task_id is not None and run.status is RunStatus.CANCEL_REQUESTED:
            try:
                self._mcp.close_operation(run_id)
                self._adapter.cancel_task(run.task_id)
            except Exception:
                self._store.append_run_event(
                    run_id,
                    event_key="run.cancel_transport_failed",
                    event_type="run.stage",
                    data={"stage": "cancel_transport_failed"},
                )
        return self._store.get_run(run_id)

    def reconcile_active(self) -> None:
        with self._lifecycle_lock:
            for run in self._store.active_runs():
                self._store.append_run_event(
                    run.run_id,
                    event_key="run.reconciled",
                    event_type="run.stage",
                    data={"stage": "startup_reconcile"},
                )
                self._submit_job(run.run_id, self._resume_run, run.run_id)

    def wait(self, run_id: str, *, timeout: float = 35.0) -> RunView:
        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            run = self._store.get_run(run_id)
            if run.terminal_at is not None and (
                run.worker_released_at is not None
                or run.reason_code == "WORKER_RELEASE_UNCONFIRMED"
            ):
                return run
            self._sleep(min(0.02, self._poll_interval_seconds))
        raise TimeoutError("run did not reach a released terminal state")

    @property
    def terminal_deadline_seconds(self) -> float:
        return self._terminal_deadline_seconds

    @property
    def max_logical_duration_seconds(self) -> float:
        attempts = 1 + (MAX_CONTROLLED_PROVIDER_RETRIES if self._controlled_retry_enabled else 0)
        retry_wait = self._retry_after_cap_seconds if self._controlled_retry_enabled else 0.0
        return (self._terminal_deadline_seconds * attempts) + retry_wait

    @property
    def controlled_provider_retry_enabled(self) -> bool:
        return self._controlled_retry_enabled

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)
        if isinstance(self._adapter, OuroborosTaskAdapter):
            self._adapter.close()

    def _submit_job(
        self,
        run_id: str,
        function: Callable[..., None],
        *args: object,
    ) -> None:
        with self._future_lock:
            existing = self._futures.get(run_id)
            if existing is not None and not existing.done():
                return
            self._futures[run_id] = self._executor.submit(function, *args)

    def _submit_monitor(self, run_id: str, attempt_id: str) -> None:
        self._submit_job(run_id, self._monitor, run_id, attempt_id)

    def _monitor(self, run_id: str, attempt_id: str) -> None:
        try:
            self._monitor_inner(run_id, attempt_id)
        except Exception:
            self._monitor_failed(run_id, attempt_id)

    def _resume_run(self, run_id: str) -> None:
        try:
            run = self._store.get_run(run_id)
            if not run.attempts:
                self._finish_logical_failure(run_id, "TASK_ID_MISSING", released=True)
                return
            attempt = run.attempts[-1]
            if attempt.status is RunAttemptStatus.RELEASED:
                if (
                    self._controlled_retry_enabled
                    and attempt.attempt_number == 1
                    and attempt.retry_allowed
                ):
                    self._retry_after_failure(run_id, self._retry_backoff_seconds)
                else:
                    self._finish_from_persisted_attempt(run, attempt)
                return
            if attempt.status is RunAttemptStatus.SUBMITTING:
                location, _ = self._locate_task(attempt.task_id)
                if location == "present":
                    self._store.mark_attempt_started(attempt.attempt_id)
                    self._store.mark_run_started(run_id)
                    self._monitor_inner(run_id, attempt.attempt_id)
                    return
                if location == "absent":
                    self._resolve_absent_submission_after_restart(run, attempt)
                    return
                self._release_unconfirmed(run_id, attempt, events=[])
                return
            if attempt.status is RunAttemptStatus.PREPARED:
                # Legacy rows could be marked RUNNING before per-attempt state existed.
                # Recover their already-known task instead of constructing a new submit.
                if run.status in {RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED}:
                    location, _ = self._locate_task(attempt.task_id)
                    if location == "present":
                        self._store.mark_attempt_started(attempt.attempt_id)
                        self._monitor_inner(run_id, attempt.attempt_id)
                        return
                    if location == "unconfirmed":
                        self._release_unconfirmed(run_id, attempt, events=[])
                        return
                    self._resolve_absent_submission_after_restart(run, attempt)
                    return
                self._submit_attempt(run, attempt, admission=None, inline_monitor=True)
                return
            self._monitor_inner(run_id, attempt.attempt_id)
        except Exception:
            attempts = self._store.run_attempts(run_id)
            self._monitor_failed(run_id, attempts[-1].attempt_id if attempts else "")

    def _ensure_mcp_authorization(self, run: RunView, attempt: RunAttemptView) -> None:
        idempotency_key = self._idempotency_key(run.run_id)
        if attempt.attempt_number == 1:
            context = self._store.get_current_context(run.campaign_id)
            if context.context_version != run.context_version:
                raise RuntimeError("active run context drifted before task submission")
            self._mcp.prepare_operation(
                run_id=run.run_id,
                task_id=attempt.task_id,
                project_id=run.project_id,
                campaign_id=run.campaign_id,
                operation=run.operation,
                iteration=run.iteration,
                idempotency_key=idempotency_key,
                context=context.model_dump(mode="json"),
                attempt_id=attempt.attempt_id,
            )
            return
        self._mcp.prepare_retry_operation(
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            task_id=attempt.task_id,
            project_id=run.project_id,
            campaign_id=run.campaign_id,
            operation=run.operation,
            iteration=run.iteration,
            idempotency_key=idempotency_key,
            context_version=run.context_version,
        )

    def _submit_attempt(
        self,
        run: RunView,
        attempt: RunAttemptView,
        *,
        admission: TaskAdmission | None,
        inline_monitor: bool,
    ) -> None:
        if not self._store.claim_attempt_submission(attempt.attempt_id):
            return
        try:
            self._ensure_mcp_authorization(run, attempt)
            effective_admission = admission or self._admit_attempt(run, attempt)
            self._assert_attempt_identity(run, attempt, effective_admission)
            payload = self._build_attempt_payload(run, attempt, effective_admission)
            if campaign_request_digest(payload) != attempt.request_digest:
                raise RuntimeError("managed retry request digest drifted")
            if self._before_task_submit is not None:
                self._before_task_submit(run, attempt, payload)
        except Exception as exc:
            self._fail_before_task(run.run_id, attempt, exc)
            return
        try:
            created = self._adapter.submit_task(payload)
            if str(created.get("task_id") or "") != attempt.task_id:
                raise ManagedTaskTransportError(
                    "managed Task API returned a different task id",
                    phase="submit",
                    failure=TaskTransportFailure.INVALID_RESPONSE,
                    acceptance_ambiguous=True,
                )
        except ManagedTaskTransportError as exc:
            self._handle_submit_failure(run, attempt, exc, inline_monitor=inline_monitor)
            return
        self._mark_task_started(run, attempt)
        if inline_monitor:
            self._monitor_inner(run.run_id, attempt.attempt_id)
        else:
            self._submit_monitor(run.run_id, attempt.attempt_id)

    def _admit_attempt(self, run: RunView, attempt: RunAttemptView) -> TaskAdmission:
        try:
            return self._adapter.admit()
        except TaskAdmissionError as exc:
            transient_retry_admission = (
                self._controlled_retry_enabled
                and attempt.attempt_number == 2
                and str(exc) in TRANSIENT_RETRY_ADMISSION_ERRORS
            )
            if not transient_retry_admission:
                raise
            self._store.append_run_event(
                run.run_id,
                event_key="run.retry_admission_settle",
                event_type="run.stage",
                data={
                    "stage": "retry_admission_settle",
                    "seconds": self._retry_admission_settle_seconds,
                },
            )
            self._sleep(self._retry_admission_settle_seconds)
            return self._adapter.admit()

    def _resolve_absent_submission_after_restart(
        self,
        run: RunView,
        attempt: RunAttemptView,
    ) -> None:
        """Resolve a durable submit intent only after its task is proven absent."""

        self._mcp.close_operation(run.run_id)
        current = self._store.get_run(run.run_id)
        cancelled = current.status is RunStatus.CANCEL_REQUESTED
        assessment = RetryAssessment(
            outcome=(AttemptOutcome.CANCELLED if cancelled else AttemptOutcome.TRANSIENT_FAILURE),
            reason_code=(
                "LIVE_TASK_CANCELLED" if cancelled else "SUBMISSION_NOT_OBSERVED_AFTER_RESTART"
            ),
            retry_allowed=not cancelled,
            failure_kind="cancelled" if cancelled else "provider_unavailable",
        )
        finished = self._finish_attempt_record(
            attempt,
            assessment,
            events=[],
            draft_present=False,
            result_present=False,
            released=True,
        )
        if cancelled:
            self._finish_cancelled(run.run_id)
            return
        if (
            self._controlled_retry_enabled
            and finished.attempt_number == 1
            and finished.retry_allowed
        ):
            self._retry_after_failure(run.run_id, self._retry_backoff_seconds)
            return
        reason = (
            "CONTROLLED_RETRY_EXHAUSTED"
            if finished.attempt_number == 2
            else "TASK_ADMISSION_FAILED"
        )
        self._finish_logical_failure(run.run_id, reason, released=True)

    def _mark_task_started(self, run: RunView, attempt: RunAttemptView) -> None:
        self._store.mark_attempt_started(attempt.attempt_id)
        self._store.append_run_event(
            run.run_id,
            event_key=f"run.task_bound.{attempt.attempt_number}",
            event_type="run.task_bound",
            data={
                "task_id": attempt.task_id,
                "project_id": run.project_id,
                "attempt_number": attempt.attempt_number,
            },
        )
        self._store.mark_run_started(run.run_id)

    def _handle_submit_failure(
        self,
        run: RunView,
        attempt: RunAttemptView,
        error: ManagedTaskTransportError,
        *,
        inline_monitor: bool,
    ) -> None:
        location, _ = self._locate_task(attempt.task_id)
        if location == "present":
            self._store.append_run_event(
                run.run_id,
                event_key=f"run.task_recovered.{attempt.attempt_number}",
                event_type="run.stage",
                data={
                    "stage": "ambiguous_submit_recovered",
                    "attempt_number": attempt.attempt_number,
                },
            )
            self._mark_task_started(run, attempt)
            if inline_monitor:
                self._monitor_inner(run.run_id, attempt.attempt_id)
            else:
                self._submit_monitor(run.run_id, attempt.attempt_id)
            return
        if location != "absent":
            self._mcp.close_operation(run.run_id)
            self._release_unconfirmed(run.run_id, attempt, events=[])
            return

        self._mcp.close_operation(run.run_id)
        assessment = assess_transport_failure(
            error,
            draft_present=False,
            result_present=False,
        )
        finished = self._finish_attempt_record(
            attempt,
            assessment,
            events=[],
            draft_present=False,
            result_present=False,
            released=True,
        )
        if (
            self._controlled_retry_enabled
            and finished.attempt_number == 1
            and finished.retry_allowed
            and self._store.get_run(run.run_id).status is not RunStatus.CANCEL_REQUESTED
        ):
            delay = self._bounded_retry_delay(assessment.retry_after_seconds)
            if inline_monitor:
                self._retry_after_failure(run.run_id, delay)
            else:
                self._submit_job(run.run_id, self._retry_job, run.run_id, delay)
            return
        reason = (
            "CONTROLLED_RETRY_EXHAUSTED"
            if finished.attempt_number == 2 and assessment.retry_allowed
            else "TASK_ADMISSION_FAILED"
        )
        self._finish_logical_failure(run.run_id, reason, released=True)

    def _locate_task(
        self,
        task_id: str,
    ) -> tuple[Literal["present", "absent", "unconfirmed"], dict[str, Any]]:
        try:
            result = self._adapter.task(task_id)
            observed_id = str(result.get("task_id") or result.get("id") or task_id)
            if observed_id != task_id:
                return "unconfirmed", {}
            return "present", result
        except ManagedTaskTransportError as exc:
            if not exc.task_not_found:
                return "unconfirmed", {}
        except Exception:
            return "unconfirmed", {}
        try:
            queued = self._adapter.tasks()
        except Exception:
            return "unconfirmed", {}
        if queue_contains_task(queued, task_id):
            return "present", {"task_id": task_id, "status": "queued"}
        return "absent", {}

    def _retry_after_failure(self, run_id: str, delay: float) -> None:
        if delay:
            self._store.append_run_event(
                run_id,
                event_key="run.retry_backoff",
                event_type="run.stage",
                data={"stage": "retry_backoff", "seconds": delay},
            )
            self._sleep(delay)
        run = self._store.get_run(run_id)
        if run.status is RunStatus.CANCEL_REQUESTED:
            self._finish_cancelled(run_id)
            return
        if len(run.attempts) == 2:
            second = run.attempts[1]
        else:
            first = run.attempts[0]
            if (
                first.status is not RunAttemptStatus.RELEASED
                or not first.retry_allowed
                or first.draft_present
                or first.result_present
            ):
                self._finish_logical_failure(run_id, "RETRY_PRECONDITION_FAILED", released=True)
                return
            snapshot = self._mcp.probe_snapshot(
                run.campaign_id,
                operation=run.operation,
                iteration=run.iteration,
            )
            if isinstance(snapshot.get("draft"), dict) or self._store.operation_result_present(
                campaign_id=run.campaign_id,
                operation=run.operation,
                context_version=run.context_version,
            ):
                self._finish_logical_failure(run_id, "RESULT_ALREADY_PERSISTED", released=True)
                return
            try:
                second = self._store.prepare_retry_attempt(
                    run_id,
                    attempt_id=f"attempt_{uuid.uuid4().hex}",
                    task_id=f"task_{uuid.uuid4().hex}",
                    request_digest=first.request_digest,
                )
            except WorkflowConflict:
                concurrent = self._store.get_run(run_id)
                if len(concurrent.attempts) != 2:
                    raise
                second = concurrent.attempts[1]
            run = self._store.get_run(run_id)
        self._submit_attempt(run, second, admission=None, inline_monitor=True)

    def _retry_job(self, run_id: str, delay: float) -> None:
        try:
            self._retry_after_failure(run_id, delay)
        except Exception:
            attempts = self._store.run_attempts(run_id)
            self._monitor_failed(run_id, attempts[-1].attempt_id if attempts else "")

    def _monitor_inner(self, run_id: str, attempt_id: str) -> None:
        attempts = self._store.run_attempts(run_id)
        attempt = next(item for item in attempts if item.attempt_id == attempt_id)
        deadline = self._monotonic() + self._terminal_deadline_seconds
        cleanup_margin = min(
            1.0,
            max(self._poll_interval_seconds * 2, self._terminal_deadline_seconds * 0.1),
        )
        cancel_at = deadline - cleanup_margin
        events: list[dict[str, Any]] = []
        final_result: dict[str, Any] = {}
        worker_released = False
        cancel_sent = False
        terminal_deadline_expired = False
        while self._monotonic() < deadline:
            try:
                final_result, events, worker_released = self._observe_task(run_id, attempt.task_id)
            except ManagedTaskTransportError:
                worker_released = False
            if worker_released:
                break
            current = self._store.get_run(run_id)
            user_cancelled = current.status is RunStatus.CANCEL_REQUESTED
            if (user_cancelled or self._monotonic() >= cancel_at) and not cancel_sent:
                cancel_sent = True
                terminal_deadline_expired = not user_cancelled
                self._mcp.close_operation(run_id)
                with suppress(Exception):
                    self._adapter.cancel_task(attempt.task_id)
                try:
                    final_result, events, worker_released = self._observe_task(
                        run_id, attempt.task_id
                    )
                except ManagedTaskTransportError:
                    worker_released = False
                if worker_released:
                    break
            remaining = max(0.0, deadline - self._monotonic())
            self._sleep(min(self._poll_interval_seconds, remaining))
        if not worker_released:
            if not cancel_sent:
                with suppress(Exception):
                    self._adapter.cancel_task(attempt.task_id)
            self._mcp.close_operation(run_id)
            self._release_unconfirmed(run_id, attempt, events=events)
            return

        self._mcp.close_operation(run_id)
        self._evaluate_released_attempt(
            run_id,
            attempt,
            final_result=final_result,
            events=events,
            terminal_deadline_expired=terminal_deadline_expired,
        )

    def _evaluate_released_attempt(
        self,
        run_id: str,
        attempt: RunAttemptView,
        *,
        final_result: dict[str, Any],
        events: list[dict[str, Any]],
        terminal_deadline_expired: bool,
    ) -> None:
        tool_names = observed_tool_names(events)
        ledger = provider_call_ledger(events)
        usage_complete = usage_is_complete(
            ledger,
            expected_provider=self._usage_expected_provider,
            require_post_task_summary=self._usage_require_post_task_summary,
        )
        status = str(final_result.get("status") or "")
        run = self._store.get_run(run_id)
        snapshot = self._mcp.probe_snapshot(
            run.campaign_id,
            operation=run.operation,
            iteration=run.iteration,
        )
        audit_types = [str(item.get("event_type") or "") for item in snapshot.get("events") or []]
        draft_present = isinstance(snapshot.get("draft"), dict)
        result_present = self._store.operation_result_present(
            campaign_id=run.campaign_id,
            operation=run.operation,
            context_version=run.context_version,
        )
        exact_tools = sorted(tool_names) == sorted(ALLOWED_PROVIDER_TOOLS)
        audit_valid = (
            1 <= audit_types.count("context_tool_completed") <= run.physical_attempt_count
            and audit_types.count("draft_saved") == 1
        )
        live_ok = (
            status == "completed"
            and draft_present
            and result_present
            and exact_tools
            and audit_valid
            and usage_complete
        )
        recovery_tools_valid = set(tool_names).issubset(set(ALLOWED_PROVIDER_TOOLS))
        recovered_result = draft_present and result_present and audit_valid and recovery_tools_valid
        final_answer = str(final_result.get("final_answer") or "")[:4_000] or None
        if live_ok or recovered_result:
            assessment = RetryAssessment(
                outcome=AttemptOutcome.SUCCEEDED,
                reason_code="LIVE_RESULT_ACCEPTED" if live_ok else "LIVE_RESULT_RECOVERED",
                retry_allowed=False,
                failure_kind="",
            )
            self._finish_attempt_record(
                attempt,
                assessment,
                events=events,
                draft_present=draft_present,
                result_present=result_present,
                released=True,
                usage_status="EXACT" if usage_complete else "UNKNOWN",
            )
            self._complete_live_run(run_id, final_answer=final_answer, recovered=not live_ok)
            return

        current = self._store.get_run(run_id)
        assessment = assess_terminal_failure(
            result=final_result,
            draft_present=draft_present,
            result_present=result_present,
            tool_sequence_valid=exact_tools,
            usage_complete=usage_complete,
            user_cancelled=current.status is RunStatus.CANCEL_REQUESTED,
            terminal_deadline_expired=(
                terminal_deadline_expired and self._controlled_retry_enabled
            ),
        )
        finished = self._finish_attempt_record(
            attempt,
            assessment,
            events=events,
            draft_present=draft_present,
            result_present=result_present,
            released=True,
            usage_status="EXACT" if usage_complete else "UNKNOWN",
        )
        if (
            self._controlled_retry_enabled
            and finished.attempt_number == 1
            and finished.retry_allowed
        ):
            self._retry_after_failure(run_id, self._bounded_retry_delay(None))
            return
        self._resolve_terminal_failure(
            run_id,
            finished,
            assessment,
            final_answer=final_answer,
        )

    def _finish_attempt_record(
        self,
        attempt: RunAttemptView,
        assessment: RetryAssessment,
        *,
        events: list[dict[str, Any]],
        draft_present: bool,
        result_present: bool,
        released: bool,
        usage_status: str = "UNKNOWN",
    ) -> RunAttemptView:
        return self._store.finish_attempt(
            attempt.attempt_id,
            outcome=assessment.outcome.value,
            reason_code=assessment.reason_code,
            failure_kind=assessment.failure_kind,
            retry_allowed=assessment.retry_allowed,
            tool_receipts=observed_tool_names(events),
            provider_call_ledger=provider_call_ledger(events),
            usage_status=usage_status,
            draft_present=draft_present,
            result_present=result_present,
            released=released,
        )

    def _complete_live_run(
        self,
        run_id: str,
        *,
        final_answer: str | None,
        recovered: bool,
    ) -> None:
        self._store.append_run_event(
            run_id,
            event_key="run.qa_completed",
            event_type="run.qa_completed",
            data={"status": "PASS", "result_recovered": recovered},
        )
        run = self._store.get_run(run_id)
        self._store.append_run_event(
            run_id,
            event_key="package.version_created",
            event_type="package.version_created",
            data={"package_id": self._store.get_campaign(run.campaign_id).package_id or ""},
        )
        receipts, ledger = self._aggregate_attempt_audit(run_id)
        self._store.finish_run(
            run_id,
            status=RunStatus.COMPLETED,
            reason_code=None,
            mode="live_ouroboros",
            tool_receipts=receipts,
            provider_call_ledger=ledger,
            final_answer=final_answer,
        )
        self._store.mark_worker_released(run_id)

    def _resolve_terminal_failure(
        self,
        run_id: str,
        attempt: RunAttemptView,
        assessment: RetryAssessment,
        *,
        final_answer: str | None,
    ) -> None:
        if assessment.outcome is AttemptOutcome.CANCELLED and self._controlled_retry_enabled:
            self._finish_cancelled(run_id)
            return
        receipts, ledger = self._aggregate_attempt_audit(run_id)
        if self._controlled_retry_enabled and attempt.attempt_number == 2:
            self._store.finish_run(
                run_id,
                status=RunStatus.FAILED,
                reason_code="CONTROLLED_RETRY_EXHAUSTED",
                mode="live_ouroboros",
                tool_receipts=receipts,
                provider_call_ledger=ledger,
                final_answer=final_answer,
            )
            self._store.mark_worker_released(run_id)
            return
        run = self._store.get_run(run_id)
        if attempt.result_present:
            self._store.finish_run(
                run_id,
                status=RunStatus.FAILED,
                reason_code=assessment.reason_code,
                mode="live_ouroboros",
                tool_receipts=receipts,
                provider_call_ledger=ledger,
                final_answer=final_answer,
            )
        else:
            self._store.run_current_deterministic_operation(run.campaign_id)
            self._store.finish_run(
                run_id,
                status=RunStatus.COMPLETED_FALLBACK,
                reason_code=assessment.reason_code,
                mode="deterministic_template",
                tool_receipts=receipts,
                provider_call_ledger=ledger,
                final_answer=final_answer,
            )
        self._store.mark_worker_released(run_id)

    def _finish_cancelled(self, run_id: str) -> None:
        receipts, ledger = self._aggregate_attempt_audit(run_id)
        self._store.finish_run(
            run_id,
            status=RunStatus.CANCELLED,
            reason_code="LIVE_TASK_CANCELLED",
            mode="live_ouroboros",
            tool_receipts=receipts,
            provider_call_ledger=ledger,
            final_answer=None,
        )
        self._store.mark_worker_released(run_id)

    def _release_unconfirmed(
        self,
        run_id: str,
        attempt: RunAttemptView,
        *,
        events: list[dict[str, Any]],
    ) -> None:
        assessment = RetryAssessment(
            outcome=AttemptOutcome.RELEASE_UNCONFIRMED,
            reason_code="WORKER_RELEASE_UNCONFIRMED",
            retry_allowed=False,
            failure_kind="release_unconfirmed",
        )
        run = self._store.get_run(run_id)
        snapshot = self._mcp.probe_snapshot(
            run.campaign_id,
            operation=run.operation,
            iteration=run.iteration,
        )
        draft_present = isinstance(snapshot.get("draft"), dict)
        result_present = self._store.operation_result_present(
            campaign_id=run.campaign_id,
            operation=run.operation,
            context_version=run.context_version,
        )
        self._finish_attempt_record(
            attempt,
            assessment,
            events=events,
            draft_present=draft_present,
            result_present=result_present,
            released=False,
        )
        receipts, ledger = self._aggregate_attempt_audit(run_id)
        self._store.finish_run(
            run_id,
            status=RunStatus.FAILED,
            reason_code="WORKER_RELEASE_UNCONFIRMED",
            mode="live_ouroboros",
            tool_receipts=receipts,
            provider_call_ledger=ledger,
            final_answer=None,
        )

    def _finish_logical_failure(self, run_id: str, reason: str, *, released: bool) -> None:
        receipts, ledger = self._aggregate_attempt_audit(run_id)
        self._store.finish_run(
            run_id,
            status=RunStatus.FAILED,
            reason_code=reason,
            mode="live_ouroboros",
            tool_receipts=receipts,
            provider_call_ledger=ledger,
            final_answer=None,
        )
        if released:
            self._store.mark_worker_released(run_id)

    def _fail_before_task(
        self,
        run_id: str,
        attempt: RunAttemptView,
        error: Exception,
    ) -> None:
        self._mcp.close_operation(run_id)
        reason = "TASK_ADMISSION_FAILED"
        failure_kind = "contract_or_orchestration"
        if isinstance(error, TaskAdmissionError):
            failure_kind = "task_admission"
        assessment = RetryAssessment(
            outcome=AttemptOutcome.PERMANENT_FAILURE,
            reason_code=reason,
            retry_allowed=False,
            failure_kind=failure_kind,
        )
        self._finish_attempt_record(
            attempt,
            assessment,
            events=[],
            draft_present=False,
            result_present=False,
            released=True,
        )
        self._finish_logical_failure(run_id, reason, released=True)

    def _monitor_failed(self, run_id: str, attempt_id: str) -> None:
        with suppress(Exception):
            self._mcp.close_operation(run_id)
        if attempt_id:
            with suppress(Exception):
                attempt = next(
                    item
                    for item in self._store.run_attempts(run_id)
                    if item.attempt_id == attempt_id
                )
                assessment = RetryAssessment(
                    outcome=AttemptOutcome.PERMANENT_FAILURE,
                    reason_code="RUN_MONITOR_FAILED",
                    retry_allowed=False,
                    failure_kind="monitor",
                )
                self._finish_attempt_record(
                    attempt,
                    assessment,
                    events=[],
                    draft_present=False,
                    result_present=False,
                    released=False,
                )
        with suppress(Exception):
            self._finish_logical_failure(
                run_id,
                "WORKER_RELEASE_UNCONFIRMED",
                released=False,
            )

    def _finish_from_persisted_attempt(self, run: RunView, attempt: RunAttemptView) -> None:
        if run.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_FALLBACK, RunStatus.FAILED}:
            return
        assessment = RetryAssessment(
            outcome=AttemptOutcome(attempt.outcome),
            reason_code=attempt.reason_code or "LIVE_TASK_FAILED",
            retry_allowed=attempt.retry_allowed,
            failure_kind="reconciled",
        )
        self._resolve_terminal_failure(run.run_id, attempt, assessment, final_answer=None)

    def _observe_task(
        self,
        run_id: str,
        task_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        result = self._adapter.task(task_id)
        events = parse_sse_events(self._adapter.task_events_text(task_id))
        self._record_safe_events(run_id, events)
        task_status = str(result.get("status") or "")
        has_terminal_event = terminal_event_observed(events, task_status)
        task_in_queue = queue_contains_task(self._adapter.tasks(), task_id)
        return result, events, has_terminal_event and not task_in_queue

    def _record_safe_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        for event in events:
            event_type = str(event.get("type") or "")
            source = str(event.get("source") or "")
            raw_data = event.get("data")
            data: dict[str, Any] = (
                {str(key): value for key, value in raw_data.items()}
                if isinstance(raw_data, dict)
                else {}
            )
            if source == "tools":
                tool_name = str(data.get("tool") or data.get("name") or "")
                if tool_name not in ALLOWED_PROVIDER_TOOLS:
                    continue
                safe_type = "run.tool_completed"
                safe_data: dict[str, Any] = {"tool": tool_name}
            elif event_type in {
                "task_started",
                "task_done",
                "task_result",
                "task_terminal_timeout",
                "safety_check",
                "llm_usage",
            }:
                safe_type = "run.stage"
                safe_data = {
                    "stage": event_type,
                    "category": str(data.get("category") or ""),
                }
            else:
                continue
            event_hash = hashlib.sha256(
                json.dumps(
                    {"type": event_type, "source": source, "data": safe_data},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:24]
            self._store.append_run_event(
                run_id,
                event_key=f"task.{event_hash}",
                event_type=safe_type,
                data=safe_data,
            )

    def _assert_attempt_identity(
        self,
        run: RunView,
        attempt: RunAttemptView,
        admission: TaskAdmission,
    ) -> None:
        if (
            run.prompt_hash != admission.prompt_hash
            or run.skill_content_hash != admission.skill_content_hash
            or run.tool_inventory_hash != admission.tool_inventory_hash
            or attempt.provider != self._provider_identity
            or attempt.model != self._model_identity
            or attempt.provider_profile != self._provider_profile
            or attempt.context_digest != run.context_version
        ):
            raise RuntimeError("managed retry identity drifted")

    def _build_attempt_payload(
        self,
        run: RunView,
        attempt: RunAttemptView,
        admission: TaskAdmission,
    ) -> dict[str, Any]:
        return build_campaign_task(
            task_id=attempt.task_id,
            run_id=run.run_id,
            campaign_id=run.campaign_id,
            operation=run.operation,
            iteration=run.iteration,
            idempotency_key=self._idempotency_key(run.run_id),
            context_version=run.context_version,
            project_id=run.project_id,
            admission=admission,
            timeout_sec=self._task_timeout_seconds,
        )

    def _aggregate_attempt_audit(self, run_id: str) -> tuple[list[str], dict[str, Any]]:
        attempts = self._store.run_attempts(run_id)
        receipts = [receipt for attempt in attempts for receipt in attempt.tool_receipts]
        if len(attempts) == 1:
            return receipts, dict(attempts[0].provider_call_ledger)
        categories: dict[str, dict[str, Any]] = {}
        for attempt in attempts:
            for category, raw in attempt.provider_call_ledger.items():
                if not isinstance(raw, dict):
                    continue
                target = categories.setdefault(category, {})
                for key, value in raw.items():
                    if isinstance(value, bool):
                        target[key] = bool(target.get(key, False)) or value
                    elif isinstance(value, int | float):
                        target[key] = target.get(key, 0) + value
                    elif isinstance(value, list):
                        combined = [*(target.get(key, []) or []), *value]
                        target[key] = sorted({str(item) for item in combined if item})
                    elif key not in target:
                        target[key] = value
        for row in categories.values():
            if "cost_usd" in row:
                row["cost_usd"] = round(float(row["cost_usd"]), 8)
        logical: dict[str, Any] = {str(key): value for key, value in categories.items()}
        logical["attempts"] = [
            {
                "attempt_id": attempt.attempt_id,
                "attempt_number": attempt.attempt_number,
                "task_id": attempt.task_id,
                "outcome": attempt.outcome,
                "reason_code": attempt.reason_code,
                "failure_kind": attempt.failure_kind,
                "usage_status": attempt.usage_status,
                "ledger": attempt.provider_call_ledger,
            }
            for attempt in attempts
        ]
        return receipts, logical

    def _bounded_retry_delay(self, retry_after_seconds: float | None) -> float:
        requested = (
            self._retry_backoff_seconds
            if retry_after_seconds is None
            else max(0.0, retry_after_seconds)
        )
        return min(requested, self._retry_after_cap_seconds)

    @staticmethod
    def _idempotency_key(run_id: str) -> str:
        return f"operation-{hashlib.sha256(run_id.encode()).hexdigest()}"
