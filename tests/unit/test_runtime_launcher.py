from __future__ import annotations

import importlib.util
import json
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

import request_ledger
from provider_profiles import CAMPAIGN_AUTHORING_PROFILE_NAME

RUNTIME_LAUNCHER_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "ouroboros" / "runtime" / "runtime_launcher.py"
)


def _load_runtime_launcher():
    spec = importlib.util.spec_from_file_location("cf_runtime_launcher", RUNTIME_LAUNCHER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Ouroboros runtime launcher")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provider_secrets_are_removed_only_from_the_persisted_copy() -> None:
    launcher = _load_runtime_launcher()
    settings = {
        "OPENAI_API_KEY": "owner-secret",
        "OPENROUTER_API_KEY": "alternate-secret",
        "MCP_SERVERS": [{"auth_token": "Bearer private-mcp-token"}],
        "OUROBOROS_MODEL": "openai::gpt-5.4-mini",
    }

    persisted = launcher.settings_for_persistence(settings)

    assert persisted["OPENAI_API_KEY"] == ""
    assert persisted["OPENROUTER_API_KEY"] == ""
    assert persisted["MCP_SERVERS"] == settings["MCP_SERVERS"]
    assert persisted["OUROBOROS_MODEL"] == "openai::gpt-5.4-mini"
    assert settings["OPENAI_API_KEY"] == "owner-secret"


def test_p0_runtime_model_profile_removes_automatic_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    monkeypatch.delenv("CF_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("OPENROUTER_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_MODEL", raising=False)

    normalized, changed = launcher.settings_for_p0_runtime(
        {
            "OUROBOROS_MODEL": "openai::gpt-5.5",
            "OUROBOROS_MODEL_FALLBACKS": "openai::gpt-5.4-mini",
        }
    )

    assert normalized["OUROBOROS_MODEL"] == "openai::gpt-5.4-mini"
    assert normalized["OUROBOROS_MODEL_FALLBACKS"] == ""
    assert normalized["OUROBOROS_REVIEW_MODELS"] == "openai::gpt-5.4-mini"
    assert "OUROBOROS_MODEL_FALLBACKS" in changed


def test_explicit_railway_profile_pins_glm_without_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")

    normalized, _ = launcher.settings_for_p0_runtime({})

    assert normalized["OUROBOROS_MODEL"] == "openrouter::z-ai/glm-5.2"
    assert normalized["OUROBOROS_MODEL_LIGHT"] == "openrouter::z-ai/glm-5.2"
    assert normalized["OUROBOROS_REVIEW_MODELS"] == "openrouter::z-ai/glm-5.2"
    assert normalized["OUROBOROS_MODEL_FALLBACKS"] == ""


def test_profile_model_guard_normalizes_consolidation_and_blocks_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")

    class FakeLLMClient:
        def chat(
            self,
            *,
            messages: list[dict[str, str]],
            model: str,
            max_tokens: int = 65_536,
        ) -> tuple[dict[str, str], dict[str, int]]:
            del messages, max_tokens
            return {"content": model}, {"prompt_tokens": 1}

    fake_llm = SimpleNamespace(
        LLMClient=FakeLLMClient,
        DEFAULT_LIGHT_MODEL="google/gemini-3.5-flash",
    )
    fake_consolidator = SimpleNamespace(
        CONSOLIDATION_MODEL="google/gemini-3.5-flash",
    )
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.llm":
            return fake_llm
        if name == "ouroboros.consolidator":
            return fake_consolidator
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)
    launcher.install_profile_model_guard()

    assert fake_llm.DEFAULT_LIGHT_MODEL == "openrouter::z-ai/glm-5.2"
    assert fake_consolidator.CONSOLIDATION_MODEL == "openrouter::z-ai/glm-5.2"
    assert FakeLLMClient().chat(messages=[], model="z-ai/glm-5.2")[0]["content"] == ("z-ai/glm-5.2")
    with pytest.raises(RuntimeError, match="selected P0 profile"):
        FakeLLMClient().chat(messages=[], model="google/gemini-3.5-flash")
    with pytest.raises(RuntimeError, match="selected P0 profile"):
        FakeLLMClient().chat(messages=[], model="openai::z-ai/glm-5.2")


def test_glm_profile_caps_main_loop_output_without_changing_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    fake_loop = SimpleNamespace(MAIN_LOOP_MAX_TOKENS=65_536)
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.loop_llm_call":
            return fake_loop
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)

    monkeypatch.delenv("CF_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("OPENROUTER_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_MODEL", raising=False)
    launcher.install_glm_main_loop_output_cap()
    assert fake_loop.MAIN_LOOP_MAX_TOKENS == 65_536

    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")
    launcher.install_glm_main_loop_output_cap()
    assert fake_loop.MAIN_LOOP_MAX_TOKENS == 10_240

    launcher.install_glm_main_loop_output_cap()
    assert fake_loop.MAIN_LOOP_MAX_TOKENS == 10_240


def test_campaign_authoring_profile_uses_its_reviewed_output_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")
    monkeypatch.setenv("CF_PROVIDER_PROFILE", CAMPAIGN_AUTHORING_PROFILE_NAME)
    fake_loop = SimpleNamespace(MAIN_LOOP_MAX_TOKENS=65_536)
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.loop_llm_call":
            return fake_loop
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)

    launcher.install_glm_main_loop_output_cap()

    assert fake_loop.MAIN_LOOP_MAX_TOKENS == 16_384

    launcher.install_glm_main_loop_output_cap()
    assert fake_loop.MAIN_LOOP_MAX_TOKENS == 16_384


