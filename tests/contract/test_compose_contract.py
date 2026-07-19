from __future__ import annotations

import copy

import pytest

from provider_profiles import GLM_FUNCTIONAL_PROFILE_NAME, provider_profile
from scripts.compose_contract import (
    load_rendered_compose,
    validate_compose,
    validate_static_files,
)


@pytest.mark.contract
def test_rendered_compose_enforces_network_and_secret_boundaries() -> None:
    assert validate_compose(load_rendered_compose()) == []


@pytest.mark.contract
def test_static_container_inputs_exclude_secrets_and_private_routes() -> None:
    assert validate_static_files() == []


@pytest.mark.contract
def test_runtime_policies_fail_closed_on_unbounded_logs_or_resources() -> None:
    config = load_rendered_compose()
    changed = copy.deepcopy(config)
    changed["services"]["app"]["logging"]["options"]["max-size"] = "unlimited"
    changed["services"]["ouroboros"]["mem_limit"] = 0

    errors = validate_compose(changed)

    assert "app log rotation is not canonical" in errors
    assert "ouroboros resource limits are not canonical" in errors


@pytest.mark.contract
def test_ouroboros_ledger_group_matches_the_compose_supplemental_group() -> None:
    config = load_rendered_compose()
    ouroboros = config["services"]["ouroboros"]

    assert ouroboros["environment"]["CF_REQUEST_LEDGER_GID"] == str(ouroboros["group_add"][0])


@pytest.mark.contract
def test_qualification_review_output_ceiling_reaches_ouroboros(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_TOKENS", "16384")

    config = load_rendered_compose()

    assert config["services"]["ouroboros"]["environment"]["OUROBOROS_REVIEW_MAX_TOKENS"] == "16384"


@pytest.mark.contract
def test_glm_compose_profile_mounts_only_the_openrouter_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = provider_profile(GLM_FUNCTIONAL_PROFILE_NAME)
    values = {
        "EVAL_PROVIDER_PROFILE": profile.name,
        "CF_RUNTIME_PROVIDER": profile.runtime_provider,
        "OPENROUTER_ENABLED": "true",
        "OUROBOROS_MODEL": profile.runtime_route,
        "PROVIDER_API_KEY_HOST_PATH": profile.default_secret_host_path,
        "PROVIDER_API_KEY_CONTAINER_PATH": profile.secret_container_path,
        "LIVE_TASK_TIMEOUT_SECONDS": str(profile.task_timeout_seconds),
        "LIVE_RUN_TERMINAL_DEADLINE_SECONDS": str(profile.terminal_deadline_seconds),
        "LIVE_USAGE_EXPECTED_PROVIDER": profile.ledger_provider,
        "LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY": "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    rendered = load_rendered_compose()

    assert validate_compose(rendered, selected_profile=profile) == []
    mounts = rendered["services"]["ouroboros"]["volumes"]
    provider_mounts = [
        item for item in mounts if item.get("target") == profile.secret_container_path
    ]
    assert len(provider_mounts) == 1
    assert provider_mounts[0]["source"] == profile.default_secret_host_path
