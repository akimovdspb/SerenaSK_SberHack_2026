from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import time
from contextlib import suppress
from typing import Any

from apps.api.app.mcp.service import FactoryMcpService
from apps.api.app.ouroboros_client import OuroborosTaskAdapter, TaskAdmission
from apps.api.app.settings import get_settings
from provider_profiles import normalize_provider_model, provider_profile

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "rejected_duplicate"}
TASK_DEADLINE_SECONDS = 25
RUN_TERMINAL_DEADLINE_SECONDS = 29
LEDGER_CATEGORIES = (
    "main_generation",
    "safety",
    "post_task_summary",
    "post_task_reflection",
    "post_task_evolution_decision",
    "provider_retry",
)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def build_live_probe_task(
    *,
    run_id: str,
    prepared: dict[str, Any],
    admission: TaskAdmission,
    task_timeout_seconds: int = TASK_DEADLINE_SECONDS,
) -> dict[str, Any]:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:20]
    campaign_id = str(prepared["campaign_id"])
    return {
        "task_id": f"cfp_{digest}",
        "type": "task",
        "description": (
            f"Кампания {campaign_id}; операция initial; итерация 1. "
            f"Ключ идемпотентности {prepared['idempotency_key']}. "
            "Создай и сохрани один синтетический черновик по действующим ограничениям."
        ),
        "context": (
            "Синтетическая задача без отправки; бизнес-данные доступны только через "
            "инструмент контекста."
        ),
        "expected_output": "Один сохранённый DraftEnvelope и компактный FINAL ANSWER JSON.",
        "constraints": admission.constraints,
        "disabled_tools": admission.disabled_tools,
        "allowed_resources": {"network": True},
        "answer_protocol": "final_answer_line",
        "context_requires_self_body_docs": False,
        "project_id": f"cf_gate0_{digest}",
        "memory_mode": "empty",
        "timeout_sec": task_timeout_seconds,
        "source": "communication_factory_live_probe",
        "metadata": {
            "run_id": run_id,
            "campaign_id": campaign_id,
            "operation": "initial",
            "iteration": 1,
            "idempotency_key": prepared["idempotency_key"],
            "context_version": prepared["context_version"],
            "skill_content_hash": admission.skill_content_hash,
            "prompt_hash": admission.prompt_hash,
            "tool_inventory_hash": admission.tool_inventory_hash,
            "activation_mode": admission.activation_mode,
        },
    }


def parse_sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            value = json.loads(line.removeprefix("data: "))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append({str(key): item for key, item in value.items()})
    return events


def queue_contains_task(queue_payload: dict[str, Any], task_id: str) -> bool:
    queue_raw = queue_payload.get("queue")
    queue: dict[str, Any] = (
        {str(key): value for key, value in queue_raw.items()} if isinstance(queue_raw, dict) else {}
    )
    for lane in ("running", "pending"):
        for row in queue.get(lane) or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("id") or row.get("task_id") or "") == task_id:
                return True
            task_raw = row.get("task")
            task: dict[str, Any] = (
                {str(key): value for key, value in task_raw.items()}
                if isinstance(task_raw, dict)
                else {}
            )
            if str(task.get("id") or task.get("task_id") or "") == task_id:
                return True
    return False