def test_glm_main_loop_output_cap_fails_closed_on_upstream_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")
    fake_loop = SimpleNamespace(MAIN_LOOP_MAX_TOKENS=32_768)
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.loop_llm_call":
            return fake_loop
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)

    with pytest.raises(RuntimeError, match="output-token contract drifted"):
        launcher.install_glm_main_loop_output_cap()


def test_glm_safety_calls_use_strict_verdict_schema_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")
    captured: list[dict[str, Any]] = []

    def chat_observed(
        llm: object,
        *,
        drive_root: pathlib.Path,
        task_id: str = "",
        call_type: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del llm, drive_root, task_id
        captured.append(
            {
                "call_type": call_type,
                "response_format": kwargs["response_format"],
            }
        )
        return kwargs["response_format"]

    fake_observability = SimpleNamespace(chat_observed=chat_observed)
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.llm_observability":
            return fake_observability
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)
    launcher.install_glm_safety_response_schema()
    installed = fake_observability.chat_observed

    result = installed(
        object(),
        drive_root=pathlib.Path("/tmp/test-drive"),
        call_type="safety_supervisor",
        messages=[],
        response_format={"type": "json_object"},
    )
    assert result == launcher.SAFETY_RESPONSE_SCHEMA
    assert captured[-1]["response_format"]["json_schema"]["schema"] == {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["SAFE", "SUSPICIOUS", "DANGEROUS"],
            },
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["status", "reason"],
        "additionalProperties": False,
    }

    ordinary_format = {"type": "json_object", "marker": "unchanged"}
    assert (
        installed(
            object(),
            drive_root=pathlib.Path("/tmp/test-drive"),
            call_type="main_generation",
            messages=[],
            response_format=ordinary_format,
        )
        == ordinary_format
    )
    assert captured[-1]["response_format"] is ordinary_format

    with pytest.raises(RuntimeError, match="response-format contract drifted"):
        installed(
            object(),
            drive_root=pathlib.Path("/tmp/test-drive"),
            call_type="safety_supervisor_repair",
            messages=[],
            response_format={"type": "text"},
        )

    launcher.install_glm_safety_response_schema()
    assert fake_observability.chat_observed is installed


