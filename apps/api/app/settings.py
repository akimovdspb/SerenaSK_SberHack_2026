from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from provider_profiles import CANONICAL_PROFILE_NAME, ProviderProfileError, provider_profile


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=True,
        extra="ignore",
    )

    APP_ENV: str = "development"
    DATABASE_URL: str = "sqlite:////data/factory.db"
    ARTIFACTS_DIR: Path = Path("/data/artifacts")
    EVIDENCE_DIR: Path = Path("/evidence")
    MVP_REPORT_DIR: Path = Path("/srv/app/reports/basket03-mvp-testing")
    SYNTHETIC_DATA_DIR: Path = Path("/srv/app/data/synthetic")
    CONTRACT_LOCK_PATH: Path = Path("/contract-lock/communication_factory.lock.json")
    RUNTIME_READY_PATH: Path | None = None
    SKILL_PATH: Path = Path("/skills/communication_factory/SKILL.md")
    OUROBOROS_BASE_URL: str = "http://ouroboros:8765"
    LIVE_PROVIDER_PROFILE: str = CANONICAL_PROFILE_NAME
    LIVE_TASK_TIMEOUT_SECONDS: int = Field(default=25, ge=5, le=900)
    LIVE_RUN_TERMINAL_DEADLINE_SECONDS: float = Field(default=29.0, ge=6, le=1_200)
    LIVE_USAGE_EXPECTED_PROVIDER: Literal["openai", "openrouter"] = "openai"
    LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY: bool = True
    CONTROLLED_PROVIDER_RETRY_ENABLED: bool = False
    CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE: Literal[
        "none", "transient_then_success", "transient_twice"
    ] = "none"
    RUNTIME_CONTRACT_IDENTITY_KIND: str = "docker_image"
    RUNTIME_CONTRACT_IDENTITY: str = ""
    MCP_SHARED_TOKEN: SecretStr = Field(min_length=32)
    MCP_MAX_PAYLOAD_BYTES: int = Field(default=65_536, ge=1_024, le=1_048_576)
    DEFAULT_EXECUTION_MODE: Literal["deterministic_template", "live_ouroboros"] = (
        "deterministic_template"
    )
    HUMAN_ACTIONS_TEST_ONLY: bool = True
    DEMO_RESET_ENABLED: bool = False
    SESSION_AUTH_ENABLED: bool = False

    @model_validator(mode="after")
    def validate_live_provider_profile(self) -> Settings:
        if self.CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE != "none" and (
            self.APP_ENV != "test" or not self.CONTROLLED_PROVIDER_RETRY_ENABLED
        ):
            raise ValueError("controlled retry fault profiles are restricted to enabled test runs")
        if self.APP_ENV == "test":
            return self
        try:
            profile = provider_profile(self.LIVE_PROVIDER_PROFILE)
        except ProviderProfileError as exc:
            raise ValueError(str(exc)) from exc
        expected = {
            "LIVE_TASK_TIMEOUT_SECONDS": profile.task_timeout_seconds,
            "LIVE_RUN_TERMINAL_DEADLINE_SECONDS": float(profile.terminal_deadline_seconds),
            "LIVE_USAGE_EXPECTED_PROVIDER": profile.ledger_provider,
            "LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY": profile.require_post_task_summary,
        }
        actual = {
            "LIVE_TASK_TIMEOUT_SECONDS": self.LIVE_TASK_TIMEOUT_SECONDS,
            "LIVE_RUN_TERMINAL_DEADLINE_SECONDS": self.LIVE_RUN_TERMINAL_DEADLINE_SECONDS,
            "LIVE_USAGE_EXPECTED_PROVIDER": self.LIVE_USAGE_EXPECTED_PROVIDER,
            "LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY": self.LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY,
        }
        if actual != expected:
            raise ValueError("live runtime settings do not match the selected provider profile")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
