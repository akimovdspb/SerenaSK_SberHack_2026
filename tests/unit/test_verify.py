from __future__ import annotations

import pathlib
from collections.abc import Sequence

import pytest

from scripts.verify import (
    CORE_COMMANDS,
    RELEASE_STATUSES,
    VerificationError,
    path_state,
    run_core,
    safe_environment,
    validate_backup_binding,
    validate_documentation,
    validate_readme_release_status,
)


def test_core_manifest_has_no_paid_or_evidence_creation_surface() -> None:
    flattened = " ".join(" ".join(item.command) for item in CORE_COMMANDS)
    environment = safe_environment(
        {
            "OPENAI_API_KEY": "not-a-real-key",
            "ALLOW_LIVE_EVAL": "true",
            "EVALUATION_ID": "must-not-propagate",
            "CONTROLLED_PROVIDER_RETRY_ENABLED": "true",
            "CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE": "transient_twice",
            "PATH": "/usr/bin",
        }
    )

    assert "eval-live" not in flattened
    assert "scripts.live_evaluation" not in flattened
    assert "scripts.evidence" not in flattened
    assert "OPENAI_API_KEY" not in environment
    assert "EVALUATION_ID" not in environment
    assert environment["ALLOW_LIVE_EVAL"] == "false"
    assert environment["CONTROLLED_PROVIDER_RETRY_ENABLED"] == "false"
    assert environment["CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE"] == "none"
    assert environment["PATH"] == "/usr/bin"


def test_core_runner_detects_no_protected_state_mutation(tmp_path: pathlib.Path) -> None:
    protected = tmp_path / "usage.jsonl"
    protected.write_text("stable\n", encoding="utf-8")
    seen: list[tuple[str, ...]] = []

    def passing(command: Sequence[str], environment: dict[str, str]) -> int:
        seen.append(tuple(command))
        assert environment["ALLOW_LIVE_EVAL"] == "false"
        return 0

    report = run_core(
        command_runner=passing,
        report_path=tmp_path / "report.json",
        protected_paths=(protected,),
    )

    assert report["status"] == "PASS"
    assert report["protected_state_unchanged"] is True
    assert len(seen) == len(CORE_COMMANDS)
    assert path_state(protected).startswith("file:")


def test_current_documentation_has_exact_four_step_quickstart() -> None:
    result = validate_documentation()

    assert result["startup_step_count"] == 4
    assert result["required_file_count"] == 16
    assert result["release_status"] == "IMPLEMENTATION_COMPLETE"


@pytest.mark.parametrize("status", RELEASE_STATUSES)
def test_readme_accepts_each_canonical_release_status(status: str) -> None:
    assert validate_readme_release_status(f"Текущий release status:\n`{status}`.") == status


def test_readme_rejects_multiple_release_statuses() -> None:
    with pytest.raises(VerificationError, match="exactly one"):
        validate_readme_release_status(
            "Текущий release status:\n`IMPLEMENTATION_COMPLETE` and `WAITING_FOR_OPERATOR`."
        )


def test_release_backup_must_bind_commit_archive_and_selected_evidence() -> None:
    report = {
        "git_commit": "a" * 40,
        "archive_sha256": "b" * 64,
        "provider_calls": 0,
    }
    validated = {
        "git_commit": "a" * 40,
        "archive_sha256": "b" * 64,
        "evidence_evaluation_ids": ["evaluation-release-001"],
    }

    validate_backup_binding(
        report,
        validated,
        commit="a" * 40,
        evaluation_id="evaluation-release-001",
    )

    validated["evidence_evaluation_ids"] = []
    with pytest.raises(VerificationError, match="not bound"):
        validate_backup_binding(
            report,
            validated,
            commit="a" * 40,
            evaluation_id="evaluation-release-001",
        )