def test_canonical_profile_does_not_patch_safety_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    monkeypatch.delenv("CF_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("OPENROUTER_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_MODEL", raising=False)

    def chat_observed() -> None:
        return None

    fake_observability = SimpleNamespace(chat_observed=chat_observed)
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.llm_observability":
            return fake_observability
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)
    launcher.install_glm_safety_response_schema()

    assert fake_observability.chat_observed is chat_observed


def test_factory_saved_receipt_is_the_only_terminal_answer_source() -> None:
    launcher = _load_runtime_launcher()
    saved = {
        "status": "SAVED",
        "persisted": True,
        "campaign_id": "campaign_test",
        "operation": "initial",
        "iteration": 1,
        "draft_id": "draft_test",
        "blockers": [],
        "warnings": ["synthetic_warning"],
    }
    answer = launcher.factory_saved_draft_final_answer(
        [
            {
                "tool": "mcp_factory__cf_draft_save",
                "is_error": False,
                "result": "External MCP result.\n\n" + json.dumps(saved),
            }
        ]
    )

    assert answer is not None
    assert json.loads(answer.removeprefix("FINAL ANSWER: ")) == {
        "blockers": [],
        "campaign_id": "campaign_test",
        "draft_id": "draft_test",
        "iteration": 1,
        "operation": "initial",
        "status": "SAVED",
        "warnings": ["synthetic_warning"],
    }
    assert (
        launcher.factory_saved_draft_final_answer(
            [
                {
                    "tool": "mcp_factory__cf_draft_save",
                    "is_error": True,
                    "result": json.dumps(saved),
                },
                {
                    "tool": "mcp_factory__cf_draft_save",
                    "is_error": False,
                    "result": json.dumps({**saved, "persisted": False}),
                },
            ]
        )
        is None
    )


def test_glm_factory_terminal_boundary_skips_only_the_post_save_provider_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")
    provider_calls: list[str] = []
    progress: list[str] = []

    def handle_tool_calls(
        tool_calls: list[dict[str, Any]],
        tools: object,
        drive_logs: pathlib.Path,
        task_id: str,
        stateful_executor: object,
        messages: list[dict[str, Any]],
        llm_trace: dict[str, Any],
        emit_progress: Any,
    ) -> int:
        del tools, drive_logs, task_id, stateful_executor, messages, emit_progress
        llm_trace.setdefault("tool_calls", []).extend(tool_calls)
        return 0

    def call_llm_with_retry(*, task_id: str) -> tuple[dict[str, str], float]:
        provider_calls.append(task_id)
        return {"role": "assistant", "content": "provider response"}, 1.0

    fake_loop = SimpleNamespace(
        handle_tool_calls=handle_tool_calls,
        call_llm_with_retry=call_llm_with_retry,
    )
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.loop":
            return fake_loop
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)
    launcher.install_glm_factory_terminal_boundary()
    installed_handle = fake_loop.handle_tool_calls
    installed_call = fake_loop.call_llm_with_retry

    saved = {
        "status": "SAVED",
        "persisted": True,
        "campaign_id": "campaign_test",
        "operation": "initial",
        "iteration": 1,
        "draft_id": "draft_test",
        "blockers": [],
        "warnings": [],
    }
    trace: dict[str, Any] = {}
    assert (
        fake_loop.handle_tool_calls(
            [
                {
                    "tool": "mcp_factory__cf_draft_save",
                    "is_error": False,
                    "result": "External MCP result.\n\n" + json.dumps(saved),
                }
            ],
            object(),
            pathlib.Path("/tmp/test-drive"),
            "task-test",
            object(),
            [],
            trace,
            progress.append,
        )
        == 0
    )

    finalized, cost = fake_loop.call_llm_with_retry(task_id="task-test")
    assert cost == 0.0
    assert finalized["content"].startswith("FINAL ANSWER: ")
    assert provider_calls == []
    assert trace["factory_terminal_boundary"] == {
        "mode": "persisted_save_receipt",
        "tool": "mcp_factory__cf_draft_save",
    }
    assert progress == ["Factory draft persisted; finalizing the task."]

    provider_result, provider_cost = fake_loop.call_llm_with_retry(task_id="task-test")
    assert provider_result["content"] == "provider response"
    assert provider_cost == 1.0
    assert provider_calls == ["task-test"]

    launcher.install_glm_factory_terminal_boundary()
    assert fake_loop.handle_tool_calls is installed_handle
    assert fake_loop.call_llm_with_retry is installed_call