def terminal_event_observed(events: list[dict[str, Any]], task_status: str) -> bool:
    """Confirm the pinned Task API terminal signal without requiring legacy task_done."""
    if task_status not in TERMINAL_STATUSES:
        return False
    for event in events:
        event_type = str(event.get("type") or "")
        data = _event_data(event)
        event_status = str(data.get("status") or "")
        if event_type in {"task_done", "task_result"} and event_status == task_status:
            return True
        if event_type == "task_terminal_timeout" and task_status in {"failed", "cancelled"}:
            return True
    return False


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("data")
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def provider_request_accounting(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Project safe physical-request anomalies without provider payload content."""
    headers: list[dict[str, Any]] = []
    terminals: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = str(event.get("type") or "")
        data = _event_data(event)
        call_id = str(data.get("provider_call_id") or "")
        category = _provider_category(data.get("category"))
        if event_type == "provider_request_headers":
            headers.append(
                {
                    "provider_call_id": call_id,
                    "category": category,
                    "generation_id": str(data.get("generation_id") or ""),
                    "status_code": int(data.get("status_code") or 0),
                    "estimated_prompt_tokens": max(
                        0, int(data.get("estimated_prompt_tokens") or 0)
                    ),
                    "configured_max_output_tokens": max(
                        0, int(data.get("configured_max_output_tokens") or 0)
                    ),
                    "prompt_estimation_method": str(data.get("prompt_estimation_method") or ""),
                }
            )
        elif event_type == "provider_request_terminal":
            terminals[call_id] = {
                "status": str(data.get("status") or ""),
                "usage_observed": data.get("usage_observed") is True,
                "generation_ids": sorted(
                    str(value) for value in data.get("generation_ids") or [] if value
                ),
                "error_type": str(data.get("error_type") or ""),
                "category": category,
                "estimated_prompt_tokens": max(0, int(data.get("estimated_prompt_tokens") or 0)),
                "configured_max_output_tokens": max(
                    0, int(data.get("configured_max_output_tokens") or 0)
                ),
                "prompt_estimation_method": str(data.get("prompt_estimation_method") or ""),
            }

    completed_generation_ids = {
        generation_id
        for terminal in terminals.values()
        if terminal["status"] == "completed" and terminal["usage_observed"] is True
        for generation_id in terminal["generation_ids"]
    }
    orphan_requests = [
        {
            key: row[key]
            for key in (
                "provider_call_id",
                "category",
                "generation_id",
                "status_code",
                "estimated_prompt_tokens",
                "configured_max_output_tokens",
                "prompt_estimation_method",
            )
        }
        for row in headers
        if row["generation_id"] and row["generation_id"] not in completed_generation_ids
    ]
    pre_generation_anomalies = [
        {
            "provider_call_id": row["provider_call_id"],
            "category": row["category"],
            "status_code": row["status_code"],
            "error_type": str(terminals.get(row["provider_call_id"], {}).get("error_type") or ""),
            "generation_id_present": False,
            "reserved_tokens": 0,
            "reserved_cost_usd": 0.0,
        }
        for row in headers
        if not row["generation_id"]
    ]
    header_call_ids = {str(row["provider_call_id"]) for row in headers}
    for call_id, terminal in terminals.items():
        if (
            call_id not in header_call_ids
            and terminal["status"] != "completed"
            and not terminal["generation_ids"]
        ):
            pre_generation_anomalies.append(
                {
                    "provider_call_id": call_id,
                    "category": terminal["category"],
                    "status_code": 0,
                    "error_type": terminal["error_type"],
                    "generation_id_present": False,
                    "reserved_tokens": 0,
                    "reserved_cost_usd": 0.0,
                }
            )
    return {
        "schema_version": 1,
        "orphan_requests": sorted(
            orphan_requests,
            key=lambda row: (str(row["category"]), str(row["generation_id"])),
        ),
        "pre_generation_anomalies": sorted(
            pre_generation_anomalies,
            key=lambda row: (
                str(row["category"]),
                str(row["provider_call_id"]),
                int(row["status_code"]),
            ),
        ),
    }


def account_terminal_task_without_provider_request(
    accounting: dict[str, Any],
    *,
    task: dict[str, Any],
    ledger: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Classify a terminal zero-request task failure without inventing provider usage."""
    orphans = [dict(row) for row in accounting.get("orphan_requests") or []]
    anomalies = [dict(row) for row in accounting.get("pre_generation_anomalies") or []]
    request_count = sum(int(row.get("provider_request_count") or 0) for row in ledger.values())
    status = str(task.get("status") or "")
    if status in {"failed", "cancelled"} and request_count == 0 and not orphans and not anomalies:
        anomalies.append(
            {
                "provider_call_id": "",
                "category": "main_generation",
                "status_code": 0,
                "error_type": str(task.get("reason_code") or f"task_{status}"),
                "generation_id_present": False,
                "reserved_tokens": 0,
                "reserved_cost_usd": 0.0,
                "source": "task_terminal_without_provider_request",
                "task_id": str(task.get("task_id") or ""),
            }
        )
    return {
        "schema_version": 1,
        "orphan_requests": orphans,
        "pre_generation_anomalies": sorted(
            anomalies,
            key=lambda row: (
                str(row.get("category") or ""),
                str(row.get("provider_call_id") or ""),
                int(row.get("status_code") or 0),
            ),
        ),
    }


def _provider_category(value: Any) -> str:
    category = str(value or "")
    return "main_generation" if category == "task" else category


def provider_call_ledger(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    ledger: dict[str, dict[str, Any]] = {
        category: {
            "call_count": 0,
            "provider_request_count": 0,
            "provider_request_completed_count": 0,
            "provider_request_ids": [],
            "provider_request_completed_ids": [],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "cost_usd": 0.0,
            "generation_ids": [],
            "terminal_generation_ids": [],
            "models": [],
            "providers": [],
            "timestamps": [],
        }
        for category in LEDGER_CATEGORIES
    }
    for event in events:
        event_type = str(event.get("type") or "")
        data = _event_data(event)
        if event_type == "llm_usage":
            raw_category = str(data.get("category") or "")
            if raw_category == "task":
                category = "main_generation"
            elif raw_category in ledger:
                category = raw_category
            else:
                continue
            row = ledger[category]
            row["call_count"] += 1
            row["prompt_tokens"] += int(data.get("prompt_tokens") or 0)
            row["completion_tokens"] += int(data.get("completion_tokens") or 0)
            row["cached_tokens"] += int(data.get("cached_tokens") or 0)
            row["cache_write_tokens"] += int(data.get("cache_write_tokens") or 0)
            row["cost_usd"] += float(data.get("cost") or 0.0)
            model = str(data.get("model") or "")
            provider = str(data.get("provider") or "")
            timestamp = str(data.get("ts") or event.get("ts") or "")
            if model and model not in row["models"]:
                row["models"].append(model)
            if provider and provider not in row["providers"]:
                row["providers"].append(provider)
            if timestamp:
                row["timestamps"].append(timestamp)
        elif event_type == "provider_request_headers":
            category = _provider_category(data.get("category"))
            if category not in ledger or category == "provider_retry":
                continue
            row = ledger[category]
            generation_id = str(data.get("generation_id") or "")
            if generation_id:
                row["provider_request_count"] += 1
                provider_call_id = str(data.get("provider_call_id") or "")
                if provider_call_id:
                    row["provider_request_ids"].append(provider_call_id)
                if generation_id not in row["generation_ids"]:
                    row["generation_ids"].append(generation_id)
        elif event_type == "provider_request_terminal":
            category = _provider_category(data.get("category"))
            if category not in ledger or category == "provider_retry":
                continue
            if data.get("status") == "completed" and data.get("usage_observed") is True:
                row = ledger[category]
                terminal_generation_ids = [
                    str(value) for value in data.get("generation_ids") or [] if value
                ]
                completed_count = len(terminal_generation_ids)
                row["provider_request_completed_count"] += completed_count
                provider_call_id = str(data.get("provider_call_id") or "")
                if provider_call_id:
                    row["provider_request_completed_ids"].extend(
                        [provider_call_id] * completed_count
                    )
                row["terminal_generation_ids"].extend(terminal_generation_ids)
        elif "retry" in event_type:
            row = ledger["provider_retry"]
            row["call_count"] += 1
            timestamp = str(data.get("ts") or event.get("ts") or "")
            if timestamp:
                row["timestamps"].append(timestamp)
    for row in ledger.values():
        row["cost_usd"] = round(float(row["cost_usd"]), 8)
        row["provider_request_ids"].sort()
        row["provider_request_completed_ids"].sort()
        row["generation_ids"].sort()
        row["terminal_generation_ids"].sort()
        row["models"].sort()
        row["providers"].sort()
    return ledger


def observed_tool_names(events: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[tuple[str, str, bytes]] = set()
    for event in events:
        if event.get("source") != "tools":
            continue
        data = _event_data(event)
        name = str(data.get("tool") or data.get("name") or "")
        signature = (
            name,
            str(data.get("ts") or event.get("ts") or ""),
            json.dumps(
                data.get("args") if isinstance(data.get("args"), dict) else {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        if name and signature not in seen:
            seen.add(signature)
            names.append(name)
    return names


def usage_is_complete(
    ledger: dict[str, dict[str, Any]],
    *,
    expected_provider: str = "openai",
    require_post_task_summary: bool = True,
) -> bool:
    if ledger["main_generation"]["call_count"] <= 0:
        return False
    if require_post_task_summary and ledger["post_task_summary"]["call_count"] <= 0:
        return False
    for category, row in ledger.items():
        if category == "provider_retry":
            continue
        provider_request_count = int(row.get("provider_request_count") or 0)
        correlation_required = expected_provider == "openrouter" and int(row["call_count"]) > 0
        if (provider_request_count or correlation_required) and (
            provider_request_count <= 0
            or int(row.get("provider_request_completed_count") or 0) != provider_request_count
            or int(row["call_count"]) != provider_request_count
            or len(row.get("provider_request_ids") or []) != provider_request_count
            or row.get("provider_request_ids") != row.get("provider_request_completed_ids")
            or len(row.get("generation_ids") or []) != provider_request_count
            or row.get("generation_ids") != row.get("terminal_generation_ids")
        ):
            return False
        if row["call_count"] == 0:
            continue
        if not row["models"] or row["providers"] != [expected_provider]:
            return False
        if (
            row["prompt_tokens"] <= 0
            or row["completion_tokens"] < 0
            or row["cached_tokens"] < 0
            or row["cache_write_tokens"] < 0
        ):
            return False
    return True


def _elapsed_ms(started: float, ended: float) -> int:
    return max(0, round((ended - started) * 1000))


def run_probe(run_id: str) -> dict[str, Any]:
    settings = get_settings()
    profile = provider_profile(settings.LIVE_PROVIDER_PROFILE)
    service = FactoryMcpService(settings.DATABASE_URL)
    service.initialize()
    prepared = service.prepare_live_probe(run_id)
    adapter = OuroborosTaskAdapter(
        base_url=settings.OUROBOROS_BASE_URL,
        lock_path=settings.CONTRACT_LOCK_PATH,
        skill_path=settings.SKILL_PATH,
    )
    admission = adapter.admit()
    task_payload = build_live_probe_task(
        run_id=run_id,
        prepared=prepared,
        admission=admission,
        task_timeout_seconds=profile.effective_task_timeout_seconds,
    )
    started = time.monotonic()
    created = adapter.submit_task(task_payload)
    task_created = utc_now()
    task_id = str(created.get("task_id") or "")
    result_observed_at = ""
    result_observed_mono = 0.0
    terminal_observed_at = ""
    terminal_observed_mono = 0.0
    released_at = ""
    released_mono = 0.0
    draft_at_result: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    deadline = started + profile.effective_terminal_deadline_seconds
    while time.monotonic() < deadline:
        result = adapter.task(task_id)
        now = time.monotonic()
        if not result_observed_at and str(result.get("status") or "") in TERMINAL_STATUSES:
            result_observed_at = utc_now()
            result_observed_mono = now
            draft_at_result = service.probe_snapshot(str(prepared["campaign_id"]))
        events = parse_sse_events(adapter.task_events_text(task_id))
        task_status = str(result.get("status") or "")
        has_terminal_event = terminal_event_observed(events, task_status)
        if has_terminal_event and not terminal_observed_at:
            terminal_observed_at = utc_now()
            terminal_observed_mono = now
        if has_terminal_event and not queue_contains_task(adapter.tasks(), task_id):
            released_at = utc_now()
            released_mono = time.monotonic()
            break
        time.sleep(0.25)
    else:
        with suppress(Exception):
            adapter.cancel_task(task_id)
        raise RuntimeError(
            "live probe did not release its worker within the selected profile deadline"
        )

    final_result = adapter.task(task_id)
    final_snapshot = service.probe_snapshot(str(prepared["campaign_id"]))
    draft_before = (draft_at_result or {}).get("draft")
    draft_after = final_snapshot.get("draft")
    draft_before_hash = (
        str(draft_before.get("draft_hash") or "") if isinstance(draft_before, dict) else ""
    )
    draft_after_hash = (
        str(draft_after.get("draft_hash") or "") if isinstance(draft_after, dict) else ""
    )
    audit_events = final_snapshot.get("events") or []
    audit_types = [str(item.get("event_type") or "") for item in audit_events]
    ledger = provider_call_ledger(events)
    tool_names = observed_tool_names(events)
    all_providers = sorted(
        {provider for row in ledger.values() for provider in row["providers"] if provider}
    )
    timestamps = {
        "task_created": task_created,
        "context_tool_completed": next(
            (
                str(item.get("completed_at") or "")
                for item in audit_events
                if item.get("event_type") == "context_tool_completed"
            ),
            "",
        ),
        "draft_saved": next(
            (
                str(item.get("completed_at") or "")
                for item in audit_events
                if item.get("event_type") == "draft_saved"
            ),
            "",
        ),
        "task_result_persisted": result_observed_at,
        "task_terminal": terminal_observed_at,
        "worker_released": released_at,
    }
    observed_models = sorted(
        {
            normalize_provider_model(str(model))
            for row in ledger.values()
            for model in row["models"]
            if model
        }
    )
    checks = {
        "task_completed": final_result.get("status") == "completed",
        "context_completed_once": audit_types.count("context_tool_completed") == 1,
        "draft_saved_once": audit_types.count("draft_saved") == 1,
        "exact_two_tool_receipts": sorted(tool_names)
        == ["mcp_factory__cf_context_get", "mcp_factory__cf_draft_save"],
        "all_timestamps_present": all(timestamps.values()),
        "user_visible_under_30s": _elapsed_ms(started, result_observed_mono) < 30_000,
        "worker_released_under_30s": _elapsed_ms(started, released_mono) < 30_000,
        "draft_hash_unchanged_after_persistence": bool(draft_before_hash)
        and draft_before_hash == draft_after_hash,
        "usage_complete": usage_is_complete(
            ledger,
            expected_provider=profile.ledger_provider,
            require_post_task_summary=profile.require_post_task_summary,
        ),
        "provider_route_unchanged": all_providers == [profile.ledger_provider],
        "model_route_unchanged": observed_models == [profile.normalized_model],
        "provider_retry_absent": ledger["provider_retry"]["call_count"] == 0,
        "project_isolated": final_result.get("project_id") == task_payload["project_id"]
        and final_result.get("memory_mode") == "empty",
    }
    canonical_latency_passed = bool(
        checks["user_visible_under_30s"] and checks["worker_released_under_30s"]
    )
    functional_quality_passed = all(
        value
        for name, value in checks.items()
        if name not in {"user_visible_under_30s", "worker_released_under_30s"}
    )
    ok = functional_quality_passed and (
        canonical_latency_passed or profile.functional_latency_gap_allowed
    )
    task_report = {
        "task_id": task_id,
        "campaign_id": prepared["campaign_id"],
        "operation": "initial",
        "iteration": 1,
        "project_id": task_payload["project_id"],
        "memory_mode": "empty",
        "status": final_result.get("status"),
        "reason_code": final_result.get("reason_code"),
        "final_answer": final_result.get("final_answer"),
        "total_rounds": int(final_result.get("total_rounds") or 0),
    }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "case_id": "B01",
        "provider_profile": profile.name,
        "ok": ok,
        "functional_quality_passed": functional_quality_passed,
        "canonical_latency_passed": canonical_latency_passed,
        "latency_gap": functional_quality_passed and not canonical_latency_passed,
        "runtime_image_id": admission.runtime_image_id,
        "activation": {
            "prompt_hash": admission.prompt_hash,
            "skill_content_hash": admission.skill_content_hash,
            "disabled_tool_count": len(admission.disabled_tools),
        },
        "task": task_report,
        "timestamps": timestamps,
        "latency_ms": {
            "user_visible": _elapsed_ms(started, result_observed_mono),
            "task_result_to_terminal": _elapsed_ms(result_observed_mono, terminal_observed_mono),
            "full_worker_occupancy": _elapsed_ms(started, released_mono),
        },
        "provider_call_ledger": ledger,
        "provider_accounting": account_terminal_task_without_provider_request(
            provider_request_accounting(events),
            task=task_report,
            ledger=ledger,
        ),
        "tool_receipts": tool_names,
        "draft": draft_after,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        report = run_probe(args.run_id)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
