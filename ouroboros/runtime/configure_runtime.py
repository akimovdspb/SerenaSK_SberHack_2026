from __future__ import annotations

import os
from typing import Any

from provider_profiles import (
    CANONICAL_PROFILE_NAME,
    GLM_FUNCTIONAL_PROFILE_NAME,
    ProviderProfileError,
    provider_profile,
)

SECRET_SETTING_KEYS = (
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "GIGACHAT_CREDENTIALS",
    "GIGACHAT_PASSWORD",
    "GITHUB_TOKEN",
)


def _required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"required non-provider runtime setting {name} is missing")
    return value


def selected_runtime_model() -> str:
    provider = str(os.environ.get("CF_RUNTIME_PROVIDER") or "openai").strip().lower()
    default_profile = (
        GLM_FUNCTIONAL_PROFILE_NAME if provider == "openrouter" else CANONICAL_PROFILE_NAME
    )
    profile_name = str(os.environ.get("CF_PROVIDER_PROFILE") or default_profile).strip()
    try:
        profile = provider_profile(profile_name)
    except ProviderProfileError as exc:
        raise RuntimeError(str(exc)) from exc
    if provider != profile.runtime_provider:
        raise RuntimeError("runtime provider does not match the selected provider profile")
    if provider == "openrouter" and (
        str(os.environ.get("OPENROUTER_ENABLED") or "").strip().lower() != "true"
    ):
        raise RuntimeError("OpenRouter runtime requires OPENROUTER_ENABLED=true")
    configured = str(os.environ.get("OUROBOROS_MODEL") or profile.runtime_route).strip()
    if configured == profile.normalized_model:
        configured = f"{profile.runtime_provider}::{configured}"
    if configured != profile.runtime_route:
        if provider == "openrouter":
            raise RuntimeError(
                "runtime model does not match the approved z-ai/glm-5.2 provider profile"
            )
        raise RuntimeError("runtime model does not match the selected provider profile")
    return configured


def selected_reasoning_effort() -> str:
    model = selected_runtime_model()
    for name in (CANONICAL_PROFILE_NAME, GLM_FUNCTIONAL_PROFILE_NAME):
        profile = provider_profile(name)
        if profile.runtime_route == model:
            return profile.reasoning_effort
    raise RuntimeError("runtime reasoning profile is unavailable")


def selected_safety_call_timeout_seconds() -> int:
    model = selected_runtime_model()
    for name in (CANONICAL_PROFILE_NAME, GLM_FUNCTIONAL_PROFILE_NAME):
        profile = provider_profile(name)
        if profile.runtime_route == model:
            return profile.safety_call_timeout_seconds
    raise RuntimeError("runtime safety-timeout profile is unavailable")


def selected_tool_call_timeout_seconds() -> int:
    model = selected_runtime_model()
    for name in (CANONICAL_PROFILE_NAME, GLM_FUNCTIONAL_PROFILE_NAME):
        profile = provider_profile(name)
        if profile.runtime_route == model:
            return profile.tool_call_timeout_seconds
    raise RuntimeError("runtime tool-timeout profile is unavailable")


def selected_deployment_profile() -> str:
    profile = str(os.environ.get("CF_DEPLOYMENT_PROFILE") or "compose").strip().lower()
    if profile not in {"compose", "railway"}:
        raise RuntimeError("CF_DEPLOYMENT_PROFILE must be compose or railway")
    return profile


def selected_runtime_host() -> str:
    return "127.0.0.1" if selected_deployment_profile() == "railway" else "0.0.0.0"


def selected_factory_mcp_url() -> str:
    profile = selected_deployment_profile()
    expected = (
        "http://127.0.0.1:8000/internal/mcp"
        if profile == "railway"
        else "http://app:8000/internal/mcp"
    )
    configured = str(os.environ.get("FACTORY_MCP_URL") or expected).strip()
    if configured != expected:
        raise RuntimeError("factory MCP URL does not match the selected deployment profile")
    return configured


