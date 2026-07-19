from __future__ import annotations

import pathlib

from apps.api.app.domain.campaigns import CampaignBriefInput
from apps.api.app.domain.learning import FeedbackCreateRequest
from apps.api.app.domain.models import (
    ContextGetRequest,
    DraftSaveRequest,
    Operation,
    RuleScope,
)
from apps.api.app.mcp.service import FactoryMcpService
from apps.api.app.services.catalog import DEFAULT_DATA_DIR, load_catalog
from apps.api.app.services.revisions import build_deterministic_patch
from apps.api.app.services.rules import build_deterministic_rule_proposal
from apps.api.app.workflow.store import WorkflowStore


def _services(tmp_path: pathlib.Path) -> tuple[WorkflowStore, FactoryMcpService]:
    database_url = f"sqlite:///{tmp_path / 'factory.db'}"
    workflow = WorkflowStore(
        database_url,
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "artifacts",
    )
    mcp = FactoryMcpService(database_url, draft_processor=workflow)
    workflow.initialize()
    mcp.initialize()
    return workflow, mcp


def _b01_feedback(workflow: WorkflowStore) -> tuple[str, str, str]:
    campaign = workflow.create_campaign(brief=None, case_id="B01")
    workflow.validate_campaign(campaign.campaign_id)
    answers = CampaignBriefInput(
        cta_label="Собрать первый реестр",
        cta_url="https://pulse-pay.example.test/start",
    )
    workflow.patch_brief(
        campaign.campaign_id,
        answers,
        fields_set=set(answers.model_fields_set),
    )
    workflow.validate_campaign(campaign.campaign_id)
    v1 = workflow.run_deterministic(campaign.campaign_id)
    feedback = workflow.create_feedback(
        v1.package_id,
        FeedbackCreateRequest(
            artifact_path="/email/sections/0/body",
            comment="Добавьте payouts_via_online_bank.",
            scope="CURRENT_CHANNEL",
            author_role="editor",
        ),
        author_id="learning_mcp_editor",
    )
    return campaign.campaign_id, v1.package_id, feedback.feedback_id


def test_revision_and_rule_proposal_use_the_same_two_mcp_operations(
    tmp_path: pathlib.Path,
) -> None:
    workflow, mcp = _services(tmp_path)
    campaign_id, v1_id, feedback_id = _b01_feedback(workflow)
    revision_context = workflow.prepare_revision_context(v1_id, feedback_id)
    revision_key = "learning-revision-operation-0001"
    mcp.prepare_operation(
        run_id="run_learning_revision_0001",
        task_id="task_learning_revision_0001",
        project_id="project_learning_campaign",
        campaign_id=campaign_id,
        operation="revision",
        iteration=1,
        idempotency_key=revision_key,
        context=revision_context.model_dump(mode="json"),
    )
    revision_get = mcp.context_get(
        ContextGetRequest(
            campaign_id=campaign_id,
            operation=Operation.REVISION,
            iteration=1,
            context_version=revision_context.context_version,
            idempotency_key=revision_key,
        )
    )
    assert revision_get.ready is True
    assert revision_get.output_schema is not None
    assert revision_get.output_schema["title"] == "CommunicationPatchEnvelope"
    patch = build_deterministic_patch(revision_context)
    revision_save = mcp.draft_save(
        DraftSaveRequest.model_validate(
            {
                "campaign_id": campaign_id,
                "operation": "revision",
                "iteration": 1,
                "context_version": revision_context.context_version,
                "idempotency_key": revision_key,
                "draft": {
                    "kind": "communication_patch",
                    "schema_version": "1.0",
                    "campaign_id": campaign_id,
                    "operation": "revision",
                    "iteration": 1,
                    "context_version": revision_context.context_version,
                    "payload": patch.model_dump(mode="json"),
                },
            }
        )
    )
    assert revision_save.persisted is True
    v2_id = workflow.get_campaign(campaign_id).package_id
    assert v2_id is not None
    assert workflow.get_package(v2_id).package_version == 2
    assert workflow.get_package_diff(v2_id).feedback_id == feedback_id

    scope = RuleScope(
        product_ids=["synthetic_payroll"],
        channel="email",
        segment_ids=[],
    )
    rule_context = workflow.prepare_rule_proposal_context(feedback_id, scope)
    proposal = build_deterministic_rule_proposal(
        proposal_id="proposal_learning_mcp_0001",
        feedback=workflow.get_feedback(feedback_id),
        selected_scope=scope,
        base_rules_version=rule_context.rules_version,
        catalog=load_catalog(),
    )
    rule_key = "learning-rule-operation-0001"
    mcp.prepare_operation(
        run_id="run_learning_rule_0001",
        task_id="task_learning_rule_0001",
        project_id="project_learning_campaign",
        campaign_id=campaign_id,
        operation="rule_proposal",
        iteration=1,
        idempotency_key=rule_key,
        context=rule_context.model_dump(mode="json"),
    )
    rule_get = mcp.context_get(
        ContextGetRequest(
            campaign_id=campaign_id,
            operation=Operation.RULE_PROPOSAL,
            iteration=1,
            context_version=rule_context.context_version,
            idempotency_key=rule_key,
        )
    )
    assert rule_get.ready is True
    assert rule_get.output_schema is not None
    assert rule_get.output_schema["title"] == "RuleProposalEnvelope"
    rule_save = mcp.draft_save(
        DraftSaveRequest.model_validate(
            {
                "campaign_id": campaign_id,
                "operation": "rule_proposal",
                "iteration": 1,
                "context_version": rule_context.context_version,
                "idempotency_key": rule_key,
                "draft": {
                    "kind": "rule_proposal",
                    "schema_version": "1.0",
                    "campaign_id": campaign_id,
                    "operation": "rule_proposal",
                    "iteration": 1,
                    "context_version": rule_context.context_version,
                    "payload": {
                        "type": proposal.type.value,
                        "condition_id": proposal.condition_id,
                        "value": proposal.value,
                        "rationale": proposal.rationale,
                        "risk": proposal.risk,
                    },
                },
            }
        )
    )
    assert rule_save.persisted is True
    saved_proposals = workflow.workspace(campaign_id).rule_proposals
    assert len(saved_proposals) == 1
    saved_proposal = saved_proposals[0]
    assert saved_proposal.status.value == "READY_FOR_APPROVAL"
    assert saved_proposal.proposal.scope == scope
    assert saved_proposal.proposal.source_feedback_id == feedback_id
    assert saved_proposal.proposal.target_case_ids == ["B03"]
    assert saved_proposal.proposal.candidate_rules_version != rule_context.rules_version

    revision_snapshot = mcp.probe_snapshot(campaign_id, operation="revision", iteration=1)
    rule_snapshot = mcp.probe_snapshot(campaign_id, operation="rule_proposal", iteration=1)
    assert revision_snapshot["draft"]["envelope"]["kind"] == "communication_patch"
    assert rule_snapshot["draft"]["envelope"]["kind"] == "rule_proposal"
    assert set(rule_snapshot["draft"]["envelope"]["payload"]) == {
        "condition_id",
        "rationale",
        "risk",
        "type",
        "value",
    }