def test_glm_factory_terminal_boundary_stops_a_multi_save_batch_after_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")
    executed: list[str] = []
    progress: list[str] = []
    saved = {
        "status": "SAVED",
        "persisted": True,
        "campaign_id": "campaign_batch",
        "operation": "initial",
        "iteration": 1,
        "draft_id": "draft_batch",
        "blockers": [],
        "warnings": [],
    }

    def handle_tool_calls(
        tool_calls: list[dict[str, Any]],
        tools: object,
        drive_logs: pathlib.Path,
        task_id: str,
        stateful_executor: object,
        messages: list[dict[str, Any]],
        llm_trace: dict[str, Any],
        emit_progress: Any,
    ) -> int:
        del tools, drive_logs, task_id, stateful_executor, messages, emit_progress
        for tool_call in tool_calls:
            call_id = str(tool_call["id"])
            executed.append(call_id)
            tool_name = str(tool_call["function"]["name"])
            llm_trace.setdefault("tool_calls", []).append(
                {
                    "tool": tool_name,
                    "is_error": False,
                    "result": json.dumps(saved if call_id == "save-1" else {"status": "READY"}),
                }
            )
        return 0

    def call_llm_with_retry(*, task_id: str) -> tuple[dict[str, str], float]:
        return {"role": "assistant", "content": task_id}, 1.0

    fake_loop = SimpleNamespace(
        handle_tool_calls=handle_tool_calls,
        call_llm_with_retry=call_llm_with_retry,
    )
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.loop":
            return fake_loop
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)
    launcher.install_glm_factory_terminal_boundary()
    trace: dict[str, Any] = {}
    calls = [
        {
            "id": "context-1",
            "function": {"name": "mcp_factory__cf_context_get", "arguments": "{}"},
        },
        {
            "id": "save-1",
            "function": {"name": "mcp_factory__cf_draft_save", "arguments": "{}"},
        },
        {
            "id": "save-2",
            "function": {"name": "mcp_factory__cf_draft_save", "arguments": "{}"},
        },
    ]

    assert (
        fake_loop.handle_tool_calls(
            calls,
            object(),
            pathlib.Path("/tmp/test-drive"),
            "task-batch",
            object(),
            [],
            trace,
            progress.append,
        )
        == 0
    )
    assert executed == ["context-1", "save-1"]
    assert trace["factory_terminal_boundary"] == {
        "mode": "persisted_save_receipt",
        "tool": "mcp_factory__cf_draft_save",
        "suppressed_tool_calls": 1,
    }
    finalized, cost = fake_loop.call_llm_with_retry(task_id="task-batch")
    assert cost == 0.0
    assert json.loads(finalized["content"].removeprefix("FINAL ANSWER: "))["draft_id"] == (
        "draft_batch"
    )
    assert progress == ["Factory draft persisted; finalizing the task."]