def _runtime_settings(*, startup: bool) -> dict[str, Any]:
    del startup
    model = selected_runtime_model()
    reasoning_effort = selected_reasoning_effort()
    return {
        "OUROBOROS_SERVER_HOST": selected_runtime_host(),
        "OUROBOROS_MODEL": model,
        "OUROBOROS_MODEL_HEAVY": model,
        "OUROBOROS_MODEL_LIGHT": model,
        "OUROBOROS_MODEL_FALLBACKS": "",
        "OUROBOROS_EFFORT_TASK": reasoning_effort,
        "OUROBOROS_EFFORT_REVIEW": reasoning_effort,
        "OUROBOROS_EFFORT_SCOPE_REVIEW": reasoning_effort,
        "OUROBOROS_REVIEW_MODELS": model,
        "OUROBOROS_SCOPE_REVIEW_MODEL": model,
        "OUROBOROS_SCOPE_REVIEW_MODELS": model,
        "OUROBOROS_RUNTIME_MODE": "light",
        "OUROBOROS_CONTEXT_MODE": "low",
        "OUROBOROS_SAFETY_MODE": "full",
        "OUROBOROS_TASK_REVIEW_MODE": "off",
        "OUROBOROS_ACCEPTANCE_MAX_IMPROVEMENT_PASSES": 0,
        "OUROBOROS_POST_TASK_EVOLUTION": "false",
        "OUROBOROS_POST_TASK_EVOLUTION_CADENCE": "off",
        "OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS": "false",
        "OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS": "true",
        "OUROBOROS_TRUST_NATIVE_SEEDED_SKILLS": "false",
        "OUROBOROS_MAX_ACTIVE_SUBAGENTS_PER_ROOT": 1,
        "OUROBOROS_MAX_SUBAGENT_DEPTH": 0,
        "OUROBOROS_SKILLS_REPO_PATH": "/skills",
        "OUROBOROS_TOOL_TIMEOUT_SEC": selected_tool_call_timeout_seconds(),
        "OUROBOROS_SAFETY_CALL_TIMEOUT_SEC": selected_safety_call_timeout_seconds(),
        "OUROBOROS_FINALIZATION_GRACE_SEC": 2,
        "OUROBOROS_GENERATIVE_PROBE": "0",
        "TOTAL_BUDGET": float(os.environ.get("TOTAL_BUDGET", "20")),
        "OUROBOROS_PER_TASK_COST_USD": float(os.environ.get("OUROBOROS_PER_TASK_COST_USD", "2")),
        "MCP_ENABLED": True,
        "MCP_TOOL_TIMEOUT_SEC": 5,
        "MCP_SERVERS": [
            {
                "id": "factory",
                "name": "Communication Factory",
                "enabled": True,
                "transport": "streamable_http",
                "url": selected_factory_mcp_url(),
                "auth_header": "Authorization",
                "auth_token": f"Bearer {_required_env('MCP_SHARED_TOKEN')}",
                "allowed_tools": ["cf_context_get", "cf_draft_save"],
            }
        ],
    }


def configure_runtime(*, startup: bool = False) -> None:
    from ouroboros.config import load_settings  # type: ignore[import-not-found]
    from ouroboros.gateway.settings import (  # type: ignore[import-not-found]
        _owner_write_settings,
    )

    settings = load_settings()
    runtime_settings = _runtime_settings(startup=startup)
    settings.update(runtime_settings)
    for key in ("OUROBOROS_SAFETY_CALL_TIMEOUT_SEC", "OUROBOROS_TOOL_TIMEOUT_SEC"):
        os.environ[key] = str(runtime_settings[key])
    for key in SECRET_SETTING_KEYS:
        settings[key] = ""
    _owner_write_settings(settings, allow_context_lowering=True)


if __name__ == "__main__":
    configure_runtime(startup=False)
    print("runtime-settings: PASS (provider credentials not persisted)")
