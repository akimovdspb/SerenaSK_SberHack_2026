from __future__ import annotations

import contextvars
import copy
import functools
import importlib
import inspect
import json
import logging
import os
import pathlib
import re
import sys
import uuid
from collections.abc import Mapping
from typing import Any, cast

import request_ledger
from provider_profiles import (
    CANONICAL_PROFILE_NAME,
    GLM_FUNCTIONAL_PROFILE_NAME,
    normalize_provider_model,
    provider_profile,
)

NON_PERSISTED_SECRET_KEYS = (
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "GIGACHAT_CREDENTIALS",
    "GIGACHAT_PASSWORD",
    "GITHUB_TOKEN",
    "OUROBOROS_NETWORK_PASSWORD",
)
DEFAULT_OPENAI_MODEL = "openai::gpt-5.4-mini"
RAILWAY_OPENROUTER_MODEL = "openrouter::z-ai/glm-5.2"
PINNED_MAIN_LOOP_MAX_TOKENS = provider_profile(CANONICAL_PROFILE_NAME).main_loop_max_tokens
GLM_MAIN_LOOP_MAX_TOKENS = provider_profile(GLM_FUNCTIONAL_PROFILE_NAME).main_loop_max_tokens
FACTORY_CONTEXT_TOOL_RESULT_LIMIT = 80_000
FACTORY_CONTEXT_TOOL_NAMES = frozenset(
    {
        "mcp_factory__cf_context_get",
        "mcp_factory__cf_script_context_get",
    }
)
POST_TASK_CATEGORIES = {
    "post_task_summary",
    "post_task_reflection",
    "post_task_evolution_decision",
}
SAFETY_CALL_TYPES = frozenset({"safety_supervisor", "safety_supervisor_repair"})
SAFETY_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "communication_factory_safety_decision",
        "strict": True,
        "schema": {
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
        },
    },
}
_RUNTIME_LOGGER = logging.getLogger("communication_factory.runtime")
_POST_TASK_AUDIT: contextvars.ContextVar[tuple[str, str, pathlib.Path] | None] = (
    contextvars.ContextVar("communication_factory_post_task_audit", default=None)
)
_PROVIDER_CALL_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "communication_factory_provider_call_context", default=None
)
_PROVIDER_REQUEST_AUDIT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "communication_factory_provider_request_audit", default=None
)
_FACTORY_TERMINAL_ANSWER: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "communication_factory_terminal_answer", default=None
)
_OPENROUTER_GENERATION_ID = re.compile(r"gen-[A-Za-z0-9_-]{8,128}")
FACTORY_DRAFT_SAVE_TOOL = "mcp_factory__cf_draft_save"


def _requested_tool_name(tool_call: Any) -> str:
    if not isinstance(tool_call, Mapping):
        return ""
    function = tool_call.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "").strip()
    return str(tool_call.get("tool") or "").strip()


def _json_transport_metadata(value: str) -> tuple[bool, list[str]]:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return False, []
    if not isinstance(parsed, dict):
        return True, []
    return True, sorted(str(key) for key in parsed)[:32]


def factory_tool_result_transport_event(
    *,
    tool_name: str,
    raw_result: Any,
    visible_result: Any,
    ordinal: int,
) -> dict[str, Any]:
    raw_text = str(raw_result)
    visible_text = str(visible_result)
    raw_json_valid, top_level_keys = _json_transport_metadata(raw_text)
    visible_json_valid, _ = _json_transport_metadata(visible_text)
    return {
        "type": "factory_tool_result_transport",
        "tool": tool_name,
        "ordinal": max(1, int(ordinal)),
        "limit_chars": FACTORY_CONTEXT_TOOL_RESULT_LIMIT,
        "raw_chars": len(raw_text),
        "visible_chars": len(visible_text),
        "truncated": raw_text != visible_text,
        "raw_json_valid": raw_json_valid,
        "visible_json_valid": visible_json_valid,
        "top_level_keys": top_level_keys,
    }


def install_factory_tool_result_transport() -> None:
    loop_tool_execution: Any = importlib.import_module("ouroboros.loop_tool_execution")
    current_process = loop_tool_execution.process_tool_results
    if getattr(current_process, "_communication_factory_transport_guard", False):
        return

    tool_limits = getattr(loop_tool_execution, "_TOOL_RESULT_LIMITS", None)
    if not isinstance(tool_limits, dict):
        raise RuntimeError("Ouroboros tool-result limit registry is unavailable")
    for tool_name in FACTORY_CONTEXT_TOOL_NAMES:
        tool_limits[tool_name] = FACTORY_CONTEXT_TOOL_RESULT_LIMIT

    original_process = current_process

    @functools.wraps(original_process)
    def observed_process(
        results: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        llm_trace: dict[str, Any],
        emit_progress: Any,
    ) -> int:
        message_start = len(messages)
        error_count = int(original_process(results, messages, llm_trace, emit_progress))
        visible_by_call = {
            str(message.get("tool_call_id") or ""): message.get("content", "")
            for message in messages[message_start:]
            if message.get("role") == "tool"
        }
        records = llm_trace.get("factory_tool_result_transport")
        if not isinstance(records, list):
            records = []
        for result in results:
            tool_name = str(result.get("fn_name") or "")
            if tool_name not in FACTORY_CONTEXT_TOOL_NAMES:
                continue
            llm_trace["factory_tool_result_transport"] = records
            event = factory_tool_result_transport_event(
                tool_name=tool_name,
                raw_result=result.get("result", ""),
                visible_result=visible_by_call.get(str(result.get("tool_call_id") or ""), ""),
                ordinal=(
                    1
                    + sum(
                        record.get("tool") == tool_name
                        for record in records
                        if isinstance(record, dict)
                    )
                ),
            )
            records.append(event)
            _RUNTIME_LOGGER.info(
                "CF_TOOL_RESULT_TRANSPORT %s",
                json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            )
        return error_count

    observed_process._communication_factory_transport_guard = True  # type: ignore[attr-defined]
    loop_tool_execution.process_tool_results = observed_process


