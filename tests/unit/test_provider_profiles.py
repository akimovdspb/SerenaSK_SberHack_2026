from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.api.app.settings import Settings
from provider_profiles import (
    CAMPAIGN_AUTHORING_PROFILE_NAME,
    CANONICAL_PROFILE_NAME,
    EXACT_P0_TOOLS,
    GLM_FUNCTIONAL_PROFILE_NAME,
    ProviderProfileError,
    provider_profile,
    requested_provider_profile,
)


def test_canonical_profile_preserves_release_defaults() -> None:
    profile = provider_profile(CANONICAL_PROFILE_NAME)

    assert profile.runtime_route == "openai::gpt-5.4-mini"
    assert profile.ledger_provider == "openai"
    assert profile.normalized_model == "gpt-5.4-mini"
    assert profile.task_timeout_seconds == 25
    assert profile.terminal_deadline_seconds == 29
    assert profile.effective_task_timeout_seconds == 25
    assert profile.effective_terminal_deadline_seconds == 29
    assert profile.main_loop_max_tokens == 65_536
    assert profile.safety_call_timeout_seconds == 5
    assert profile.tool_call_timeout_seconds == 5
    assert profile.require_post_task_summary is True
    assert profile.functional_latency_gap_allowed is False
    assert profile.allowed_tools == EXACT_P0_TOOLS
    assert profile.required_branch == "codex/p0-autonomous"


def test_glm_profile_is_exact_and_has_no_fallback() -> None:
    profile = provider_profile(GLM_FUNCTIONAL_PROFILE_NAME)

    assert profile.runtime_route == "openrouter::z-ai/glm-5.2"
    assert profile.runtime_provider == "openrouter"
    assert profile.ledger_provider == "openrouter"
    assert profile.normalized_model == "z-ai/glm-5.2"
    assert profile.reasoning_effort == "low"
    assert profile.fallbacks == ()
    assert profile.task_timeout_seconds == 180
    assert profile.terminal_deadline_seconds == 195
    assert profile.effective_task_timeout_seconds == 330
    assert profile.effective_terminal_deadline_seconds == 360
    assert profile.main_loop_max_tokens == 10_240
    assert profile.safety_call_timeout_seconds == 20
    assert profile.tool_call_timeout_seconds == 30
    assert profile.require_post_task_summary is False
    assert profile.functional_latency_gap_allowed is True
    assert profile.allowed_tools == EXACT_P0_TOOLS
    assert profile.pilot_case_ids == ("B04", "B07", "B08", "B14", "B15")
    assert profile.required_branch == "codex/p0-glm-basket"


def test_campaign_authoring_profile_has_goal_scoped_deadlines_and_branch() -> None:
    profile = provider_profile(CAMPAIGN_AUTHORING_PROFILE_NAME)

    assert profile.runtime_route == "openrouter::z-ai/glm-5.2"
    assert profile.ledger_provider == "openrouter"
    assert profile.normalized_model == "z-ai/glm-5.2"
    assert profile.reasoning_effort == "low"
    assert profile.fallbacks == ()
    assert profile.task_timeout_seconds == 600
    assert profile.terminal_deadline_seconds == 900
    assert profile.effective_task_timeout_seconds == 600
    assert profile.effective_terminal_deadline_seconds == 900
    assert profile.main_loop_max_tokens == 16_384
    assert profile.allowed_tools == EXACT_P0_TOOLS
    assert profile.required_branch == "codex/campaign-authoring-quality-v3-20260717"


def test_profile_selection_is_explicit_and_fail_closed() -> None:
    with pytest.raises(ProviderProfileError, match="must name an explicit"):
        requested_provider_profile({})

    with pytest.raises(ProviderProfileError, match="unsupported provider profile"):
        requested_provider_profile({"EVAL_PROVIDER_PROFILE": "openrouter-any-model"})


def test_app_settings_bind_all_glm_runtime_controls() -> None:
    settings = Settings(
        APP_ENV="development",
        MCP_SHARED_TOKEN="mcp-test-token-that-is-at-least-32-chars",
        LIVE_PROVIDER_PROFILE=GLM_FUNCTIONAL_PROFILE_NAME,
        LIVE_TASK_TIMEOUT_SECONDS=180,
        LIVE_RUN_TERMINAL_DEADLINE_SECONDS=195,
        LIVE_USAGE_EXPECTED_PROVIDER="openrouter",
        LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY=False,
    )
    assert settings.LIVE_PROVIDER_PROFILE == GLM_FUNCTIONAL_PROFILE_NAME

    with pytest.raises(ValidationError, match="do not match"):
        Settings(
            APP_ENV="development",
            MCP_SHARED_TOKEN="mcp-test-token-that-is-at-least-32-chars",
            LIVE_PROVIDER_PROFILE=GLM_FUNCTIONAL_PROFILE_NAME,
            LIVE_TASK_TIMEOUT_SECONDS=25,
            LIVE_RUN_TERMINAL_DEADLINE_SECONDS=29,
            LIVE_USAGE_EXPECTED_PROVIDER="openai",
            LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY=True,
        )


def test_controlled_provider_retry_is_typed_off_by_default_and_faults_are_test_only() -> None:
    settings = Settings(MCP_SHARED_TOKEN="mcp-test-token-that-is-at-least-32-chars")

    assert settings.CONTROLLED_PROVIDER_RETRY_ENABLED is False
    assert settings.CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE == "none"

    with pytest.raises(ValidationError, match="restricted to enabled test runs"):
        Settings(
            APP_ENV="development",
            MCP_SHARED_TOKEN="mcp-test-token-that-is-at-least-32-chars",
            CONTROLLED_PROVIDER_RETRY_ENABLED=True,
            CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE="transient_then_success",
        )

    test_settings = Settings(
        APP_ENV="test",
        MCP_SHARED_TOKEN="mcp-test-token-that-is-at-least-32-chars",
        CONTROLLED_PROVIDER_RETRY_ENABLED=True,
        CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE="transient_twice",
    )
    assert test_settings.CONTROLLED_PROVIDER_RETRY_ENABLED is True
