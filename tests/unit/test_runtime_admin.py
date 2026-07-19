from __future__ import annotations

import importlib.util
import pathlib

import pytest

RUNTIME_ADMIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "ouroboros" / "runtime" / "runtime_admin.py"
)


def _load_runtime_admin():
    spec = importlib.util.spec_from_file_location("cf_runtime_admin", RUNTIME_ADMIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load runtime admin")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_snapshot_validation_requires_exact_private_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _load_runtime_admin()
    monkeypatch.delenv("CF_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("OPENROUTER_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_MODEL", raising=False)
    snapshot = {
        "runtime": {
            "supervisor_ready": True,
            "supervisor_error": False,
            "workers_alive": 1,
            "workers_total": 1,
            "runtime_mode": "light",
            "context_mode": "low",
            "safety_mode": "full",
            "evolution_enabled": False,
            "background_enabled": False,
        },
        "mcp": {
            "enabled": True,
            "sdk_available": True,
            "tool_timeout_sec": 5,
            "server_count": 1,
            "factory_auth_configured": True,
            "factory_has_error": False,
            "raw_tools": admin.EXPECTED_RAW_TOOLS,
            "prefixed_tools": admin.EXPECTED_PREFIXED_TOOLS,
        },
        "skill": {
            "name": "communication_factory",
            "version": "1.0.0",
            "type": "instruction",
            "permissions": [],
            "load_error": False,
            "enabled": True,
            "executable_review": True,
            "review_stale": False,
            "review_status": "clean",
            "all_granted": True,
        },
        "settings": {
            "provider_secrets_empty": True,
            "mcp_enabled": True,
            "mcp_tool_timeout_sec": 5,
            "mcp_server_count": 1,
            "model": "openai::gpt-5.4-mini",
            "fallbacks_empty": True,
            "safety_call_timeout_sec": 5,
            "tool_call_timeout_sec": 5,
        },
    }

    assert admin.validate_snapshot(snapshot, require_skill=True) == []

    snapshot["mcp"]["prefixed_tools"] = ["mcp_factory__cf_context_get"]
    assert "MCP prefixed tool inventory drifted" in admin.validate_snapshot(
        snapshot, require_skill=True
    )


def test_runtime_snapshot_rejects_executable_blocker_review() -> None:
    admin = _load_runtime_admin()
    snapshot = {
        "runtime": {
            "supervisor_ready": True,
            "supervisor_error": False,
            "workers_alive": 1,
            "workers_total": 1,
            "runtime_mode": "light",
            "context_mode": "low",
            "safety_mode": "full",
            "evolution_enabled": False,
            "background_enabled": False,
        },
        "mcp": {
            "enabled": True,
            "sdk_available": True,
            "tool_timeout_sec": 5,
            "server_count": 1,
            "factory_auth_configured": True,
            "factory_has_error": False,
            "raw_tools": admin.EXPECTED_RAW_TOOLS,
            "prefixed_tools": admin.EXPECTED_PREFIXED_TOOLS,
        },
        "skill": {
            "name": "communication_factory",
            "version": "1.0.0",
            "type": "instruction",
            "permissions": [],
            "load_error": False,
            "enabled": True,
            "executable_review": True,
            "review_stale": False,
            "review_status": "blockers",
            "all_granted": True,
        },
        "settings": {
            "provider_secrets_empty": True,
            "mcp_enabled": True,
            "mcp_tool_timeout_sec": 5,
            "mcp_server_count": 1,
            "model": "openai::gpt-5.4-mini",
            "fallbacks_empty": True,
            "safety_call_timeout_sec": 5,
            "tool_call_timeout_sec": 5,
        },
    }

    assert "skill lifecycle is not clean, executable, and enabled" in admin.validate_snapshot(
        snapshot, require_skill=True
    )


def test_review_rebuttal_is_code_grounded_not_an_owner_attestation() -> None:
    admin = _load_runtime_admin()

    assert "type=instruction" in admin.SKILL_REVIEW_REBUTTAL
    assert "locked ToolRegistry" in admin.SKILL_REVIEW_REBUTTAL
    assert "permissions=[]" in admin.SKILL_REVIEW_REBUTTAL
    assert "owner attestation" not in admin.SKILL_REVIEW_REBUTTAL.lower()


def test_runtime_snapshot_uses_the_explicit_openrouter_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _load_runtime_admin()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")

    assert admin._expected_runtime_model() == "openrouter::z-ai/glm-5.2"
    assert admin._expected_runtime_profile().name == "openrouter-glm-5.2-functional"

    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::unapproved/model")
    with pytest.raises(admin.RuntimeAdminError, match="approved provider profile"):
        admin._expected_runtime_profile()


def test_runtime_snapshot_rejects_openrouter_tool_timeout_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _load_runtime_admin()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")
    snapshot = {
        "runtime": {
            "supervisor_ready": True,
            "supervisor_error": False,
            "workers_alive": 1,
            "workers_total": 1,
            "runtime_mode": "light",
            "context_mode": "low",
            "safety_mode": "full",
            "evolution_enabled": False,
            "background_enabled": False,
        },
        "mcp": {
            "enabled": True,
            "sdk_available": True,
            "tool_timeout_sec": 5,
            "server_count": 1,
            "factory_auth_configured": True,
            "factory_has_error": False,
            "raw_tools": admin.EXPECTED_RAW_TOOLS,
            "prefixed_tools": admin.EXPECTED_PREFIXED_TOOLS,
        },
        "skill": {
            "name": "communication_factory",
            "version": "1.0.0",
            "type": "instruction",
            "permissions": [],
            "load_error": False,
            "enabled": True,
            "executable_review": True,
            "review_stale": False,
            "review_status": "clean",
            "all_granted": True,
        },
        "settings": {
            "provider_secrets_empty": True,
            "mcp_enabled": True,
            "mcp_tool_timeout_sec": 5,
            "mcp_server_count": 1,
            "model": "openrouter::z-ai/glm-5.2",
            "fallbacks_empty": True,
            "safety_call_timeout_sec": 20,
            "tool_call_timeout_sec": 5,
        },
    }

    assert "runtime settings readback drifted" in admin.validate_snapshot(
        snapshot, require_skill=True
    )
    snapshot["settings"]["tool_call_timeout_sec"] = 30
    assert admin.validate_snapshot(snapshot, require_skill=True) == []