def settings_for_persistence(settings: Mapping[str, Any]) -> dict[str, Any]:
    persisted = dict(settings)
    for key in NON_PERSISTED_SECRET_KEYS:
        if key in persisted:
            persisted[key] = ""
    return persisted


def canonical_runtime_models() -> dict[str, str]:
    provider = str(os.environ.get("CF_RUNTIME_PROVIDER") or "openai").strip().lower()
    model = DEFAULT_OPENAI_MODEL
    if provider == "openrouter":
        if str(os.environ.get("OPENROUTER_ENABLED") or "").strip().lower() != "true":
            raise RuntimeError("OpenRouter runtime requires OPENROUTER_ENABLED=true")
        configured = str(os.environ.get("OUROBOROS_MODEL") or RAILWAY_OPENROUTER_MODEL).strip()
        if configured == RAILWAY_OPENROUTER_MODEL.removeprefix("openrouter::"):
            configured = f"openrouter::{configured}"
        if configured != RAILWAY_OPENROUTER_MODEL:
            raise RuntimeError("OpenRouter runtime requires the approved z-ai/glm-5.2 route")
        model = configured
    elif provider != "openai":
        raise RuntimeError("CF_RUNTIME_PROVIDER must be openai or openrouter")
    return {
        "OUROBOROS_MODEL": model,
        "OUROBOROS_MODEL_HEAVY": model,
        "OUROBOROS_MODEL_LIGHT": model,
        "OUROBOROS_MODEL_FALLBACKS": "",
        "OUROBOROS_REVIEW_MODELS": model,
        "OUROBOROS_SCOPE_REVIEW_MODEL": model,
        "OUROBOROS_SCOPE_REVIEW_MODELS": model,
    }


