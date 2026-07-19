from __future__ import annotations

import json
from typing import Any

from apps.api.app.live_probe_transport import (
    account_terminal_task_without_provider_request,
    build_live_probe_task,
    parse_sse_events,
    provider_call_ledger,
    provider_request_accounting,
    queue_contains_task,
    terminal_event_observed,
    usage_is_complete,
)
from apps.api.app.ouroboros_client import TaskAdmission


def test_live_probe_task_uses_exact_constraints_and_locked_denylist() -> None:
    body = "COMMUNICATION_FACTORY_CONTRACT_V1\n\n# Контракт"
    payload = build_live_probe_task(
        run_id="gate0-live-01",
        prepared={
            "campaign_id": "cmp_gate0_123",
            "idempotency_key": "gate0-live-probe-123456",
            "context_version": "a" * 64,
        },
        admission=TaskAdmission(
            constraints=body,
            disabled_tools=["run_command", "web_search"],
            prompt_hash="b" * 64,
            skill_content_hash="c" * 64,
            tool_inventory_hash="e" * 64,
            activation_mode="adapter_injected",
            runtime_image_id=f"sha256:{'d' * 64}",
        ),
    )

    assert payload["constraints"] == body
    assert payload["disabled_tools"] == ["run_command", "web_search"]
    assert "COMMUNICATION_FACTORY_CONTRACT_V1" not in payload["description"]
    assert "gate0-live-probe-123456" in payload["description"]
    assert payload["metadata"]["tool_inventory_hash"] == "e" * 64
    assert payload["metadata"]["activation_mode"] == "adapter_injected"
    assert payload["timeout_sec"] == 25
    assert payload["memory_mode"] == "empty"
    assert payload["answer_protocol"] == "final_answer_line"


def test_live_probe_ledger_separates_main_summary_safety_and_retry() -> None:
    def usage(category: str, tokens: int) -> dict[str, Any]:
        return {
            "type": "llm_usage",
            "source": "events",
            "data": {
                "type": "llm_usage",
                "category": category,
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "prompt_tokens": tokens,
                "completion_tokens": 10,
                "cached_tokens": 7,
                "cache_write_tokens": 3,
                "cost": 0.001,
                "ts": "2026-07-11T00:00:00+00:00",
            },
        }

    events: list[dict[str, Any]] = [
        usage("task", 100),
        usage("task", 110),
        usage("safety", 20),
        usage("post_task_summary", 30),
        {"type": "llm_retry_deadline_exhausted", "data": {"ts": "later"}},
    ]

    ledger = provider_call_ledger(events)

    assert ledger["main_generation"]["call_count"] == 2
    assert ledger["main_generation"]["cached_tokens"] == 14
    assert ledger["main_generation"]["cache_write_tokens"] == 6
    assert ledger["safety"]["call_count"] == 1
    assert ledger["post_task_summary"]["call_count"] == 1
    assert ledger["post_task_reflection"]["call_count"] == 0
    assert ledger["provider_retry"]["call_count"] == 1
    assert usage_is_complete(ledger)


def test_usage_completeness_honors_explicit_openrouter_profile_without_summary() -> None:
    def usage(category: str, tokens: int) -> dict[str, Any]:
        return {
            "type": "llm_usage",
            "source": "events",
            "data": {
                "type": "llm_usage",
                "category": category,
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "prompt_tokens": tokens,
                "completion_tokens": 10,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "cost": 0.001,
                "ts": "2026-07-14T00:00:00+00:00",
            },
        }

    def correlation(category: str, suffix: str) -> list[dict[str, Any]]:
        call_id = f"cf_provider_{suffix}"
        generation_id = f"gen-{suffix}-12345678"
        return [
            {
                "type": "provider_request_headers",
                "data": {
                    "category": category,
                    "provider_call_id": call_id,
                    "generation_id": generation_id,
                },
            },
            {
                "type": "provider_request_terminal",
                "data": {
                    "category": category,
                    "provider_call_id": call_id,
                    "generation_ids": [generation_id],
                    "status": "completed",
                    "usage_observed": True,
                    "physical_response_count": 1,
                },
            },
        ]

    uncorrelated_ledger = provider_call_ledger([usage("task", 100), usage("safety", 20)])
    assert not usage_is_complete(
        uncorrelated_ledger,
        expected_provider="openrouter",
        require_post_task_summary=False,
    )
    ledger = provider_call_ledger(
        [
            *correlation("main_generation", "main"),
            usage("task", 100),
            *correlation("safety", "safety"),
            usage("safety", 20),
        ]
    )

    assert usage_is_complete(
        ledger,
        expected_provider="openrouter",
        require_post_task_summary=False,
    )

    assert not usage_is_complete(ledger)
    assert not usage_is_complete(
        ledger,
        expected_provider="openai",
        require_post_task_summary=False,
    )
    assert not usage_is_complete(
        ledger,
        expected_provider="openrouter",
        require_post_task_summary=True,
    )


