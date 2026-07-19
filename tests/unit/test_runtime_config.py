from __future__ import annotations

import importlib.util
import pathlib

import pytest

CONFIG_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "ouroboros" / "runtime" / "configure_runtime.py"
)


def _load_config():
    spec = importlib.util.spec_from_file_location("cf_configure_runtime", CONFIG_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load runtime configuration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_profile_remains_on_the_canonical_openai_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_config()
    monkeypatch.delenv("CF_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("CF_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.delenv("FACTORY_MCP_URL", raising=False)
    monkeypatch.setenv("MCP_SHARED_TOKEN", "t" * 32)

    settings = config._runtime_settings(startup=True)

    assert settings["OUROBOROS_MODEL"] == "openai::gpt-5.4-mini"
    assert settings["OUROBOROS_MODEL_FALLBACKS"] == ""
    assert settings["OUROBOROS_SAFETY_CALL_TIMEOUT_SEC"] == 5
    assert settings["OUROBOROS_TOOL_TIMEOUT_SEC"] == 5
    assert settings["OUROBOROS_SERVER_HOST"] == "0.0.0.0"
    assert settings["MCP_SERVERS"][0]["url"] == "http://app:8000/internal/mcp"


def test_railway_profile_selects_only_the_approved_openrouter_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_config()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("CF_DEPLOYMENT_PROFILE", "railway")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")
    monkeypatch.setenv("MCP_SHARED_TOKEN", "t" * 32)

    settings = config._runtime_settings(startup=True)

    assert settings["OUROBOROS_MODEL"] == "openrouter::z-ai/glm-5.2"
    assert settings["OUROBOROS_REVIEW_MODELS"] == "openrouter::z-ai/glm-5.2"
    assert settings["OUROBOROS_MODEL_FALLBACKS"] == ""
    assert settings["OUROBOROS_SAFETY_CALL_TIMEOUT_SEC"] == 20
    assert settings["OUROBOROS_TOOL_TIMEOUT_SEC"] == 30
    assert settings["OUROBOROS_SERVER_HOST"] == "127.0.0.1"
    assert settings["MCP_SERVERS"][0]["url"] == "http://127.0.0.1:8000/internal/mcp"


def test_openrouter_profile_rejects_an_unapproved_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_config()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::some/other-model")

    with pytest.raises(RuntimeError, match=r"approved z-ai/glm-5\.2"):
        config.selected_runtime_model()
