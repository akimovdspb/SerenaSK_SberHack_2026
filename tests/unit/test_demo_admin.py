from __future__ import annotations

import pathlib

import pytest

from apps.api.app.demo_admin import DemoAdminError, reset_demo_state
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.workflow.store import WorkflowStore


def test_demo_reset_removes_only_mutable_database_and_exports(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "factory.db"
    artifacts = tmp_path / "artifacts"
    store = WorkflowStore(
        f"sqlite:///{database}",
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=artifacts,
    )
    store.initialize()
    campaign = store.create_campaign(brief=None, case_id="B04")
    store.validate_campaign(campaign.campaign_id)
    store.run_deterministic(campaign.campaign_id)
    (artifacts / "sentinel.txt").write_text("mutable", encoding="utf-8")

    result = reset_demo_state(
        database_url=f"sqlite:///{database}",
        artifacts_dir=artifacts,
        data_root=tmp_path,
    )

    assert result["status"] == "PASS"
    assert result["catalog_case_count"] == 15
    assert result["observed_case_count"] == 0
    assert not (artifacts / "sentinel.txt").exists()


def test_demo_reset_refuses_paths_outside_data_root(tmp_path: pathlib.Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    with pytest.raises(DemoAdminError, match="escapes"):
        reset_demo_state(
            database_url=f"sqlite:///{tmp_path / 'outside.db'}",
            artifacts_dir=allowed / "artifacts",
            data_root=allowed,
        )