def settings_for_p0_runtime(settings: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    normalized = dict(settings)
    changed: list[str] = []
    for key, value in canonical_runtime_models().items():
        if normalized.get(key) != value:
            normalized[key] = value
            changed.append(key)
    return normalized, changed


def assert_runtime_model_route(model: str | None) -> str:
    expected_route = canonical_runtime_models()["OUROBOROS_MODEL"]
    requested_route = str(model or expected_route).strip()
    expected_provider = expected_route.split("::", 1)[0]
    requested_provider = ""
    if "::" in requested_route:
        requested_provider = requested_route.split("::", 1)[0]
    elif requested_route.startswith(("openai/", "openrouter/")):
        requested_provider = requested_route.split("/", 1)[0]
    if (requested_provider and requested_provider != expected_provider) or normalize_provider_model(
        requested_route
    ) != normalize_provider_model(expected_route):
        raise RuntimeError("Ouroboros LLM call model differs from the selected P0 profile")
    return expected_route


def install_profile_model_guard() -> None:
    """Bind every pinned-runtime LLM role to the selected P0 model route."""
    llm_module: Any = importlib.import_module("ouroboros.llm")
    consolidator: Any = importlib.import_module("ouroboros.consolidator")
    LLMClient = llm_module.LLMClient
    current_chat = LLMClient.chat
    expected_route = assert_runtime_model_route(None)

    # Pinned v6.61.4 hard-codes Gemini for consolidation/task summaries. The
    # factory profile is intentionally single-model, so normalize that role too.
    consolidator.CONSOLIDATION_MODEL = expected_route
    llm_module.DEFAULT_LIGHT_MODEL = expected_route
    if getattr(current_chat, "_communication_factory_model_guard", False):
        return

    signature = inspect.signature(current_chat)

    @functools.wraps(current_chat)
    def guarded_chat(client: Any, *args: Any, **kwargs: Any) -> Any:
        bound = signature.bind_partial(client, *args, **kwargs)
        assert_runtime_model_route(bound.arguments.get("model"))
        return current_chat(client, *args, **kwargs)

    guarded_chat._communication_factory_model_guard = True  # type: ignore[attr-defined]
    LLMClient.chat = guarded_chat


def install_glm_main_loop_output_cap() -> None:
    """Bound runaway GLM turns without changing the canonical OpenAI profile.

    The pinned main loop exposes a 65,536-token generic ceiling. That ceiling is
    larger than the reviewed P0 DraftEnvelope needs and lets a stalled GLM tool
    turn consume the entire fixed task deadline. Keep the upstream value as a
    drift assertion, then narrow only the explicit GLM functional route.
    """
    if canonical_runtime_models()["OUROBOROS_MODEL"] != RAILWAY_OPENROUTER_MODEL:
        return

    selected_profile_name = str(
        os.environ.get("CF_PROVIDER_PROFILE") or GLM_FUNCTIONAL_PROFILE_NAME
    ).strip()
    selected_profile = provider_profile(selected_profile_name)
    if selected_profile.runtime_route != RAILWAY_OPENROUTER_MODEL:
        raise RuntimeError("selected GLM profile route differs from the pinned runtime route")
    selected_output_cap = selected_profile.main_loop_max_tokens
    loop_llm_call: Any = importlib.import_module("ouroboros.loop_llm_call")
    current = getattr(loop_llm_call, "MAIN_LOOP_MAX_TOKENS", None)
    if current == selected_output_cap:
        return
    if current != PINNED_MAIN_LOOP_MAX_TOKENS:
        raise RuntimeError("pinned main-loop output-token contract drifted")
    loop_llm_call.MAIN_LOOP_MAX_TOKENS = selected_output_cap


def safety_response_format() -> dict[str, Any]:
    """Return the strict GLM safety verdict contract without sharing mutable state."""
    return copy.deepcopy(SAFETY_RESPONSE_SCHEMA)


def install_glm_safety_response_schema() -> None:
    """Require a typed safety verdict while retaining the pinned full-safety flow.

    Pinned Ouroboros requests only ``json_object`` for safety decisions. That keeps
    the response syntactically JSON but does not constrain the verdict enum. The
    GLM functional profile therefore tightens only the two existing safety call
    types at their provider request boundary. Parsing, fail-closed behavior, the
    safety prompt, model route and retry implementation remain upstream-owned.
    """
    if canonical_runtime_models()["OUROBOROS_MODEL"] != RAILWAY_OPENROUTER_MODEL:
        return

    observability: Any = importlib.import_module("ouroboros.llm_observability")
    current_chat_observed = observability.chat_observed
    if getattr(current_chat_observed, "_communication_factory_safety_schema_guard", False):
        return

    @functools.wraps(current_chat_observed)
    def structured_chat_observed(*args: Any, **kwargs: Any) -> Any:
        call_type = str(kwargs.get("call_type") or "")
        if call_type in SAFETY_CALL_TYPES:
            response_format = kwargs.get("response_format")
            if response_format not in ({"type": "json_object"}, SAFETY_RESPONSE_SCHEMA):
                raise RuntimeError("pinned safety response-format contract drifted")
            kwargs = {**kwargs, "response_format": safety_response_format()}
        return current_chat_observed(*args, **kwargs)

    structured_chat_observed._communication_factory_safety_schema_guard = True  # type: ignore[attr-defined]
    observability.chat_observed = structured_chat_observed


def _provider_event_category(value: Any) -> str:
    category = str(value or "").strip()
    if category in {"task", "main_generation", "llm_call"}:
        return "main_generation"
    if category in SAFETY_CALL_TYPES or category == "safety":
        return "safety"
    return category or "unattributed"


def _append_provider_event(context: Mapping[str, Any], event: dict[str, Any]) -> None:
    """Persist only safe provider correlation metadata in the task event stream."""
    try:
        utils: Any = importlib.import_module("ouroboros.utils")
        drive_root = pathlib.Path(context["drive_root"])
        event = {
            "ts": utils.utc_now_iso(),
            "task_id": str(context.get("task_id") or ""),
            **event,
        }
        if not utils.append_jsonl(drive_root / "logs" / "events.jsonl", event):
            raise RuntimeError("provider correlation event was not persisted")
        _RUNTIME_LOGGER.info(
            "CF_PROVIDER_REQUEST %s",
            json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        )
    except Exception:
        _RUNTIME_LOGGER.exception("Failed to persist safe provider request correlation metadata")


def _safe_generation_id(headers: Any) -> str | None:
    try:
        value = str(headers.get("x-generation-id") or "").strip()
    except Exception:
        return None
    return value if _OPENROUTER_GENERATION_ID.fullmatch(value) else None


def _prompt_token_estimate(arguments: Mapping[str, Any]) -> int:
    """Return the content-free estimate used by the durable request ledger."""
    prompt_surfaces = {
        "messages": arguments.get("messages") or [],
        "tools": arguments.get("tools") or [],
        "response_format": arguments.get("response_format"),
    }
    estimate, _digest = request_ledger.serialized_request_metrics(prompt_surfaces)
    return estimate


def observe_openrouter_response_headers(response: Any) -> None:
    """Durably capture the generation ID as soon as HTTP headers arrive.

    The hook intentionally does not read the body and does not persist arbitrary headers,
    prompts, responses, credentials, URLs or key-derived metadata.
    """
    audit = _PROVIDER_REQUEST_AUDIT.get()
    if not isinstance(audit, dict):
        return
    try:
        generation_id = _safe_generation_id(response.headers)
        response_record = {
            "generation_id": generation_id,
            "status_code": int(response.status_code),
        }
        request_ledger.observe_active_response(
            status_code=response_record["status_code"],
            generation_id=generation_id,
        )
        responses = audit.setdefault("responses", [])
        if isinstance(responses, list):
            responses.append(response_record)
        context = audit["context"]
        _append_provider_event(
            context,
            {
                "type": "provider_request_headers",
                "provider": "openrouter",
                "category": audit["category"],
                "model": audit["model"],
                "provider_call_id": audit["provider_call_id"],
                "generation_id": generation_id,
                "generation_id_present": generation_id is not None,
                "status_code": response_record["status_code"],
                "estimated_prompt_tokens": audit["estimated_prompt_tokens"],
                "configured_max_output_tokens": audit["configured_max_output_tokens"],
                "prompt_estimation_method": request_ledger.PROMPT_ESTIMATION_METHOD,
            },
        )
    except Exception:
        _RUNTIME_LOGGER.exception("Failed to observe safe OpenRouter response metadata")


def _provider_terminal_event(
    audit: Mapping[str, Any],
    *,
    status: str,
    usage_observed: bool,
    error_type: str | None = None,
) -> None:
    responses = audit.get("responses")
    response_rows = responses if isinstance(responses, list) else []
    generation_ids = sorted(
        {
            str(row.get("generation_id"))
            for row in response_rows
            if isinstance(row, dict) and row.get("generation_id")
        }
    )
    event: dict[str, Any] = {
        "type": "provider_request_terminal",
        "provider": "openrouter",
        "category": str(audit.get("category") or "unattributed"),
        "model": str(audit.get("model") or ""),
        "provider_call_id": str(audit.get("provider_call_id") or ""),
        "status": status,
        "physical_response_count": len(response_rows),
        "generation_ids": generation_ids,
        "usage_observed": usage_observed,
        "estimated_prompt_tokens": int(audit.get("estimated_prompt_tokens") or 0),
        "configured_max_output_tokens": int(audit.get("configured_max_output_tokens") or 0),
        "prompt_estimation_method": request_ledger.PROMPT_ESTIMATION_METHOD,
    }
    if error_type:
        event["error_type"] = error_type
    _append_provider_event(audit["context"], event)


def install_openrouter_generation_audit() -> None:
    """Make header-only OpenRouter attempts recoverable and accounting-visible."""
    if canonical_runtime_models()["OUROBOROS_MODEL"] != RAILWAY_OPENROUTER_MODEL:
        return

    llm_module: Any = importlib.import_module("ouroboros.llm")
    loop_llm_call: Any = importlib.import_module("ouroboros.loop_llm_call")
    observability: Any = importlib.import_module("ouroboros.llm_observability")
    LLMClient = llm_module.LLMClient

    current_make_client = LLMClient._make_no_proxy_client
    if not getattr(current_make_client, "_communication_factory_generation_headers", False):

        def observed_make_client(
            cls: Any,
            target: dict[str, Any],
            timeout: float | None = None,
        ) -> tuple[Any, Any]:
            oa_client, http_client = current_make_client(target, timeout=timeout)
            if str(target.get("provider") or "").lower() == "openrouter":
                hooks = http_client.event_hooks.setdefault("response", [])
                hooks.append(observe_openrouter_response_headers)
            return oa_client, http_client

        observed_make_client._communication_factory_generation_headers = True  # type: ignore[attr-defined]
        LLMClient._make_no_proxy_client = classmethod(observed_make_client)

    current_loop_call = loop_llm_call.call_llm_with_retry
    if not getattr(current_loop_call, "_communication_factory_generation_context", False):
        loop_signature = inspect.signature(current_loop_call)

        @functools.wraps(current_loop_call)
        def contextual_loop_call(*args: Any, **kwargs: Any) -> Any:
            bound = loop_signature.bind_partial(*args, **kwargs)
            drive_logs = pathlib.Path(bound.arguments["drive_logs"])
            token = _PROVIDER_CALL_CONTEXT.set(
                {
                    "task_id": str(bound.arguments.get("task_id") or ""),
                    "drive_root": drive_logs.parent,
                    "category": "main_generation",
                }
            )
            try:
                return current_loop_call(*args, **kwargs)
            finally:
                _PROVIDER_CALL_CONTEXT.reset(token)

        contextual_loop_call._communication_factory_generation_context = True  # type: ignore[attr-defined]
        loop_llm_call.call_llm_with_retry = contextual_loop_call
        loaded_loop = sys.modules.get("ouroboros.loop")
        if loaded_loop is not None and getattr(loaded_loop, "call_llm_with_retry", None) is (
            current_loop_call
        ):
            cast(Any, loaded_loop).call_llm_with_retry = contextual_loop_call

    current_observed = observability.chat_observed
    if not getattr(current_observed, "_communication_factory_generation_context", False):

        @functools.wraps(current_observed)
        def contextual_chat_observed(*args: Any, **kwargs: Any) -> Any:
            post_task = _POST_TASK_AUDIT.get()
            category = (
                post_task[0]
                if post_task is not None
                else _provider_event_category(kwargs.get("call_type"))
            )
            token = _PROVIDER_CALL_CONTEXT.set(
                {
                    "task_id": str(kwargs.get("task_id") or (post_task[1] if post_task else "")),
                    "drive_root": pathlib.Path(
                        kwargs.get("drive_root") or (post_task[2] if post_task else "../data")
                    ),
                    "category": category,
                }
            )
            try:
                return current_observed(*args, **kwargs)
            finally:
                _PROVIDER_CALL_CONTEXT.reset(token)

        contextual_chat_observed._communication_factory_generation_context = True  # type: ignore[attr-defined]
        observability.chat_observed = contextual_chat_observed

    current_chat = LLMClient.chat
    if getattr(current_chat, "_communication_factory_generation_audit", False):
        return
    chat_signature = inspect.signature(current_chat)

    @functools.wraps(current_chat)
    def audited_provider_chat(client: Any, *args: Any, **kwargs: Any) -> Any:
        context = _PROVIDER_CALL_CONTEXT.get()
        post_task = _POST_TASK_AUDIT.get()
        if context is None and post_task is not None:
            context = {
                "task_id": post_task[1],
                "drive_root": post_task[2],
                "category": post_task[0],
            }
        if context is None and str(os.environ.get("CF_REQUEST_LEDGER_PATH") or "").strip():
            context = {
                "task_id": f"ledger_lifecycle_{uuid.uuid4().hex}",
                "drive_root": pathlib.Path(
                    os.environ.get("OUROBOROS_DATA_DIR") or "/home/ouroboros/Ouroboros/data"
                ),
                "category": "schema_probe",
            }
        bound = chat_signature.bind_partial(client, *args, **kwargs)
        bound.apply_defaults()
        model = str(bound.arguments.get("model") or "")
        if context is None or normalize_provider_model(model) != normalize_provider_model(
            RAILWAY_OPENROUTER_MODEL
        ):
            return current_chat(client, *args, **kwargs)
        audit: dict[str, Any] = {
            "context": context,
            "category": _provider_event_category(context.get("category")),
            "model": normalize_provider_model(model),
            "provider_call_id": f"cf_provider_{uuid.uuid4().hex}",
            "responses": [],
            "estimated_prompt_tokens": _prompt_token_estimate(bound.arguments),
            "configured_max_output_tokens": max(
                1,
                int(bound.arguments.get("max_tokens") or PINNED_MAIN_LOOP_MAX_TOKENS),
            ),
        }
        token = _PROVIDER_REQUEST_AUDIT.set(audit)
        try:
            result = current_chat(client, *args, **kwargs)
        except BaseException as exc:
            _provider_terminal_event(
                audit,
                status="failed",
                usage_observed=False,
                error_type=type(exc).__name__,
            )
            raise
        else:
            usage = result[1] if isinstance(result, tuple) and len(result) == 2 else None
            usage_observed = isinstance(usage, Mapping) and (
                usage.get("prompt_tokens") is not None
                and usage.get("completion_tokens") is not None
            )
            _provider_terminal_event(
                audit,
                status="completed",
                usage_observed=usage_observed,
            )
            return result
        finally:
            _PROVIDER_REQUEST_AUDIT.reset(token)

    audited_provider_chat._communication_factory_generation_audit = True  # type: ignore[attr-defined]
    LLMClient.chat = audited_provider_chat


def _physical_response_usage(response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, Mapping):
        return None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if prompt_tokens is None or completion_tokens is None:
        return None
    details = usage.get("prompt_tokens_details")
    if hasattr(details, "model_dump"):
        details = details.model_dump()
    cached_tokens = details.get("cached_tokens", 0) if isinstance(details, Mapping) else 0
    provider_cost = usage.get("cost")
    if provider_cost is None:
        provider_cost = usage.get("total_cost")
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens or 0,
        "cost_usd": provider_cost,
    }