def test_glm_factory_terminal_boundary_executes_batch_without_persisted_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")
    executed: list[str] = []
    provider_calls: list[str] = []

    def handle_tool_calls(
        tool_calls: list[dict[str, Any]],
        tools: object,
        drive_logs: pathlib.Path,
        task_id: str,
        stateful_executor: object,
        messages: list[dict[str, Any]],
        llm_trace: dict[str, Any],
        emit_progress: Any,
    ) -> int:
        del tools, drive_logs, task_id, stateful_executor, messages, emit_progress
        for tool_call in tool_calls:
            call_id = str(tool_call["id"])
            executed.append(call_id)
            llm_trace.setdefault("tool_calls", []).append(
                {
                    "tool": str(tool_call["function"]["name"]),
                    "is_error": False,
                    "result": json.dumps({"status": "READY", "persisted": False}),
                }
            )
        return 0

    def call_llm_with_retry(*, task_id: str) -> tuple[dict[str, str], float]:
        provider_calls.append(task_id)
        return {"role": "assistant", "content": "provider response"}, 1.0

    fake_loop = SimpleNamespace(
        handle_tool_calls=handle_tool_calls,
        call_llm_with_retry=call_llm_with_retry,
    )
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.loop":
            return fake_loop
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)
    launcher.install_glm_factory_terminal_boundary()
    trace: dict[str, Any] = {}
    calls = [
        {
            "id": "save-rejected-1",
            "function": {"name": "mcp_factory__cf_draft_save", "arguments": "{}"},
        },
        {
            "id": "save-rejected-2",
            "function": {"name": "mcp_factory__cf_draft_save", "arguments": "{}"},
        },
    ]

    assert (
        fake_loop.handle_tool_calls(
            calls,
            object(),
            pathlib.Path("/tmp/test-drive"),
            "task-rejected-batch",
            object(),
            [],
            trace,
            lambda _message: None,
        )
        == 0
    )
    assert executed == ["save-rejected-1", "save-rejected-2"]
    assert "factory_terminal_boundary" not in trace
    response, cost = fake_loop.call_llm_with_retry(task_id="task-rejected-batch")
    assert response["content"] == "provider response"
    assert cost == 1.0
    assert provider_calls == ["task-rejected-batch"]


def test_physical_request_guard_reserves_before_call_and_reconciles_exact_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    launcher = _load_runtime_launcher()
    ledger_path = tmp_path / "request-ledger.json"
    request_ledger.initialize_ledger(
        ledger_path,
        goal_id="campaign-authoring-copy-quality-v3-20260717",
        evaluation_id="eval_runtime_guard",
        provider="openrouter",
        model="z-ai/glm-5.2",
        input_price_per_token_usd="0.00000091",
        output_price_per_token_usd="0.00000286",
        price_source="https://openrouter.ai/api/v1/models",
        price_observed_at="2026-07-17T00:00:00Z",
    )
    request_ledger.bind_task(
        ledger_path,
        task_id="task_runtime_guard",
        case_id="DQ01",
        attempt_id="attempt_runtime_guard",
        request_digest="a" * 64,
    )
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")
    monkeypatch.setenv("CF_REQUEST_LEDGER_PATH", str(ledger_path))
    monkeypatch.setenv(
        "CF_REQUEST_LEDGER_GOAL_ID",
        "campaign-authoring-copy-quality-v3-20260717",
    )
    monkeypatch.setenv("CF_REQUEST_LEDGER_EVALUATION_ID", "eval_runtime_guard")
    reservations_seen_before_call: list[int] = []

    class FakeLLMClient:
        def _create_chat_completion_with_retries(
            self,
            create_fn: Any,
            kwargs: dict[str, Any],
            target: dict[str, Any],
        ) -> Any:
            del target
            return create_fn(**kwargs)

    fake_llm = SimpleNamespace(LLMClient=FakeLLMClient)
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.llm":
            return fake_llm
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)
    launcher.install_request_ledger_guard()

    def physical_create(**kwargs: Any) -> Any:
        del kwargs
        snapshot = request_ledger.read_ledger(ledger_path)
        reservations_seen_before_call.append(
            request_ledger.ledger_totals(snapshot)["inflight_requests"]
        )
        request_ledger.observe_active_response(
            status_code=200,
            generation_id="gen-runtime-guard-1234",
        )
        usage = SimpleNamespace(
            model_dump=lambda: {
                "prompt_tokens": 600,
                "completion_tokens": 200,
                "prompt_tokens_details": {"cached_tokens": 50},
            }
        )
        return SimpleNamespace(usage=usage)

    audit_token = launcher._PROVIDER_REQUEST_AUDIT.set(
        {
            "context": {
                "task_id": "task_runtime_guard",
                "drive_root": tmp_path,
                "category": "main_generation",
            },
            "category": "main_generation",
            "model": "z-ai/glm-5.2",
            "provider_call_id": "cf_provider_runtime_guard",
            "configured_max_output_tokens": 1_000,
        }
    )
    try:
        FakeLLMClient()._create_chat_completion_with_retries(
            physical_create,
            {
                "messages": [{"role": "user", "content": "synthetic"}],
                "max_tokens": 1_000,
            },
            {
                "provider": "openrouter",
                "resolved_model": "z-ai/glm-5.2",
                "usage_model": "z-ai/glm-5.2",
            },
        )
    finally:
        launcher._PROVIDER_REQUEST_AUDIT.reset(audit_token)

    document = request_ledger.read_ledger(ledger_path)
    row = document["requests"][0]
    assert reservations_seen_before_call == [1]
    assert row["status"] == "EXACT"
    assert row["exact_total_tokens"] == 800
    assert row["exact_cached_tokens"] == 50
    assert row["generation_id"] == "gen-runtime-guard-1234"
    assert row["configured_max_output_tokens"] == 1_000


