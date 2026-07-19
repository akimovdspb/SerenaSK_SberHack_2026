from __future__ import annotations

import pytest

from scripts.clean_clone import (
    REVIEW_SEED_PROGRAM,
    CleanCloneError,
    validate_rehearsal_environment,
    validate_rehearsal_plan,
)


def _environment() -> dict[str, str]:
    return {
        "ALLOW_CLEAN_CLONE_LIVE": "true",
        "CLEAN_CLONE_EVALUATION_ID": "clean-clone-fixture-new",
        "EVAL_PROVIDER_PROFILE": "openai-gpt-5.4-mini",
        "EVAL_MAX_TOKENS": "1000",
        "EVAL_MAX_COST_USD": "1",
        "EVAL_PROJECTED_TOKENS": "800",
        "EVAL_PROJECTED_COST_USD": "0.8",
        "EVAL_CONCURRENCY": "1",
    }


def test_clean_clone_plan_has_exact_startup_and_no_full_eval_or_review() -> None:
    report = validate_rehearsal_plan()

    assert report["status"] == "PASS"
    assert report["readme_startup_step_count"] == 4
    assert report["requires_explicit_live_caps"] is True
    assert not any("eval-live" in command for command in report["commands"])


def test_clean_clone_live_guard_rejects_host_key_or_missing_cap() -> None:
    environment = _environment()
    environment["OPENAI_API_KEY"] = "not-a-real-key"
    with pytest.raises(CleanCloneError, match="must not receive"):
        validate_rehearsal_environment(environment)

    environment = _environment()
    environment["EVAL_MAX_TOKENS"] = "0"
    with pytest.raises(CleanCloneError, match="positive integer"):
        validate_rehearsal_environment(environment)


def test_review_seed_makes_the_complete_target_volume_runtime_writable() -> None:
    assert "target_root = pathlib.Path('/target')" in REVIEW_SEED_PROGRAM
    assert "[target_root, *target_root.rglob('*')]" in REVIEW_SEED_PROGRAM
