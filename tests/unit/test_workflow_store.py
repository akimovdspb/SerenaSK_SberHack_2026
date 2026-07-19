from __future__ import annotations

import hashlib
import json
import pathlib
import zipfile

import pytest

from apps.api.app.domain.campaigns import CampaignBriefInput, ContextBundle
from apps.api.app.domain.models import CommunicationBundle
from apps.api.app.domain.quality import (
    Finding,
    FindingArtifact,
    FindingSeverity,
    QualityReport,
)
from apps.api.app.domain.workflow import (
    ApprovalDecision,
    ApprovalRequest,
    CampaignState,
)
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.workflow import store as store_module
from apps.api.app.workflow.store import (
    WorkflowConflict,
    WorkflowInvalidState,
    WorkflowStore,
)


@pytest.fixture
def store(tmp_path: pathlib.Path) -> WorkflowStore:
    value = WorkflowStore(
        f"sqlite:///{tmp_path / 'factory.db'}",
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "artifacts",
    )
    value.initialize()
    return value


def test_ready_campaign_package_survives_store_restart(
    store: WorkflowStore,
    tmp_path: pathlib.Path,
) -> None:
    created = store.create_campaign(brief=None, case_id="B04")
    validated = store.validate_campaign(created.campaign_id)
    package = store.run_deterministic(created.campaign_id)

    assert created.state is CampaignState.DRAFT
    assert validated.state is CampaignState.READY
    assert validated.ready_brief is not None
    assert package.quality_report.approvable is True

    restarted = WorkflowStore(
        f"sqlite:///{tmp_path / 'factory.db'}",
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "artifacts",
    )
    restarted.initialize()
    persisted = restarted.get_campaign(created.campaign_id)

    assert persisted.state is CampaignState.APPROVABLE
    assert persisted.package_id == package.package_id
    assert restarted.get_package(package.package_id).package_hash == package.package_hash


@pytest.mark.parametrize(
    ("case_id", "state"),
    [
        ("B11", CampaignState.BLOCKED),
        ("B12", CampaignState.NOT_APPLICABLE),
        ("B13", CampaignState.NEEDS_INPUT),
    ],
)
def test_controlled_outcome_cannot_start_deterministic_run(
    store: WorkflowStore,
    case_id: str,
    state: CampaignState,
) -> None:
    created = store.create_campaign(brief=None, case_id=case_id)
    validated = store.validate_campaign(created.campaign_id)

    assert validated.state is state
    assert validated.validation is not None
    assert validated.validation.llm_calls == 0
    with pytest.raises(WorkflowInvalidState, match="no current ready brief"):
        store.run_deterministic(created.campaign_id)


def test_missing_cta_can_be_answered_as_a_new_immutable_draft_version(
    store: WorkflowStore,
) -> None:
    source = store.create_campaign(brief=None, case_id="B04")
    source_brief = source.draft.model_dump(
        mode="json", exclude={"campaign_id", "version", "input_hash"}
    )
    source_brief.update({"cta_label": None, "cta_url": None})
    created = store.create_campaign(
        brief=CampaignBriefInput.model_validate(source_brief),
        case_id=None,
    )

    incomplete = store.validate_campaign(created.campaign_id)
    patch = CampaignBriefInput(
        cta_label="Посмотреть детали",
        cta_url="https://flow.example.test/term-14",
    )
    updated = store.patch_brief(
        created.campaign_id,
        patch,
        fields_set=set(patch.model_fields_set),
    )
    ready = store.validate_campaign(created.campaign_id)

    assert incomplete.state is CampaignState.NEEDS_INPUT
    assert {question.question_id for question in incomplete.validation.questions} == {  # type: ignore[union-attr]
        "missing_cta_label",
        "missing_cta_url",
    }
    assert updated.draft_version == 2
    assert ready.state is CampaignState.READY
    assert ready.ready_brief is not None
    assert ready.ready_brief.version == 2


