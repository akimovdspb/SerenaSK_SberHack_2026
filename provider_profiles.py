from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping


class ProviderProfileError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class ProviderProfile:
    name: str
    runtime_route: str
    ledger_provider: str
    normalized_model: str
    reasoning_effort: str
    fallbacks: tuple[str, ...]
    require_post_task_summary: bool
    task_timeout_seconds: int
    terminal_deadline_seconds: int
    main_loop_max_tokens: int
    safety_call_timeout_seconds: int
    tool_call_timeout_seconds: int
    allowed_tools: tuple[str, ...]
    required_branch: str
    secret_file_env: str
    secret_host_path_env: str
    default_secret_host_path: str
    secret_container_path: str
    canonical_latency_target_seconds: int = 30
    functional_latency_gap_allowed: bool = False
    pilot_case_ids: tuple[str, ...] = ("B04", "B07", "B08")
    qualification_task_timeout_seconds: int | None = None
    qualification_terminal_deadline_seconds: int | None = None

    @property
    def runtime_provider(self) -> str:
        return self.runtime_route.split("::", 1)[0]

    @property
    def effective_task_timeout_seconds(self) -> int:
        return self.qualification_task_timeout_seconds or self.task_timeout_seconds

    @property
    def effective_terminal_deadline_seconds(self) -> int:
        return self.qualification_terminal_deadline_seconds or self.terminal_deadline_seconds


EXACT_P0_TOOLS = (
    "mcp_factory__cf_context_get",
    "mcp_factory__cf_draft_save",
)

CANONICAL_PROFILE_NAME = "openai-gpt-5.4-mini"
GLM_FUNCTIONAL_PROFILE_NAME = "openrouter-glm-5.2-functional"
CAMPAIGN_AUTHORING_PROFILE_NAME = "openrouter-glm-5.2-campaign-authoring"

_PROFILES = {
    CANONICAL_PROFILE_NAME: ProviderProfile(
        name=CANONICAL_PROFILE_NAME,
        runtime_route="openai::gpt-5.4-mini",
        ledger_provider="openai",
        normalized_model="gpt-5.4-mini",
        reasoning_effort="low",
        fallbacks=(),
        require_post_task_summary=True,
        task_timeout_seconds=25,
        terminal_deadline_seconds=29,
        main_loop_max_tokens=65_536,
        safety_call_timeout_seconds=5,
        tool_call_timeout_seconds=5,
        allowed_tools=EXACT_P0_TOOLS,
        required_branch="codex/p0-autonomous",
        secret_file_env="OPENAI_API_KEY_FILE",
        secret_host_path_env="OPENAI_API_KEY_HOST_PATH",
        default_secret_host_path=("/home/dmitry/secrets/communication-factory/OPENAI_API_KEY.txt"),
        secret_container_path="/run/secrets/openai_api_key",
    ),
    GLM_FUNCTIONAL_PROFILE_NAME: ProviderProfile(
        name=GLM_FUNCTIONAL_PROFILE_NAME,
        runtime_route="openrouter::z-ai/glm-5.2",
        ledger_provider="openrouter",
        normalized_model="z-ai/glm-5.2",
        reasoning_effort="low",
        fallbacks=(),
        require_post_task_summary=False,
        task_timeout_seconds=180,
        terminal_deadline_seconds=195,
        main_loop_max_tokens=10_240,
        safety_call_timeout_seconds=20,
        tool_call_timeout_seconds=30,
        allowed_tools=EXACT_P0_TOOLS,
        required_branch="codex/p0-glm-basket",
        secret_file_env="OPENROUTER_API_KEY_FILE",
        secret_host_path_env="OPENROUTER_API_KEY_HOST_PATH",
        default_secret_host_path=(
            "/home/dmitry/secrets/communication-factory/OPENROUTER_API_KEY.txt"
        ),
        secret_container_path="/run/secrets/openrouter_api_key",
        functional_latency_gap_allowed=True,
        pilot_case_ids=("B04", "B07", "B08", "B14", "B15"),
        qualification_task_timeout_seconds=330,
        qualification_terminal_deadline_seconds=360,
    ),
    CAMPAIGN_AUTHORING_PROFILE_NAME: ProviderProfile(
        name=CAMPAIGN_AUTHORING_PROFILE_NAME,
        runtime_route="openrouter::z-ai/glm-5.2",
        ledger_provider="openrouter",
        normalized_model="z-ai/glm-5.2",
        reasoning_effort="low",
        fallbacks=(),
        require_post_task_summary=False,
        task_timeout_seconds=600,
        terminal_deadline_seconds=900,
        main_loop_max_tokens=16_384,
        safety_call_timeout_seconds=20,
        tool_call_timeout_seconds=30,
        allowed_tools=EXACT_P0_TOOLS,
        required_branch="codex/campaign-authoring-quality-v3-20260717",
        secret_file_env="OPENROUTER_API_KEY_FILE",
        secret_host_path_env="OPENROUTER_API_KEY_HOST_PATH",
        default_secret_host_path=(
            "/home/dmitry/secrets/communication-factory/OPENROUTER_API_KEY.txt"
        ),
        secret_container_path="/run/secrets/openrouter_api_key",
        functional_latency_gap_allowed=True,
        pilot_case_ids=("B01",),
    ),
}

_PROVIDER_METADATA_MODEL_ALIASES = {
    "z-ai/glm-5.2": frozenset({"z-ai/glm-5.2", "z-ai/glm-5.2-20260616"}),
}


def provider_profile(name: str) -> ProviderProfile:
    normalized = str(name or "").strip()
    try:
        return _PROFILES[normalized]
    except KeyError as exc:
        label = normalized or "<empty>"
        raise ProviderProfileError(f"unsupported provider profile: {label}") from exc


def requested_provider_profile(
    environment: Mapping[str, str] | None = None,
    *,
    variable: str = "EVAL_PROVIDER_PROFILE",
    default: str | None = None,
) -> ProviderProfile:
    source = environment if environment is not None else os.environ
    raw_name = str(source.get(variable) or default or "").strip()
    if not raw_name:
        raise ProviderProfileError(f"{variable} must name an explicit provider profile")
    return provider_profile(raw_name)


def available_provider_profile_names() -> tuple[str, ...]:
    return tuple(_PROFILES)


def normalize_provider_model(model: str) -> str:
    value = str(model or "").strip()
    if "::" in value:
        value = value.split("::", 1)[1]
    for prefix in ("openai/", "openrouter/"):
        if value.startswith(prefix):
            return value.removeprefix(prefix)
    return value


def provider_metadata_model_allowed(*, expected_model: str, reported_model: str) -> bool:
    expected = normalize_provider_model(expected_model)
    reported = normalize_provider_model(reported_model)
    return reported in _PROVIDER_METADATA_MODEL_ALIASES.get(expected, frozenset({expected}))
