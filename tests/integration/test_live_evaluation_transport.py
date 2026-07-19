from __future__ import annotations

import pathlib
from typing import Any

import pytest

from apps.api.app import live_evaluation_transport
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.workflow.store import WorkflowStore
from scripts.evaluation import evaluate_live_case_report
from tests.factories import deterministic_live_operation_adapter


def _store(tmp_path: pathlib.Path) -> WorkflowStore:
    store = WorkflowStore(
        f"sqlite:///{tmp_path / 'factory.db'}",
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=tmp_path / "artifacts",
    )
    store.initialize()
    return store


@pytest.mark.integration
def test_b01_b03_live_transport_lifecycle_keeps_test_approvals_and_rolls_back(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(
        live_evaluation_transport,
        "run_operation",
        deterministic_live_operation_adapter(),
    )

    b01 = live_evaluation_transport._b01(store, "test-evaluation")

    assert b01["ok"] is True
    assert b01["mode"] == "live_ouroboros"
    assert len(b01["operations"]) == 3
    assert b01["metrics"]["provider_calls"] == 3
    assert b01["learning"]["rule_approval"]["test_only"] is True
    assert b01["learning"]["package_approval"]["test_only"] is True
    assert pathlib.Path(b01["learning"]["campaign_export_container_path"]).is_file()
    active_ids, active_version = store.active_rule_state()
    assert active_ids == (b01["learning"]["rule_approval"]["rule_version_id"],)

    b03 = live_evaluation_transport._b03(
        store,
        "test-evaluation",
        rule_version_id=active_ids[0],
        active_rules_version=active_version,
    )

    assert b03["ok"] is True
    assert b03["context"]["content_plan"]["applied_rule_version_ids"] == [active_ids[0]]
    assert b03["learning"]["second_case_application"]["rule_version_id"] == active_ids[0]
    assert b03["learning"]["rollback"]["status"] == "ROLLED_BACK"
    assert store.active_rule_state()[0] == ()


@pytest.mark.integration
def test_b15_live_transport_creates_targeted_second_package(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(
        live_evaluation_transport,
        "run_operation",
        deterministic_live_operation_adapter(),
    )

    report = live_evaluation_transport._b15(store, "test-evaluation")

    assert report["ok"] is True
    assert report["mode"] == "live_ouroboros"
    assert len(report["operations"]) == 2
    assert report["metrics"]["provider_calls"] == 2
    revision = report["learning"]["b15_revision"]
    assert revision["package_v1"]["package_hash"] != revision["package_v2"]["package_hash"]
    assert revision["diff"]["changed_paths"] == [
        "/email/plain_text",
        "/email/sections/0/body",
    ]


@pytest.mark.integration
def test_no_provider_transport_projection_passes_all_live_case_assertions(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(
        live_evaluation_transport,
        "run_operation",
        deterministic_live_operation_adapter(),
    )
    raw: dict[str, dict[str, Any]] = {
        "B02": live_evaluation_transport._standard_case(store, "B02", "test-evaluation")
    }
    raw["B01"] = live_evaluation_transport._b01(store, "test-evaluation")
    active_ids, active_version = store.active_rule_state()
    raw["B03"] = live_evaluation_transport._b03(
        store,
        "test-evaluation",
        rule_version_id=active_ids[0],
        active_rules_version=active_version,
    )
    for case_id in (
        "B04",
        "B05",
        "B06",
        "B07",
        "B08",
        "B09",
        "B10",
        "B11",
        "B12",
        "B13",
        "B14",
    ):
        raw[case_id] = live_evaluation_transport._standard_case(
            store,
            case_id,
            "test-evaluation",
        )
    raw["B15"] = live_evaluation_transport._b15(store, "test-evaluation")

    outcomes = {case_id: evaluate_live_case_report(report) for case_id, report in raw.items()}

    assert set(outcomes) == {f"B{ordinal:02d}" for ordinal in range(1, 16)}
    assert all(outcome["passed"] is True for outcome in outcomes.values())
    assert sum(outcome["mode"] == "live_ouroboros" for outcome in outcomes.values()) == 12
    assert sum(outcome["mode"] == "validation_only" for outcome in outcomes.values()) == 3