def test_approval_and_export_are_separate_and_archive_checksums_match(
    store: WorkflowStore,
) -> None:
    campaign = store.create_campaign(brief=None, case_id="B06")
    store.validate_campaign(campaign.campaign_id)
    package = store.run_deterministic(campaign.campaign_id)
    request = ApprovalRequest(
        package_hash=package.package_hash,
        decision=ApprovalDecision.APPROVED,
        test_only=True,
    )

    with pytest.raises(WorkflowConflict, match="hash confirmation"):
        store.approve_package(
            package.package_id,
            request.model_copy(update={"package_hash": "0" * 64}),
            actor_id="test_editor",
        )
    approval = store.approve_package(
        package.package_id,
        request,
        actor_id="test_editor",
    )

    assert store.get_campaign(campaign.campaign_id).state is CampaignState.APPROVED

    exported = store.export_package(package.package_id)
    archive_path = store.export_path(exported.export_id)

    assert approval.package_hash == package.package_hash
    assert exported.package_hash == package.package_hash
    assert store.get_campaign(campaign.campaign_id).state is CampaignState.EXPORTED
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert {
            "campaign.json",
            "brief.json",
            "run.json",
            "context-manifest.json",
            "fact-card.json",
            "rules-version.json",
            "sms/message.txt",
            "sms/metrics.json",
            "email/email.html",
            "email/email.txt",
            "email/content.json",
            "qa/findings.json",
            "qa/report.html",
            "feedback/feedback.json",
            "feedback/diff.json",
            "learning/rule-proposal.json",
            "trace/safe-events.jsonl",
            "trace/mcp-calls.jsonl",
            "trace/model-usage.json",
            "manifest.json",
            "README.txt",
        } == names
        assert manifest["synthetic"] is True
        assert manifest["no_send"] is True
        assert manifest["approval_hash"] == approval.approval_hash
        for name, expected in manifest["files"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected


def test_new_brief_version_invalidates_current_package_and_approval(
    store: WorkflowStore,
) -> None:
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
        actor_id="test_editor",
    )
    patch = CampaignBriefInput(tone="сдержанный")

    current = store.patch_brief(
        campaign.campaign_id,
        patch,
        fields_set=set(patch.model_fields_set),
    )

    assert current.state is CampaignState.DRAFT
    assert current.package_id is None
    with pytest.raises(WorkflowInvalidState, match="no longer the current"):
        store.export_package(package.package_id)


def test_package_with_qa_blocker_cannot_be_approved(
    store: WorkflowStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_evaluate = store_module.evaluate_bundle

    def blocked_report(
        bundle: CommunicationBundle,
        context: ContextBundle,
    ) -> QualityReport:
        report = real_evaluate(bundle, context)
        finding = Finding(
            finding_id="finding_test_blocker",
            check_id="QA18",
            severity=FindingSeverity.BLOCKER,
            artifact=FindingArtifact.PACKAGE,
            quote="99%",
            recommendation="Удалить неподтверждённое утверждение.",
            blocking=True,
        )
        payload = report.model_dump(mode="json")
        payload.update(
            {
                "findings": [finding.model_dump(mode="json")],
                "approvable": False,
                "deterministic_score": 80,
            }
        )
        return QualityReport.model_validate(payload)

    monkeypatch.setattr(store_module, "evaluate_bundle", blocked_report)
    campaign = store.create_campaign(brief=None, case_id="B04")
    store.validate_campaign(campaign.campaign_id)
    package = store.run_deterministic(campaign.campaign_id)

    assert package.quality_report.approvable is False
    assert store.get_campaign(campaign.campaign_id).state is CampaignState.REVIEW_REQUIRED
    with pytest.raises(WorkflowInvalidState, match="blockers cannot be approved"):
        store.approve_package(
            package.package_id,
            ApprovalRequest(
                package_hash=package.package_hash,
                decision=ApprovalDecision.APPROVED,
                test_only=True,
            ),
            actor_id="test_editor",
        )


def test_http_idempotency_replays_same_result_and_rejects_changed_body(
    store: WorkflowStore,
) -> None:
    calls = 0

    def create():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return store.create_campaign(brief=None, case_id="B04")

    first = store.execute_idempotent(
        scope="POST:/api/v1/campaigns",
        key="idempotency-key-0001",
        payload={"case_id": "B04"},
        operation=create,
    )
    replay = store.execute_idempotent(
        scope="POST:/api/v1/campaigns",
        key="idempotency-key-0001",
        payload={"case_id": "B04"},
        operation=create,
    )

    assert replay == first
    assert calls == 1
    with pytest.raises(WorkflowConflict, match="different request"):
        store.execute_idempotent(
            scope="POST:/api/v1/campaigns",
            key="idempotency-key-0001",
            payload={"case_id": "B06"},
            operation=create,
        )