def test_openrouter_generation_audit_persists_header_before_terminal_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    launcher = _load_runtime_launcher()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_MODEL", "openrouter::z-ai/glm-5.2")
    persisted: list[dict[str, Any]] = []

    class FakeHttpClient:
        def __init__(self) -> None:
            self.event_hooks: dict[str, list[Any]] = {"response": []}

    class FakeLLMClient:
        @classmethod
        def _make_no_proxy_client(
            cls,
            target: dict[str, Any],
            timeout: float | None = None,
        ) -> tuple[object, FakeHttpClient]:
            del cls, target, timeout
            return object(), FakeHttpClient()

        def chat(
            self,
            *,
            messages: list[dict[str, str]],
            model: str,
            max_tokens: int = 65_536,
        ) -> tuple[dict[str, str], dict[str, int]]:
            del messages, max_tokens
            _, http_client = self._make_no_proxy_client({"provider": "openrouter"})
            response = SimpleNamespace(
                headers={
                    "x-generation-id": "gen-HeaderSafe1234",
                    "authorization": "must-not-be-persisted",
                },
                status_code=200,
            )
            for hook in http_client.event_hooks["response"]:
                hook(response)
            return {"content": "ok"}, {"prompt_tokens": 10, "completion_tokens": 2}

    def call_llm_with_retry(
        llm: FakeLLMClient,
        messages: list[dict[str, str]],
        model: str,
        tools: object,
        effort: str,
        max_retries: int,
        drive_logs: pathlib.Path,
        task_id: str,
        round_idx: int,
        event_queue: object,
        accumulated_usage: dict[str, Any],
    ) -> tuple[dict[str, str], int]:
        del tools, effort, max_retries, drive_logs, task_id, round_idx, event_queue
        message, _ = llm.chat(messages=messages, model=model, max_tokens=10_240)
        accumulated_usage["called"] = True
        return message, 0

    def chat_observed(llm: FakeLLMClient, **kwargs: Any) -> Any:
        return llm.chat(messages=kwargs["messages"], model=kwargs["model"])

    fake_llm = SimpleNamespace(LLMClient=FakeLLMClient)
    fake_loop = SimpleNamespace(call_llm_with_retry=call_llm_with_retry)
    fake_observability = SimpleNamespace(chat_observed=chat_observed)
    fake_utils = SimpleNamespace(
        utc_now_iso=lambda: "2026-07-15T00:00:00+00:00",
        append_jsonl=lambda path, event: persisted.append({"path": str(path), **event}) is None,
    )
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.llm":
            return fake_llm
        if name == "ouroboros.loop_llm_call":
            return fake_loop
        if name == "ouroboros.llm_observability":
            return fake_observability
        if name == "ouroboros.utils":
            return fake_utils
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)
    launcher.install_openrouter_generation_audit()

    result, _ = fake_loop.call_llm_with_retry(
        FakeLLMClient(),
        [{"role": "user", "content": "synthetic prompt content must stay private"}],
        "openrouter::z-ai/glm-5.2",
        None,
        "low",
        1,
        tmp_path / "logs",
        "task-generation-audit",
        1,
        None,
        {},
    )

    assert result == {"content": "ok"}
    assert [event["type"] for event in persisted] == [
        "provider_request_headers",
        "provider_request_terminal",
    ]
    assert persisted[0]["generation_id"] == "gen-HeaderSafe1234"
    assert persisted[0]["task_id"] == "task-generation-audit"
    assert persisted[0]["estimated_prompt_tokens"] > 0
    assert persisted[0]["configured_max_output_tokens"] == 10_240
    assert persisted[0]["prompt_estimation_method"] == ("serialized_request_unicode_chars_div_2_v2")
    assert persisted[1]["usage_observed"] is True
    assert persisted[1]["physical_response_count"] == 1
    assert persisted[1]["estimated_prompt_tokens"] == persisted[0]["estimated_prompt_tokens"]
    assert persisted[1]["configured_max_output_tokens"] == 10_240
    assert "must-not-be-persisted" not in json.dumps(persisted)
    assert "synthetic prompt content" not in json.dumps(persisted)