def test_usage_completeness_matches_each_provider_header_to_terminal_usage() -> None:
    def event(event_type: str, **data: Any) -> dict[str, Any]:
        return {"type": event_type, "source": "events", "data": data}

    base_events = [
        event(
            "provider_request_headers",
            category="main_generation",
            provider_call_id="cf_provider_main",
            generation_id="gen-main-12345678",
        ),
        event(
            "provider_request_terminal",
            category="main_generation",
            provider_call_id="cf_provider_main",
            generation_ids=["gen-main-12345678"],
            status="completed",
            usage_observed=True,
            physical_response_count=1,
        ),
        event(
            "llm_usage",
            category="task",
            provider="openrouter",
            model="z-ai/glm-5.2",
            prompt_tokens=100,
            completion_tokens=10,
            cached_tokens=0,
            cache_write_tokens=0,
            cost=0.001,
        ),
    ]

    ledger = provider_call_ledger(base_events)
    assert ledger["main_generation"]["provider_request_count"] == 1
    assert ledger["main_generation"]["provider_request_completed_count"] == 1
    assert ledger["main_generation"]["generation_ids"] == ["gen-main-12345678"]
    assert usage_is_complete(
        ledger,
        expected_provider="openrouter",
        require_post_task_summary=False,
    )

    mismatched_call_id = provider_call_ledger(
        [
            base_events[0],
            event(
                "provider_request_terminal",
                category="main_generation",
                provider_call_id="cf_provider_other",
                generation_ids=["gen-main-12345678"],
                status="completed",
                usage_observed=True,
                physical_response_count=1,
            ),
            base_events[2],
        ]
    )
    assert not usage_is_complete(
        mismatched_call_id,
        expected_provider="openrouter",
        require_post_task_summary=False,
    )

    mismatched_generation_id = provider_call_ledger(
        [
            base_events[0],
            event(
                "provider_request_terminal",
                category="main_generation",
                provider_call_id="cf_provider_main",
                generation_ids=["gen-other-12345678"],
                status="completed",
                usage_observed=True,
                physical_response_count=1,
            ),
            base_events[2],
        ]
    )
    assert not usage_is_complete(
        mismatched_generation_id,
        expected_provider="openrouter",
        require_post_task_summary=False,
    )

    orphaned_main = provider_call_ledger(
        [
            *base_events,
            event(
                "provider_request_headers",
                category="main_generation",
                provider_call_id="cf_provider_orphan",
                generation_id="gen-orphan-12345678",
            ),
        ]
    )
    assert not usage_is_complete(
        orphaned_main,
        expected_provider="openrouter",
        require_post_task_summary=False,
    )

    orphaned_safety = provider_call_ledger(
        [
            *base_events,
            event(
                "provider_request_headers",
                category="safety",
                provider_call_id="cf_provider_safety",
                generation_id="gen-safety-12345678",
            ),
        ]
    )
    assert not usage_is_complete(
        orphaned_safety,
        expected_provider="openrouter",
        require_post_task_summary=False,
    )


def test_missing_generation_id_keeps_provider_usage_incomplete() -> None:
    events = [
        {
            "type": "provider_request_headers",
            "data": {"category": "main_generation", "generation_id": None},
        },
        {
            "type": "provider_request_terminal",
            "data": {
                "category": "main_generation",
                "provider_call_id": "cf_provider_missing_id",
                "generation_ids": [],
                "status": "completed",
                "usage_observed": True,
                "physical_response_count": 1,
            },
        },
        {
            "type": "llm_usage",
            "data": {
                "category": "task",
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "prompt_tokens": 100,
                "completion_tokens": 10,
            },
        },
    ]

    assert not usage_is_complete(
        provider_call_ledger(events),
        expected_provider="openrouter",
        require_post_task_summary=False,
    )


