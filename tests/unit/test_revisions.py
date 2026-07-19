from __future__ import annotations

import pathlib

import pytest

from apps.api.app.domain.campaigns import CampaignBriefInput, ContextBundle
from apps.api.app.domain.learning import FeedbackCreateRequest, FeedbackView
from apps.api.app.domain.models import Channel
from apps.api.app.domain.workflow import ApprovalDecision, ApprovalRequest, PackageView
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.services.revisions import (
    RevisionError,
    build_deterministic_patch,
    merge_communication_patch,
)
from apps.api.app.workflow.store import (
    WorkflowInvalidState,
    WorkflowNotFound,
    WorkflowStore,
)


def _prepared_revision(
    tmp_path: pathlib.Path,
) -> tuple[WorkflowStore, PackageView, FeedbackView, ContextBundle]:
    store = WorkflowStore(
        f"sqlite:///{tmp_path / 'factory.db'}",
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "artifacts",
    )
    store.initialize()
    campaign = store.create_campaign(brief=None, case_id="B01")
    store.validate_campaign(campaign.campaign_id)
    answers = CampaignBriefInput(
        cta_label="Собрать первый реестр",
        cta_url="https://pulse-pay.example.test/start",
    )
    store.patch_brief(
        campaign.campaign_id,
        answers,
        fields_set=set(answers.model_fields_set),
    )
    store.validate_campaign(campaign.campaign_id)
    package = store.run_deterministic(campaign.campaign_id)
    feedback = store.create_feedback(
        package.package_id,
        FeedbackCreateRequest(
            artifact_path="/email/sections/0/body",
            comment="Добавьте payouts_via_online_bank.",
            scope="CURRENT_FIELD",
            author_role="editor",
        ),
        author_id="revision_test_editor",
    )
    context = store.prepare_revision_context(package.package_id, feedback.feedback_id)
    return store, package, feedback, context


def test_revision_merge_accepts_exact_declared_field_and_full_qa(
    tmp_path: pathlib.Path,
) -> None:
    _, package, _, context = _prepared_revision(tmp_path)
    patch = build_deterministic_patch(context)

    assert context.content_plan.channel_selected_fact_ids
    assert "fact_payroll_statuses" not in context.content_plan.fact_ids_for(Channel.SMS)

    result = merge_communication_patch(
        context=context,
        patch=patch,
        current_package_hash=package.package_hash,
    )

    assert result.changed_paths == ("/email/sections/0/body",)
    assert result.report.approvable is True
    assert result.report.findings == ()
    assert result.bundle.email is not None
    assert "подготовка выплат в онлайн-банке" in result.bundle.email.sections[0].body


def test_revision_merge_rejects_stale_declared_mismatch_and_out_of_scope(
    tmp_path: pathlib.Path,
) -> None:
    _, package, _, context = _prepared_revision(tmp_path)
    patch = build_deterministic_patch(context)

    with pytest.raises(RevisionError) as stale:
        merge_communication_patch(
            context=context,
            patch=patch,
            current_package_hash="0" * 64,
        )
    assert stale.value.code == "STALE_BASE_PACKAGE"

    mismatch = patch.model_copy(
        update={"changed_paths": [*patch.changed_paths, "/email/preheader"]}
    )
    with pytest.raises(RevisionError) as declared:
        merge_communication_patch(
            context=context,
            patch=mismatch,
            current_package_hash=package.package_hash,
        )
    assert declared.value.code == "REVISION_SCOPE_VIOLATION"

    duplicate = patch.model_copy(
        update={"changed_paths": [*patch.changed_paths, *patch.changed_paths]}
    )
    with pytest.raises(RevisionError) as repeated:
        merge_communication_patch(
            context=context,
            patch=duplicate,
            current_package_hash=package.package_hash,
        )
    assert repeated.value.code == "REVISION_SCOPE_VIOLATION"

    assert patch.email is not None
    out_of_scope = patch.model_copy(
        update={
            "email": patch.email.model_copy(update={"subject": "Новая запрещённая тема"}),
            "changed_paths": [*patch.changed_paths, "/email/subject"],
        }
    )
    with pytest.raises(RevisionError) as scope:
        merge_communication_patch(
            context=context,
            patch=out_of_scope,
            current_package_hash=package.package_hash,
        )
    assert scope.value.code == "REVISION_SCOPE_VIOLATION"


def test_feedback_does_not_create_rule_proposal_or_mutate_v1(tmp_path: pathlib.Path) -> None:
    store, package, feedback, _ = _prepared_revision(tmp_path)

    assert store.get_package(package.package_id).package_hash == package.package_hash
    with pytest.raises(WorkflowNotFound, match="rule proposal was not found"):
        store.get_rule_proposal(feedback.feedback_id)


def test_saved_feedback_invalidates_prior_approval_and_export(tmp_path: pathlib.Path) -> None:
    store = WorkflowStore(
        f"sqlite:///{tmp_path / 'factory.db'}",
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "artifacts",
    )
    store.initialize()
    campaign = store.create_campaign(brief=None, case_id="B04")
    store.validate_campaign(campaign.campaign_id)
    package = store.run_deterministic(campaign.campaign_id)
    store.approve_package(
        package.package_id,
        ApprovalRequest(
            package_hash=package.package_hash,
            decision=ApprovalDecision.APPROVED,
            test_only=True,
        ),
        actor_id="revision_test_approver",
    )
    store.create_feedback(
        package.package_id,
        FeedbackCreateRequest(
            artifact_path="/email/headline",
            comment="Сделайте заголовок яснее без изменения фактов.",
            scope="CURRENT_FIELD",
            author_role="editor",
        ),
        author_id="revision_test_editor",
    )

    with pytest.raises(WorkflowInvalidState, match="no longer the current"):
        store.export_package(package.package_id)
