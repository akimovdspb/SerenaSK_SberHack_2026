from __future__ import annotations

import pytest

from scripts.package_submission import (
    SubmissionError,
    _fixture_documents,
    validate_operator_documents,
    validate_pipeline_readiness,
)


def test_submission_pipeline_dry_run_fixture_exercises_all_human_schemas() -> None:
    result = validate_pipeline_readiness()

    assert result == {
        "status": "PASS",
        "schema_version": 1,
        "dry_run_fixture": True,
        "review_count": 6,
        "signoff_count": 2,
        "rehearsal_count": 2,
        "real_package_requires_verify_submission": True,
    }


def test_real_submission_rejects_fixture_or_test_only_approval() -> None:
    documents = _fixture_documents()
    for document in documents.values():
        document["test_fixture"] = False
    documents["approvals.json"]["package"]["test_only"] = True

    with pytest.raises(SubmissionError):
        validate_operator_documents(
            documents,
            expected_evaluation_id="fixture-evaluation",
            allow_fixture=False,
        )