def test_no_generation_429_is_anomaly_but_does_not_poison_later_exact_usage() -> None:
    events = [
        {
            "type": "provider_request_headers",
            "data": {
                "category": "main_generation",
                "provider_call_id": "cf_provider_main",
                "generation_id": None,
                "status_code": 429,
                "estimated_prompt_tokens": 500,
                "configured_max_output_tokens": 10_240,
            },
        },
        {
            "type": "provider_request_headers",
            "data": {
                "category": "main_generation",
                "provider_call_id": "cf_provider_main",
                "generation_id": "gen-success-12345678",
                "status_code": 200,
                "estimated_prompt_tokens": 500,
                "configured_max_output_tokens": 10_240,
            },
        },
        {
            "type": "provider_request_terminal",
            "data": {
                "category": "main_generation",
                "provider_call_id": "cf_provider_main",
                "generation_ids": ["gen-success-12345678"],
                "status": "completed",
                "usage_observed": True,
                "physical_response_count": 2,
            },
        },
        {
            "type": "llm_usage",
            "data": {
                "category": "task",
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "cost": 0.001,
            },
        },
    ]

    ledger = provider_call_ledger(events)
    accounting = provider_request_accounting(events)
    assert usage_is_complete(
        ledger,
        expected_provider="openrouter",
        require_post_task_summary=False,
    )
    assert accounting["orphan_requests"] == []
    assert accounting["pre_generation_anomalies"] == [
        {
            "provider_call_id": "cf_provider_main",
            "category": "main_generation",
            "status_code": 429,
            "error_type": "",
            "generation_id_present": False,
            "reserved_tokens": 0,
            "reserved_cost_usd": 0.0,
        }
    ]


def test_orphan_accounting_preserves_only_safe_bounded_request_metadata() -> None:
    events = [
        {
            "type": "provider_request_headers",
            "data": {
                "category": "task",
                "provider_call_id": "cf_provider_orphan",
                "generation_id": "gen-orphan-12345678",
                "status_code": 200,
                "estimated_prompt_tokens": 12_345,
                "configured_max_output_tokens": 10_240,
                "prompt_estimation_method": "utf8_request_bytes_upper_bound_v1",
                "prompt": "must-not-project",
            },
        },
        {
            "type": "provider_request_terminal",
            "data": {
                "category": "task",
                "provider_call_id": "cf_provider_orphan",
                "generation_ids": ["gen-orphan-12345678"],
                "status": "failed",
                "usage_observed": False,
                "error_type": "TimeoutError",
            },
        },
    ]

    accounting = provider_request_accounting(events)
    assert accounting["pre_generation_anomalies"] == []
    assert accounting["orphan_requests"] == [
        {
            "provider_call_id": "cf_provider_orphan",
            "category": "main_generation",
            "generation_id": "gen-orphan-12345678",
            "status_code": 200,
            "estimated_prompt_tokens": 12_345,
            "configured_max_output_tokens": 10_240,
            "prompt_estimation_method": "utf8_request_bytes_upper_bound_v1",
        }
    ]
    assert "must-not-project" not in json.dumps(accounting)


def test_terminal_task_without_provider_request_is_zero_reservation_anomaly() -> None:
    accounting = account_terminal_task_without_provider_request(
        provider_request_accounting([]),
        task={
            "task_id": "task_zero_request_deadline",
            "status": "failed",
            "reason_code": "deadline",
        },
        ledger=provider_call_ledger([]),
    )

    assert accounting["orphan_requests"] == []
    assert accounting["pre_generation_anomalies"] == [
        {
            "provider_call_id": "",
            "category": "main_generation",
            "status_code": 0,
            "error_type": "deadline",
            "generation_id_present": False,
            "reserved_tokens": 0,
            "reserved_cost_usd": 0.0,
            "source": "task_terminal_without_provider_request",
            "task_id": "task_zero_request_deadline",
        }
    ]


def test_sse_and_queue_parsers_fail_closed_on_irrelevant_rows() -> None:
    event = {"type": "task_done", "task_id": "cfp_1", "data": {"status": "completed"}}
    stream = f"id: 1\nevent: task_event\ndata: {json.dumps(event)}\n\n: heartbeat\n"

    assert parse_sse_events(stream) == [event]
    assert queue_contains_task(
        {"queue": {"running": [{"task": {"id": "cfp_1"}}], "pending": []}},
        "cfp_1",
    )
    assert not queue_contains_task(
        {"queue": {"running": [{"task": {"id": "different"}}], "pending": []}},
        "cfp_1",
    )


def test_pinned_task_result_is_a_terminal_event_only_when_status_matches() -> None:
    events = [
        {
            "type": "task_result",
            "source": "task_result",
            "data": {"status": "completed"},
        }
    ]

    assert terminal_event_observed(events, "completed")
    assert not terminal_event_observed(events, "running")
    assert not terminal_event_observed(events, "failed")


def test_tool_receipts_deduplicate_parent_child_log_copies() -> None:
    from apps.api.app.live_probe_transport import observed_tool_names

    data = {
        "tool": "mcp_factory__cf_context_get",
        "ts": "2026-07-11T00:00:00+00:00",
        "args": {"campaign_id": "cmp_1"},
    }
    events = [
        {"source": "tools", "root": "/parent", "data": data},
        {"source": "tools", "root": "/child", "data": data},
    ]

    assert observed_tool_names(events) == ["mcp_factory__cf_context_get"]
