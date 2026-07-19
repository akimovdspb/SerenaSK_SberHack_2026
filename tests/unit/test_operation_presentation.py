from __future__ import annotations

import pathlib

import pytest

from apps.api.app.domain.workflow import RunStatus
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.workflow.store import WorkflowStore


def _ready_campaign(store: WorkflowStore) -> str:
    campaign = store.create_campaign(brief=None, case_id="B04")
    store.validate_campaign(campaign.campaign_id)
    return campaign.campaign_id


def _queued_run(
    store: WorkflowStore,
    campaign_id: str,
    *,
    operation: str,
    ordinal: int,
):
    context = store.get_current_context(campaign_id)
    return store.create_live_run(
        run_id=f"run_presentation_{ordinal:02d}",
        campaign_id=campaign_id,
        operation=operation,
        iteration=1,
        task_id=f"task_presentation_{ordinal:02d}",
        project_id=f"project_presentation_{ordinal:02d}",
        context_version=context.context_version,
        prompt_hash="a" * 64,
        skill_content_hash="b" * 64,
        tool_inventory_hash="c" * 64,
        attempt_id=f"attempt_presentation_{ordinal:02d}",
        provider="openai",
        model="gpt-test",
        provider_profile="test",
    )


@pytest.mark.parametrize(
    ("operation", "title"),
    [
        ("initial", "Ouroboros создаёт комплект"),
        ("revision", "Ouroboros создаёт точечную версию"),
        ("rule_proposal", "Ouroboros формирует проект правила"),
    ],
)
def test_workspace_exposes_one_reload_safe_operation_presentation(
    tmp_path: pathlib.Path,
    operation: str,
    title: str,
) -> None:
    store = WorkflowStore(
        f"sqlite:///{tmp_path / 'factory.db'}",
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "artifacts",
    )
    store.initialize()
    campaign_id = _ready_campaign(store)
    run = _queued_run(store, campaign_id, operation=operation, ordinal=1)

    presentation = store.workspace(campaign_id).operation_state

    assert presentation is not None
    assert presentation.run_id == run.run_id
    assert presentation.operation == operation
    assert presentation.status is RunStatus.QUEUED
    assert presentation.mode == "live_ouroboros"
    assert presentation.active is True
    assert presentation.title == title
    assert presentation.stage == "accepted"
    assert presentation.stage_label == "Запуск принят, задача ожидает выполнения"
    assert presentation.attempt_number == 1
    assert presentation.elapsed_from == run.created_at
    assert presentation.result_hint == "Результат появится здесь после сохранения."
    assert presentation.reason_code is None


def test_operation_presentation_keeps_logical_run_busy_across_retry_and_cancel(
    tmp_path: pathlib.Path,
) -> None:
    store = WorkflowStore(
        f"sqlite:///{tmp_path / 'factory.db'}",
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "artifacts",
    )
    store.initialize()
    campaign_id = _ready_campaign(store)
    run = _queued_run(store, campaign_id, operation="initial", ordinal=2)
    first_attempt = run.attempts[0]
    store.mark_run_started(run.run_id)
    store.finish_attempt(
        first_attempt.attempt_id,
        outcome="TRANSIENT_FAILURE",
        reason_code="TEMPORARY_PROVIDER_FAILURE",
        failure_kind="transport",
        retry_allowed=True,
        tool_receipts=[],
        provider_call_ledger={},
        usage_status="EXACT",
        draft_present=False,
        result_present=False,
        released=True,
    )
    store.prepare_retry_attempt(
        run.run_id,
        attempt_id="attempt_presentation_retry_02",
        task_id="task_presentation_retry_02",
        request_digest=first_attempt.request_digest,
    )

    retry = store.workspace(campaign_id).operation_state
    assert retry is not None
    assert retry.run_id == run.run_id
    assert retry.active is True
    assert retry.status is RunStatus.RUNNING
    assert retry.attempt_number == 2
    assert retry.stage == "retry_scheduled"
    assert retry.stage_label == "Временный сбой, готовим попытку 2 из 2"

    store.request_run_cancel(run.run_id)
    cancelling = store.workspace(campaign_id).operation_state
    assert cancelling is not None
    assert cancelling.run_id == run.run_id
    assert cancelling.active is True
    assert cancelling.status is RunStatus.CANCEL_REQUESTED
    assert cancelling.stage == "cancel_requested"
    assert cancelling.stage_label == "Передаём запрос на отмену"

    store.finish_run(
        run.run_id,
        status=RunStatus.CANCELLED,
        reason_code="USER_CANCELLED",
        mode="live_ouroboros",
        tool_receipts=[],
        provider_call_ledger={},
        final_answer=None,
    )
    cancelled = store.workspace(campaign_id).operation_state
    assert cancelled is not None
    assert cancelled.active is False
    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.stage == "cancelled"
    assert cancelled.stage_label == "Операция отменена"
    assert cancelled.reason_code == "USER_CANCELLED"


def test_operation_presentation_reports_terminal_failure_without_ready_result(
    tmp_path: pathlib.Path,
) -> None:
    store = WorkflowStore(
        f"sqlite:///{tmp_path / 'factory.db'}",
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "artifacts",
    )
    store.initialize()
    campaign_id = _ready_campaign(store)
    run = _queued_run(store, campaign_id, operation="initial", ordinal=3)
    store.finish_run(
        run.run_id,
        status=RunStatus.COMPLETED,
        reason_code=None,
        mode="live_ouroboros",
        tool_receipts=[],
        provider_call_ledger={},
        final_answer=None,
    )

    failed = store.workspace(campaign_id).operation_state
    assert failed is not None
    assert failed.active is False
    assert failed.status is RunStatus.FAILED
    assert failed.stage == "failed"
    assert failed.stage_label == "Операция завершилась с ошибкой"  # noqa: RUF001
    assert failed.reason_code == "DRAFT_NOT_PERSISTED"
