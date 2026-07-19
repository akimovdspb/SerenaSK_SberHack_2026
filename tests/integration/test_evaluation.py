from __future__ import annotations

import json
import pathlib

import pytest

from scripts.evaluation import (
    EvaluationError,
    expected_cases,
    review_packet_case_ids,
    run_replay_evaluation,
)


def test_deterministic_reference_covers_all_business_assertions_without_provider() -> None:
    report = run_replay_evaluation()

    assert report["status"] == "PASS"
    assert report["business_case_count"] == 15
    assert report["passed_case_count"] == 15
    assert report["expected_assertion_pass_rate"] == 1.0
    assert report["provider_calls"] == 0
    assert report["mode_counts"] == {
        "deterministic_template": 12,
        "validation_only": 3,
    }
    assert report["release_targets_passed"] is False
    assert report["live_case_count"] == 0
    assert review_packet_case_ids() == ("B01", "B02", "B03", "B04", "B07", "B08")
    cases = {item["case_id"]: item for item in report["cases"]}
    assert set(cases) == {f"B{ordinal:02d}" for ordinal in range(1, 16)}
    assert all(item["passed"] for item in cases.values())
    assert cases["B01"]["assertions"]["optional_concept_absent"] is True
    assert cases["B03"]["assertions"]["rule_version_evidenced"] is True
    assert cases["B07"]["assertions"]["utm_exact"] is True
    assert cases["B08"]["assertions"]["emoji_code_units_counted"] is True
    assert cases["B09"]["actual_channels"]["sms"] == "SUPPRESSED"
    assert cases["B10"]["actual_channels"]["email"] == "SUPPRESSED"
    assert cases["B15"]["assertions"]["protected_paths_unchanged"] is True
    assert report["learning"]["rule_approval"]["test_only"] is True
    assert report["learning"]["rollback"]["status"] == "ROLLED_BACK"


def test_expected_fixture_rejects_missing_or_duplicate_case(tmp_path: pathlib.Path) -> None:
    invalid = {
        "schema_version": 1,
        "cases": [
            {
                "case_id": "B01",
                "hard_assertions": ["grounded_package"],
            }
        ],
    }
    path = tmp_path / "business_expected.json"
    path.write_text(json.dumps(invalid), encoding="utf-8")

    with pytest.raises(EvaluationError, match="exactly B01-B15"):
        expected_cases(path)
