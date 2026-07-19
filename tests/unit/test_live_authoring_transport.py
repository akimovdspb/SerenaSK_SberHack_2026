from __future__ import annotations

from pathlib import Path

from apps.api.app.domain.workflow import CampaignState
from apps.api.app.live_authoring_transport import _reference_brief
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.workflow.store import WorkflowStore


def _workflow(tmp_path: Path) -> WorkflowStore:
    workflow = WorkflowStore(
        f"sqlite:///{tmp_path / 'authoring-transport.db'}",
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "artifacts",
    )
    workflow.initialize()
    return workflow


def test_reference_transport_uses_catalog_product_without_saved_output(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path)

    case_id, brief = _reference_brief(workflow, "editorial_dq01")
    campaign = workflow.create_campaign(brief=brief, case_id=None)
    validated = workflow.validate_campaign(campaign.campaign_id)

    assert case_id == "DQ01"
    assert brief.product_id == "synthetic_payroll"
    assert validated.state is CampaignState.READY


def test_reference_transport_materializes_custom_fact_card_before_brief(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)

    case_id, brief = _reference_brief(workflow, "editorial_dq03")
    first_product_id = brief.product_id
    _, repeated = _reference_brief(workflow, "editorial_dq03")
    campaign = workflow.create_campaign(brief=brief, case_id=None)
    validated = workflow.validate_campaign(campaign.campaign_id)

    assert case_id == "DQ03"
    assert first_product_id is not None
    assert repeated.product_id == first_product_id
    assert validated.state is CampaignState.READY
