from __future__ import annotations

import json
import time
from typing import Any, Literal

from apps.api.app.domain.campaigns import ContextBundle
from apps.api.app.domain.models import ContextGetRequest, DraftSaveRequest
from apps.api.app.mcp.service import FactoryMcpService
from apps.api.app.ouroboros_client import (
    ManagedTaskTransportError,
    TaskAdmission,
    TaskTransportFailure,
)
from apps.api.app.services.deterministic import build_deterministic_bundle

FaultProfile = Literal["transient_then_success", "transient_twice"]


class ControlledRetryFaultAdapter:
    """Providerless Task API double, available only through the APP_ENV=test gate."""

    def __init__(
        self,
        mcp: FactoryMcpService,
        *,
        profile: FaultProfile,
        provider: str,
        model: str,
        include_post_task_summary: bool,
    ) -> None:
        self._mcp = mcp
        self._profile = profile
        self._provider = provider
        self._model = model
        self._include_post_task_summary = include_post_task_summary
        self._submission_count: dict[str, int] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}

    def admit(self) -> TaskAdmission:
        return TaskAdmission(
            constraints=("COMMUNICATION_FACTORY_CONTRACT_V1\nПроверенный тестовый контракт."),  # noqa: RUF001
            disabled_tools=["run_command", "web_search"],
            prompt_hash="a" * 64,
            skill_content_hash="b" * 64,
            tool_inventory_hash="c" * 64,
            activation_mode="adapter_injected",
            runtime_image_id=f"sha256:{'d' * 64}",
        )

    def submit_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("fault adapter requires task metadata")
        run_id = str(metadata.get("run_id") or "")
        task_id = str(payload.get("task_id") or "")
        ordinal = self._submission_count.get(run_id, 0) + 1
        self._submission_count[run_id] = ordinal
        if ordinal == 1 or self._profile == "transient_twice":
            raise ManagedTaskTransportError(
                "synthetic transient Task API response",
                phase="submit",
                failure=TaskTransportFailure.HTTP_STATUS,
                http_status=503,
                retry_after_seconds=0.5,
            )

        # Keep the providerless UI on attempt two long enough to observe its durable status.
        time.sleep(0.3)

        context_result = self._mcp.context_get(
            ContextGetRequest(
                campaign_id=str(metadata["campaign_id"]),
                operation=str(metadata["operation"]),
                iteration=int(metadata["iteration"]),
                context_version=str(metadata["context_version"]),
                idempotency_key=str(metadata["idempotency_key"]),
            )
        )
        if context_result.context_bundle is None:
            raise RuntimeError("fault adapter context was not ready")
        context = ContextBundle.model_validate(context_result.context_bundle)
        bundle = build_deterministic_bundle(context)
        saved = self._mcp.draft_save(
            DraftSaveRequest.model_validate(
                {
                    "campaign_id": metadata["campaign_id"],
                    "operation": metadata["operation"],
                    "iteration": metadata["iteration"],
                    "context_version": metadata["context_version"],
                    "idempotency_key": metadata["idempotency_key"],
                    "draft": {
                        "kind": "communication_bundle",
                        "schema_version": "1.0",
                        "campaign_id": metadata["campaign_id"],
                        "operation": metadata["operation"],
                        "iteration": metadata["iteration"],
                        "context_version": metadata["context_version"],
                        "payload": bundle.model_dump(mode="json"),
                    },
                }
            )
        )
        if not saved.persisted:
            raise RuntimeError("fault adapter draft was not persisted")
        self._tasks[task_id] = {
            "status": "completed",
            "task_id": task_id,
            "final_answer": json.dumps({"status": "SAVED", "draft_id": saved.draft_id}),
        }
        events = [self._tool_event("mcp_factory__cf_context_get")]
        events.append(self._usage_event("task"))
        events.append(self._tool_event("mcp_factory__cf_draft_save"))
        if self._include_post_task_summary:
            events.append(self._usage_event("post_task_summary"))
        events.append({"type": "task_done", "data": {"status": "completed"}})
        self._events[task_id] = events
        return {"ok": True, "task_id": task_id}

    def task(self, task_id: str) -> dict[str, Any]:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise ManagedTaskTransportError(
                "synthetic task was not found",
                phase="lookup",
                failure=TaskTransportFailure.HTTP_STATUS,
                http_status=404,
                task_not_found=True,
            ) from exc

    def tasks(self) -> dict[str, Any]:
        return {"queue": {"running": [], "pending": []}}

    def cancel_task(self, task_id: str) -> None:
        if task_id in self._tasks:
            self._tasks[task_id] = {"status": "cancelled", "task_id": task_id}
            self._events[task_id] = [{"type": "task_done", "data": {"status": "cancelled"}}]

    def task_events_text(self, task_id: str) -> str:
        return "".join(f"data: {json.dumps(event)}\n\n" for event in self._events.get(task_id, []))

    @staticmethod
    def _tool_event(name: str) -> dict[str, Any]:
        return {
            "type": "tool_completed",
            "source": "tools",
            "data": {"tool": name, "ts": "2026-07-17T00:00:00+00:00", "args": {}},
        }

    def _usage_event(self, category: str) -> dict[str, Any]:
        return {
            "type": "llm_usage",
            "source": "events",
            "data": {
                "category": category,
                "provider": self._provider,
                "model": self._model,
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "cost": 0.0,
                "ts": "2026-07-17T00:00:00+00:00",
            },
        }