def test_generation_header_filter_rejects_arbitrary_metadata() -> None:
    launcher = _load_runtime_launcher()

    assert launcher._safe_generation_id({"x-generation-id": "gen-Safe_12345678"}) == (
        "gen-Safe_12345678"
    )
    assert launcher._safe_generation_id({"x-generation-id": "unsafe secret value"}) is None
    assert launcher._safe_generation_id({}) is None


def test_post_task_usage_event_is_redacted_and_normalized() -> None:
    launcher = _load_runtime_launcher()

    event = launcher.post_task_usage_event(
        category="post_task_summary",
        task_id="task-1",
        model="openai::gpt-5.4-mini",
        usage={
            "provider": "openai",
            "input_tokens": "120",
            "output_tokens": 30,
            "cost": "0.004",
            "request": "must-not-be-copied",
            "response": "must-not-be-copied",
        },
    )

    assert event == {
        "type": "llm_usage",
        "task_id": "task-1",
        "category": "post_task_summary",
        "model": "openai::gpt-5.4-mini",
        "provider": "openai",
        "source": "communication_factory_runtime_adapter",
        "cost_estimated": False,
        "cost": 0.004,
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
    }


def test_post_task_usage_event_rejects_unowned_category() -> None:
    launcher = _load_runtime_launcher()

    try:
        launcher.post_task_usage_event(
            category="main_generation",
            task_id="task-1",
            model="openai::gpt-5.4-mini",
            usage={},
        )
    except ValueError as exc:
        assert str(exc) == "unsupported post-task usage category"
    else:
        raise AssertionError("unowned provider category was accepted")


def test_factory_tool_result_transport_event_contains_metadata_only() -> None:
    launcher = _load_runtime_launcher()
    raw = json.dumps(
        {
            "ready": True,
            "output_schema": {"marker": "must-not-appear-in-telemetry"},
        }
    )
    visible = raw[:32] + "\n... (truncated)"

    event = launcher.factory_tool_result_transport_event(
        tool_name="mcp_factory__cf_context_get",
        raw_result=raw,
        visible_result=visible,
        ordinal=2,
    )

    assert event == {
        "type": "factory_tool_result_transport",
        "tool": "mcp_factory__cf_context_get",
        "ordinal": 2,
        "limit_chars": 80_000,
        "raw_chars": len(raw),
        "visible_chars": len(visible),
        "truncated": True,
        "raw_json_valid": True,
        "visible_json_valid": False,
        "top_level_keys": ["output_schema", "ready"],
    }
    assert "must-not-appear-in-telemetry" not in json.dumps(event)