def _physical_output_ceiling(kwargs: Mapping[str, Any], audit: Mapping[str, Any]) -> int:
    for key in ("max_completion_tokens", "max_tokens"):
        value = kwargs.get(key)
        if value is not None:
            return max(1, int(value))
    return max(1, int(audit.get("configured_max_output_tokens") or PINNED_MAIN_LOOP_MAX_TOKENS))


def install_request_ledger_guard() -> None:
    """Reserve and reconcile every physical OpenRouter request durably."""

    ledger_path = str(os.environ.get("CF_REQUEST_LEDGER_PATH") or "").strip()
    if not ledger_path:
        return
    goal_id = str(os.environ.get("CF_REQUEST_LEDGER_GOAL_ID") or "").strip()
    evaluation_id = str(os.environ.get("CF_REQUEST_LEDGER_EVALUATION_ID") or "").strip()
    default_case_id = str(os.environ.get("CF_REQUEST_LEDGER_DEFAULT_CASE_ID") or "").strip()
    default_attempt_id = str(os.environ.get("CF_REQUEST_LEDGER_DEFAULT_ATTEMPT_ID") or "").strip()
    if not goal_id or not evaluation_id:
        raise RuntimeError("request ledger goal and evaluation identities are required")
    document = request_ledger.read_ledger(ledger_path)
    if document.get("goal_id") != goal_id or document.get("evaluation_id") != evaluation_id:
        raise RuntimeError("request ledger identity differs from the active qualification run")
    route = document.get("route")
    if (
        not isinstance(route, Mapping)
        or route.get("provider") != "openrouter"
        or route.get("model") != normalize_provider_model(RAILWAY_OPENROUTER_MODEL)
    ):
        raise RuntimeError("request ledger route differs from the approved OpenRouter route")

    llm_module: Any = importlib.import_module("ouroboros.llm")
    LLMClient = llm_module.LLMClient
    current_create = LLMClient._create_chat_completion_with_retries
    if getattr(current_create, "_communication_factory_request_ledger", False):
        return

    @functools.wraps(current_create)
    def guarded_create(
        client: Any,
        create_fn: Any,
        kwargs: dict[str, Any],
        target: dict[str, Any],
    ) -> Any:
        audit = _PROVIDER_REQUEST_AUDIT.get()
        if not isinstance(audit, dict):
            raise RuntimeError("physical provider request has no logical audit identity")
        context = audit.get("context")
        if not isinstance(context, Mapping):
            raise RuntimeError("physical provider request has no task context")
        task_id = str(context.get("task_id") or "").strip()
        category = _provider_event_category(context.get("category"))
        provider = str(target.get("provider") or "").strip().lower()
        model = normalize_provider_model(
            str(
                target.get("usage_model")
                or target.get("resolved_model")
                or audit.get("model")
                or ""
            )
        )

        def physical_create(**physical_kwargs: Any) -> Any:
            prompt_estimate, request_digest = request_ledger.serialized_request_metrics(
                physical_kwargs
            )
            reservation = request_ledger.reserve_request(
                ledger_path,
                task_id=task_id,
                category=category,
                provider=provider,
                model=model,
                provider_call_id=(f"{audit.get('provider_call_id')}:physical:{uuid.uuid4().hex}"),
                estimated_prompt_tokens=prompt_estimate,
                configured_max_output_tokens=_physical_output_ceiling(
                    physical_kwargs,
                    audit,
                ),
                request_digest=request_digest,
                default_case_id=default_case_id or None,
                default_attempt_id=default_attempt_id or None,
            )
            request_id = str(reservation["request_id"])
            active_token = request_ledger.activate_request(ledger_path, request_id)
            try:
                response = create_fn(**physical_kwargs)
            except BaseException as exc:
                try:
                    request_ledger.finalize_failure(
                        ledger_path,
                        request_id=request_id,
                        failure_type=type(exc).__name__,
                    )
                except BaseException as accounting_exc:
                    raise accounting_exc from exc
                raise
            else:
                usage = _physical_response_usage(response)
                if usage is None:
                    request_ledger.retain_unknown(
                        ledger_path,
                        request_id=request_id,
                        failure_type="MissingPhysicalResponseUsage",
                    )
                else:
                    request_ledger.reconcile_exact(
                        ledger_path,
                        request_id=request_id,
                        prompt_tokens=usage["prompt_tokens"],
                        completion_tokens=usage["completion_tokens"],
                        cached_tokens=usage["cached_tokens"],
                        cost_usd=usage["cost_usd"],
                        usage_source="physical_provider_response",
                    )
                return response
            finally:
                request_ledger.reset_active_request(active_token)

        return current_create(client, physical_create, kwargs, target)

    guarded_create._communication_factory_request_ledger = True  # type: ignore[attr-defined]
    LLMClient._create_chat_completion_with_retries = guarded_create