def test_factory_context_result_is_preserved_without_weakening_other_tool_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_runtime_launcher()
    tool_limits: dict[str, int] = {}

    def truncate_result(
        result: Any,
        tool_name: str = "",
        tool_args: dict[str, Any] | None = None,
    ) -> str:
        del tool_args
        text = str(result)
        limit = tool_limits.get(tool_name, 15_000)
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n... (truncated from {len(text)} chars, limit={limit})"

    def process_tool_results(
        results: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        llm_trace: dict[str, Any],
        emit_progress: Any,
    ) -> int:
        del llm_trace, emit_progress
        for result in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result["tool_call_id"],
                    "content": truncate_result(
                        result["result"],
                        str(result["fn_name"]),
                        result.get("tool_args"),
                    ),
                }
            )
        return 0

    fake_loop = SimpleNamespace(
        _TOOL_RESULT_LIMITS=tool_limits,
        process_tool_results=process_tool_results,
    )
    real_import = launcher.importlib.import_module

    def import_module(name: str) -> Any:
        if name == "ouroboros.loop_tool_execution":
            return fake_loop
        return real_import(name)

    monkeypatch.setattr(launcher.importlib, "import_module", import_module)
    launcher.install_factory_tool_result_transport()

    long_json = json.dumps(
        {"ready": True, "output_schema": {"payload": "x" * 16_000}},
        separators=(",", ":"),
    )
    context_messages: list[dict[str, Any]] = []
    context_trace: dict[str, Any] = {}
    context_result = {
        "tool_call_id": "call-context",
        "fn_name": "mcp_factory__cf_context_get",
        "result": long_json,
        "tool_args": {},
    }
    assert (
        fake_loop.process_tool_results(
            [context_result], context_messages, context_trace, lambda _: None
        )
        == 0
    )

    assert tool_limits == {
        "mcp_factory__cf_context_get": 80_000,
        "mcp_factory__cf_script_context_get": 80_000,
    }
    assert context_messages[0]["content"] == long_json
    assert json.loads(context_messages[0]["content"])["ready"] is True
    assert context_trace["factory_tool_result_transport"] == [
        {
            "type": "factory_tool_result_transport",
            "tool": "mcp_factory__cf_context_get",
            "ordinal": 1,
            "limit_chars": 80_000,
            "raw_chars": len(long_json),
            "visible_chars": len(long_json),
            "truncated": False,
            "raw_json_valid": True,
            "visible_json_valid": True,
            "top_level_keys": ["output_schema", "ready"],
        }
    ]

    ordinary_messages: list[dict[str, Any]] = []
    ordinary_trace: dict[str, Any] = {}
    ordinary_result = {
        "tool_call_id": "call-ordinary",
        "fn_name": "unknown_tool",
        "result": long_json,
        "tool_args": {},
    }
    fake_loop.process_tool_results(
        [ordinary_result], ordinary_messages, ordinary_trace, lambda _: None
    )
    assert "truncated from" in ordinary_messages[0]["content"]
    assert "factory_tool_result_transport" not in ordinary_trace

    oversized_json = json.dumps(
        {"ready": True, "output_schema": {"payload": "x" * 81_000}},
        separators=(",", ":"),
    )
    oversized_messages: list[dict[str, Any]] = []
    oversized_trace: dict[str, Any] = {}
    oversized_result = {
        "tool_call_id": "call-oversized",
        "fn_name": "mcp_factory__cf_script_context_get",
        "result": oversized_json,
        "tool_args": {},
    }
    fake_loop.process_tool_results(
        [oversized_result], oversized_messages, oversized_trace, lambda _: None
    )
    assert "truncated from" in oversized_messages[0]["content"]
    assert oversized_trace["factory_tool_result_transport"][0]["truncated"] is True
    assert oversized_trace["factory_tool_result_transport"][0]["visible_json_valid"] is False

    installed = fake_loop.process_tool_results
    launcher.install_factory_tool_result_transport()
    assert fake_loop.process_tool_results is installed