def _first_json_object(value: Any) -> dict[str, Any] | None:
    text = str(value or "")
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return {str(key): item for key, item in parsed.items()}
    return None


def factory_saved_draft_final_answer(tool_records: list[dict[str, Any]]) -> str | None:
    """Derive the declared task answer only from a successful persisted save receipt."""
    for record in tool_records:
        if (
            str(record.get("tool") or "") != FACTORY_DRAFT_SAVE_TOOL
            or record.get("is_error") is not False
        ):
            continue
        payload = _first_json_object(record.get("result"))
        if (
            payload is None
            or payload.get("status") != "SAVED"
            or payload.get("persisted") is not True
        ):
            continue
        campaign_id = str(payload.get("campaign_id") or "").strip()
        operation = str(payload.get("operation") or "").strip()
        draft_id = str(payload.get("draft_id") or "").strip()
        iteration = payload.get("iteration")
        blockers = payload.get("blockers")
        warnings = payload.get("warnings")
        if (
            not campaign_id
            or not operation
            or not draft_id
            or not isinstance(iteration, int)
            or isinstance(iteration, bool)
            or not isinstance(blockers, list)
            or not all(isinstance(item, str) for item in blockers)
            or not isinstance(warnings, list)
            or not all(isinstance(item, str) for item in warnings)
        ):
            continue
        answer = {
            "campaign_id": campaign_id,
            "operation": operation,
            "iteration": iteration,
            "draft_id": draft_id,
            "status": "SAVED",
            "blockers": blockers,
            "warnings": warnings,
        }
        return "FINAL ANSWER: " + json.dumps(
            answer,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return None


def install_glm_factory_terminal_boundary() -> None:
    """Stop the exact GLM task loop after the first successful P0 save receipt.

    The task still reaches Ouroboros' ordinary no-tool finalization path. The only
    synthesized value is the declared final-answer envelope, copied from the
    already-persisted server receipt; no content is generated, repaired or changed.
    """
    if canonical_runtime_models()["OUROBOROS_MODEL"] != RAILWAY_OPENROUTER_MODEL:
        return

    loop: Any = importlib.import_module("ouroboros.loop")
    current_handle = loop.handle_tool_calls
    current_call = loop.call_llm_with_retry
    handle_installed = bool(
        getattr(current_handle, "_communication_factory_terminal_boundary", False)
    )
    call_installed = bool(getattr(current_call, "_communication_factory_terminal_boundary", False))
    if handle_installed and call_installed:
        return
    if handle_installed or call_installed:
        raise RuntimeError("factory terminal-boundary installation is partial")

    handle_signature = inspect.signature(current_handle)
    call_signature = inspect.signature(current_call)
    if not {"task_id", "llm_trace", "emit_progress"}.issubset(handle_signature.parameters):
        raise RuntimeError("pinned tool-loop terminal-boundary contract drifted")
    if "task_id" not in call_signature.parameters:
        raise RuntimeError("pinned LLM-loop terminal-boundary contract drifted")

    def arm_terminal_answer(
        *,
        task_id: str,
        llm_trace: dict[str, Any],
        records: list[dict[str, Any]],
        emit_progress: Any,
        suppressed_tool_calls: int = 0,
    ) -> bool:
        answer = factory_saved_draft_final_answer(records)
        if answer is None:
            return False
        _FACTORY_TERMINAL_ANSWER.set((task_id, answer))
        boundary: dict[str, Any] = {
            "mode": "persisted_save_receipt",
            "tool": FACTORY_DRAFT_SAVE_TOOL,
        }
        if suppressed_tool_calls:
            boundary["suppressed_tool_calls"] = suppressed_tool_calls
        llm_trace["factory_terminal_boundary"] = boundary
        if callable(emit_progress):
            emit_progress("Factory draft persisted; finalizing the task.")
        return True

    @functools.wraps(current_handle)
    def terminal_handle(*args: Any, **kwargs: Any) -> int:
        bound = handle_signature.bind(*args, **kwargs)
        task_id = str(bound.arguments.get("task_id") or "")
        llm_trace = bound.arguments.get("llm_trace")
        if not task_id or not isinstance(llm_trace, dict):
            raise RuntimeError("factory terminal-boundary task context is invalid")
        tool_calls = bound.arguments.get("tool_calls")
        if not isinstance(tool_calls, list):
            raise RuntimeError("factory terminal-boundary tool-call batch is invalid")
        emit_progress = bound.arguments.get("emit_progress")

        # Pinned Ouroboros already executes mutative calls sequentially, but it
        # materializes every result before returning to this wrapper. Execute a
        # batch containing a save one call at a time so a confirmed persistence
        # receipt becomes a hard boundary for the unexecuted suffix of that same
        # assistant response. Batches without a save retain the byte-for-byte
        # upstream path, including its parallel-safe behavior.
        if len(tool_calls) > 1 and any(
            _requested_tool_name(tool_call) == FACTORY_DRAFT_SAVE_TOOL for tool_call in tool_calls
        ):
            error_count = 0
            for index, tool_call in enumerate(tool_calls):
                existing = llm_trace.get("tool_calls")
                start = len(existing) if isinstance(existing, list) else 0
                per_call = handle_signature.bind(*args, **kwargs)
                per_call.arguments["tool_calls"] = [tool_call]
                error_count += int(current_handle(*per_call.args, **per_call.kwargs))
                records = llm_trace.get("tool_calls")
                added = records[start:] if isinstance(records, list) else []
                if arm_terminal_answer(
                    task_id=task_id,
                    llm_trace=llm_trace,
                    records=[record for record in added if isinstance(record, dict)],
                    emit_progress=emit_progress,
                    suppressed_tool_calls=len(tool_calls) - index - 1,
                ):
                    break
            return error_count

        existing = llm_trace.get("tool_calls")
        start = len(existing) if isinstance(existing, list) else 0
        error_count = int(current_handle(*args, **kwargs))
        records = llm_trace.get("tool_calls")
        added = records[start:] if isinstance(records, list) else []
        arm_terminal_answer(
            task_id=task_id,
            llm_trace=llm_trace,
            records=[record for record in added if isinstance(record, dict)],
            emit_progress=emit_progress,
        )
        return error_count

    @functools.wraps(current_call)
    def terminal_call(*args: Any, **kwargs: Any) -> Any:
        bound = call_signature.bind_partial(*args, **kwargs)
        task_id = str(bound.arguments.get("task_id") or "")
        pending = _FACTORY_TERMINAL_ANSWER.get()
        if pending is not None:
            _FACTORY_TERMINAL_ANSWER.set(None)
            if task_id and pending[0] == task_id:
                return {"role": "assistant", "content": pending[1]}, 0.0
        return current_call(*args, **kwargs)

    terminal_handle._communication_factory_terminal_boundary = True  # type: ignore[attr-defined]
    terminal_call._communication_factory_terminal_boundary = True  # type: ignore[attr-defined]
    loop.handle_tool_calls = terminal_handle
    loop.call_llm_with_retry = terminal_call


def _usage_int(usage: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if value in (None, ""):
            continue
        try:
            return max(0, int(float(str(value))))
        except (TypeError, ValueError):
            continue
    return 0


def post_task_usage_event(
    *,
    category: str,
    task_id: str,
    model: str,
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    if category not in POST_TASK_CATEGORIES:
        raise ValueError("unsupported post-task usage category")
    resolved_model = str(usage.get("resolved_model") or model or "").strip()
    provider = str(usage.get("provider") or "").strip().lower()
    if not provider:
        if resolved_model.startswith("openai::"):
            provider = "openai"
        elif resolved_model.startswith("openrouter::"):
            provider = "openrouter"
        else:
            provider = "unknown"
    try:
        cost = max(0.0, float(str(usage.get("cost") or 0.0)))
    except (TypeError, ValueError):
        cost = 0.0
    return {
        "type": "llm_usage",
        "task_id": task_id,
        "category": category,
        "model": resolved_model,
        "provider": provider,
        "source": "communication_factory_runtime_adapter",
        "cost_estimated": bool(usage.get("cost_estimated", False)),
        "cost": cost,
        "prompt_tokens": _usage_int(usage, "prompt_tokens", "input_tokens"),
        "completion_tokens": _usage_int(usage, "completion_tokens", "output_tokens"),
        "cached_tokens": _usage_int(usage, "cached_tokens"),
        "cache_write_tokens": _usage_int(usage, "cache_write_tokens"),
    }


def install_post_task_usage_ledger() -> None:
    agent_task_pipeline: Any = importlib.import_module("ouroboros.agent_task_pipeline")
    post_task_evolution: Any = importlib.import_module("ouroboros.post_task_evolution")
    llm_module: Any = importlib.import_module("ouroboros.llm")
    utils: Any = importlib.import_module("ouroboros.utils")
    LLMClient = llm_module.LLMClient
    append_jsonl = utils.append_jsonl
    utc_now_iso = utils.utc_now_iso

    if getattr(LLMClient.chat, "_communication_factory_usage_guard", False):
        return

    original_chat = LLMClient.chat

    @functools.wraps(original_chat)
    def audited_chat(client: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_chat(client, *args, **kwargs)
        audit = _POST_TASK_AUDIT.get()
        if audit and isinstance(result, tuple) and len(result) == 2:
            category, task_id, drive_root = audit
            usage = result[1] if isinstance(result[1], dict) else {}
            event = post_task_usage_event(
                category=category,
                task_id=task_id,
                model=str(kwargs.get("model") or ""),
                usage=usage,
            )
            event["ts"] = utc_now_iso()
            append_jsonl(drive_root / "logs" / "events.jsonl", event)
        return result

    audited_chat._communication_factory_usage_guard = True  # type: ignore[attr-defined]
    LLMClient.chat = audited_chat

    original_summary = agent_task_pipeline._run_task_summary
    original_reflection = agent_task_pipeline._run_reflection
    original_evolution_decision = post_task_evolution._decide_promotion

    @functools.wraps(original_summary)
    def audited_summary(
        env: Any,
        llm: Any,
        task: dict[str, Any],
        usage: dict[str, Any],
        llm_trace: dict[str, Any],
        drive_logs: pathlib.Path,
        review_evidence: dict[str, Any] | None = None,
    ) -> Any:
        token = _POST_TASK_AUDIT.set(
            ("post_task_summary", str(task.get("id") or ""), pathlib.Path(env.drive_root))
        )
        try:
            return original_summary(
                env,
                llm,
                task,
                usage,
                llm_trace,
                drive_logs,
                review_evidence=review_evidence,
            )
        finally:
            _POST_TASK_AUDIT.reset(token)

    @functools.wraps(original_reflection)
    def audited_reflection(
        env: Any,
        llm: Any,
        task: dict[str, Any],
        usage: dict[str, Any],
        llm_trace: dict[str, Any],
        review_evidence: dict[str, Any],
    ) -> Any:
        token = _POST_TASK_AUDIT.set(
            ("post_task_reflection", str(task.get("id") or ""), pathlib.Path(env.drive_root))
        )
        try:
            return original_reflection(env, llm, task, usage, llm_trace, review_evidence)
        finally:
            _POST_TASK_AUDIT.reset(token)

    @functools.wraps(original_evolution_decision)
    def audited_evolution_decision(
        env: Any,
        task: dict[str, Any],
        reflection_entry: dict[str, Any] | None,
        llm_client: Any,
        *,
        force: bool,
    ) -> Any:
        token = _POST_TASK_AUDIT.set(
            (
                "post_task_evolution_decision",
                str(task.get("id") or ""),
                pathlib.Path(env.drive_root),
            )
        )
        try:
            return original_evolution_decision(
                env,
                task,
                reflection_entry,
                llm_client,
                force=force,
            )
        finally:
            _POST_TASK_AUDIT.reset(token)

    agent_task_pipeline._run_task_summary = audited_summary
    agent_task_pipeline._run_reflection = audited_reflection
    post_task_evolution._decide_promotion = audited_evolution_decision


def install_secret_persistence_guard() -> None:
    config: Any = importlib.import_module("ouroboros.config")

    original_save_settings = config.save_settings

    @functools.wraps(original_save_settings)
    def guarded_save_settings(settings: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        original_save_settings(settings_for_persistence(settings), *args, **kwargs)

    config.save_settings = guarded_save_settings

    server_runtime: Any = importlib.import_module("ouroboros.server_runtime")

    original_apply_provider_defaults = server_runtime.apply_runtime_provider_defaults

    @functools.wraps(original_apply_provider_defaults)
    def guarded_apply_provider_defaults(
        settings: dict[str, Any],
    ) -> tuple[dict[str, Any], bool, list[str]]:
        normalized, changed, changed_keys = original_apply_provider_defaults(settings)
        normalized, canonical_changed = settings_for_p0_runtime(normalized)
        all_changed = list(dict.fromkeys([*changed_keys, *canonical_changed]))
        return normalized, bool(changed or canonical_changed), all_changed

    server_runtime.apply_runtime_provider_defaults = guarded_apply_provider_defaults

    gateway_settings: Any = importlib.import_module("ouroboros.gateway.settings")

    original_owner_write_settings = gateway_settings._owner_write_settings

    @functools.wraps(original_owner_write_settings)
    def guarded_owner_write_settings(settings: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        original_owner_write_settings(settings_for_persistence(settings), *args, **kwargs)

    gateway_settings.save_settings = guarded_save_settings
    gateway_settings._owner_write_settings = guarded_owner_write_settings


def main() -> int:
    from strict_tool_adapter import install_strict_tool_adapter

    install_strict_tool_adapter()
    install_factory_tool_result_transport()
    install_secret_persistence_guard()
    install_profile_model_guard()
    install_glm_main_loop_output_cap()
    install_glm_safety_response_schema()
    install_openrouter_generation_audit()
    install_request_ledger_guard()
    install_glm_factory_terminal_boundary()
    install_post_task_usage_ledger()
    import server  # type: ignore[import-not-found]

    return int(server.main())


if __name__ == "__main__":
    raise SystemExit(main())
