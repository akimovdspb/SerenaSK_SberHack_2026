from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Literal

import httpx

ALLOWED_PROVIDER_TOOLS = [
    "mcp_factory__cf_context_get",
    "mcp_factory__cf_draft_save",
]
CONTRACT_MARKER = "COMMUNICATION_FACTORY_CONTRACT_V1"


class TaskAdmissionError(RuntimeError):
    pass


class TaskTransportFailure(StrEnum):
    CONNECT_TIMEOUT = "connect_timeout"
    READ_TIMEOUT = "read_timeout"
    WRITE_TIMEOUT = "write_timeout"
    POOL_TIMEOUT = "pool_timeout"
    CONNECTION_RESET = "connection_reset"
    HTTP_STATUS = "http_status"
    INVALID_RESPONSE = "invalid_response"
    OTHER = "other"


TaskTransportPhase = Literal["submit", "lookup", "list", "cancel", "events"]


class ManagedTaskTransportError(TaskAdmissionError):
    """Safe, typed Task API failure for orchestration decisions.

    The exception intentionally stores no response body or raw transport text.  A
    client-generated task id is the only admission correlation key used after an
    ambiguous submit.
    """

    def __init__(
        self,
        message: str,
        *,
        phase: TaskTransportPhase,
        failure: TaskTransportFailure,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        acceptance_ambiguous: bool = False,
        task_not_found: bool = False,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.failure = failure
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.acceptance_ambiguous = acceptance_ambiguous
        self.task_not_found = task_not_found


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse Retry-After without accepting negative, malformed, or unbounded values."""

    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        reference = now or datetime.now(UTC)
        seconds = (target.astimezone(UTC) - reference.astimezone(UTC)).total_seconds()
    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return None
    return seconds


def _typed_transport_error(
    exc: httpx.HTTPError,
    *,
    phase: TaskTransportPhase,
) -> ManagedTaskTransportError:
    failure = TaskTransportFailure.OTHER
    if isinstance(exc, httpx.ConnectTimeout):
        failure = TaskTransportFailure.CONNECT_TIMEOUT
    elif isinstance(exc, httpx.ReadTimeout):
        failure = TaskTransportFailure.READ_TIMEOUT
    elif isinstance(exc, httpx.WriteTimeout):
        failure = TaskTransportFailure.WRITE_TIMEOUT
    elif isinstance(exc, httpx.PoolTimeout):
        failure = TaskTransportFailure.POOL_TIMEOUT
    elif isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ),
    ):
        failure = TaskTransportFailure.CONNECTION_RESET

    status: int | None = None
    retry_after: float | None = None
    not_found = False
    if isinstance(exc, httpx.HTTPStatusError):
        failure = TaskTransportFailure.HTTP_STATUS
        status = exc.response.status_code
        retry_after = parse_retry_after(exc.response.headers.get("Retry-After"))
        not_found = phase == "lookup" and status == 404

    ambiguous_types = (
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
    )
    return ManagedTaskTransportError(
        "managed Task API transport failed",
        phase=phase,
        failure=failure,
        http_status=status,
        retry_after_seconds=retry_after,
        acceptance_ambiguous=phase == "submit" and isinstance(exc, ambiguous_types),
        task_not_found=not_found,
    )


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def dict_field(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        return {}
    return {str(item_key): item_value for item_key, item_value in value.items()}


def extract_skill_body(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskAdmissionError("mounted skill is not UTF-8") from exc
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise TaskAdmissionError("mounted skill front matter is malformed")
    body = parts[2].strip()
    if not body.startswith(f"{CONTRACT_MARKER}\n"):
        raise TaskAdmissionError("mounted skill contract marker is missing")
    return body


def extension_admission_projection(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    projection = [
        {
            "name": str(row.get("name") or ""),
            "type": str(row.get("type") or ""),
            "version": str(row.get("version") or ""),
            "enabled": bool(row.get("enabled")),
            "review_status": str(row.get("review_status") or ""),
            "review_stale": bool(row.get("review_stale")),
            "executable_review": bool(row.get("executable_review")),
            "load_error": bool(row.get("load_error")),
            "source": str(row.get("source") or ""),
        }
        for row in rows
    ]
    projection.sort(key=lambda item: str(item["name"]))
    return projection


def mcp_admission_projection(status: dict[str, Any]) -> dict[str, Any]:
    servers: list[dict[str, Any]] = []
    for raw in status.get("servers") or []:
        if not isinstance(raw, dict):
            continue
        tools = [
            {
                "name": str(item.get("name") or ""),
                "prefixed_name": str(item.get("prefixed_name") or ""),
            }
            for item in raw.get("tools") or []
            if isinstance(item, dict)
        ]
        tools.sort(key=lambda item: item["prefixed_name"])
        servers.append(
            {
                "id": str(raw.get("id") or ""),
                "name": str(raw.get("name") or ""),
                "enabled": bool(raw.get("enabled")),
                "transport": str(raw.get("transport") or ""),
                "url": str(raw.get("url") or ""),
                "auth_configured": bool(raw.get("auth_configured")),
                "last_error_present": bool(raw.get("last_error")),
                "tools": tools,
            }
        )
    servers.sort(key=lambda item: item["id"])
    return {
        "enabled": bool(status.get("enabled")),
        "sdk_available": bool(status.get("sdk_available")),
        "tool_timeout_sec": int(status.get("tool_timeout_sec") or 0),
        "servers": servers,
    }


@dataclass(frozen=True)
class TaskAdmission:
    constraints: str
    disabled_tools: list[str]
    prompt_hash: str
    skill_content_hash: str
    tool_inventory_hash: str
    activation_mode: str
    runtime_image_id: str


def build_campaign_task(
    *,
    task_id: str,
    run_id: str,
    campaign_id: str,
    operation: str,
    iteration: int,
    idempotency_key: str,
    context_version: str,
    project_id: str,
    admission: TaskAdmission,
    timeout_sec: int = 25,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "type": "task",
        "description": (
            f"Кампания {campaign_id}; операция {operation}; итерация {iteration}. "
            f"Ключ идемпотентности {idempotency_key}. "
            "Исполни обязательные инструкции задачи и верни итоговый JSON указанного формата."
        ),
        "context": (
            "Синтетическая задача без отправки; бизнес-данные доступны только через "
            "инструмент контекста."
        ),
        "expected_output": (
            "FINAL ANSWER: {campaign_id, operation, iteration, draft_id, status, "
            "blockers, warnings}"
        ),
        "constraints": admission.constraints,
        "disabled_tools": admission.disabled_tools,
        "allowed_resources": {"network": True},
        "answer_protocol": "final_answer_line",
        "context_requires_self_body_docs": False,
        "project_id": project_id,
        "memory_mode": "forked",
        "timeout_sec": timeout_sec,
        "source": "communication_factory_ui",
        "metadata": {
            "run_id": run_id,
            "campaign_id": campaign_id,
            "operation": operation,
            "iteration": iteration,
            "idempotency_key": idempotency_key,
            "context_version": context_version,
            "skill_content_hash": admission.skill_content_hash,
            "prompt_hash": admission.prompt_hash,
            "tool_inventory_hash": admission.tool_inventory_hash,
            "activation_mode": admission.activation_mode,
        },
    }


class OuroborosTaskAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        lock_path: pathlib.Path,
        skill_path: pathlib.Path,
        expected_identity_kind: str = "docker_image",
        expected_runtime_identity: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._client = client or httpx.Client(base_url=base_url, timeout=10.0)
        self._lock_path = lock_path
        self._skill_path = skill_path
        self._expected_identity_kind = expected_identity_kind
        self._expected_runtime_identity = expected_runtime_identity

    def _get_object(self, path: str) -> dict[str, Any]:
        try:
            response = self._client.get(path)
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise TaskAdmissionError("private Ouroboros readiness request failed") from exc
        if not isinstance(value, dict):
            raise TaskAdmissionError("private Ouroboros readiness response is invalid")
        return value

    def _load_lock(self) -> dict[str, Any]:
        try:
            value = json.loads(self._lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskAdmissionError("runtime contract lock is unavailable") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise TaskAdmissionError("runtime contract lock schema is invalid")
        return value

    def admit(self) -> TaskAdmission:
        lock = self._load_lock()
        try:
            skill_raw = self._skill_path.read_bytes()
        except OSError as exc:
            raise TaskAdmissionError("mounted skill payload is unavailable") from exc
        body = extract_skill_body(skill_raw)
        prompt_hash = hashlib.sha256(body.encode("utf-8") + b"\n").hexdigest()
        skill_lock = dict_field(lock, "skill")
        tools_lock = dict_field(lock, "tools")
        runtime_lock = dict_field(lock, "runtime")
        if (
            hashlib.sha256(skill_raw).hexdigest() != skill_lock.get("skill_file_sha256")
            or prompt_hash != skill_lock.get("prompt_hash")
            or skill_lock.get("activation_mode") != "adapter_injected"
            or not skill_lock.get("ready")
        ):
            raise TaskAdmissionError("mounted skill differs from the activation lock")

        state = self._get_object("/api/state")
        expected_profile = dict_field(runtime_lock, "expected_profile")
        if not expected_profile:
            raise TaskAdmissionError("runtime profile is absent from the contract lock")
        observed_profile = {
            "runtime_mode": state.get("runtime_mode"),
            "context_mode": state.get("context_mode"),
            "safety_mode": state.get("safety_mode"),
            "evolution_enabled": bool(state.get("evolution_enabled")),
            "background_enabled": bool(state.get("bg_consciousness_enabled")),
        }
        if (
            observed_profile != expected_profile
            or not state.get("supervisor_ready")
            or state.get("supervisor_error")
            or int(state.get("workers_alive") or 0) <= 0
            or int(state.get("workers_alive") or 0) != int(state.get("workers_total") or 0)
        ):
            raise TaskAdmissionError("runtime readiness or mode profile drifted")

        manifest = self._get_object("/api/extensions/communication_factory/manifest")
        manifest_body = dict_field(manifest, "manifest")
        extensions = self._get_object("/api/extensions")
        extension_rows: list[dict[str, Any]] = [
            {str(key): value for key, value in row.items()}
            for row in extensions.get("skills") or []
            if isinstance(row, dict)
        ]
        factory_row = next(
            (row for row in extension_rows if row.get("name") == "communication_factory"),
            {},
        )
        if (
            manifest.get("content_hash") != skill_lock.get("skill_content_hash")
            or manifest.get("load_error")
            or manifest_body.get("name") != "communication_factory"
            or manifest_body.get("version") != skill_lock.get("version")
            or manifest_body.get("type") != "instruction"
            or list(manifest_body.get("permissions") or []) != []
            or not factory_row.get("enabled")
            or not factory_row.get("executable_review")
            or factory_row.get("review_stale")
            or factory_row.get("review_status") != "clean"
            or not bool(dict_field(factory_row, "grants").get("all_granted"))
        ):
            raise TaskAdmissionError("instruction skill lifecycle drifted")
        extension_lock = dict_field(lock, "extensions")
        extension_projection = extension_admission_projection(extension_rows)
        if hash_json(extension_projection) != extension_lock.get("admission_hash") or [
            row["name"] for row in extension_projection
        ] != list(extension_lock.get("catalog_names") or []):
            raise TaskAdmissionError("extension catalog drifted")

        mcp_status = self._get_object("/api/mcp/status")
        mcp_projection = mcp_admission_projection(mcp_status)
        mcp_lock = dict_field(lock, "mcp")
        if hash_json(mcp_projection) != mcp_lock.get("admission_hash"):
            raise TaskAdmissionError("MCP settings or discovered tools drifted")

        effective_names = [str(item) for item in tools_lock.get("effective_tool_names") or []]
        disabled_tools = [str(item) for item in tools_lock.get("disabled_tools") or []]
        provider_probe = dict_field(lock, "provider_probe")
        expected_disabled = sorted(set(effective_names) - set(ALLOWED_PROVIDER_TOOLS))
        if (
            sorted(disabled_tools) != expected_disabled
            or list(tools_lock.get("post_deny_tool_names") or []) != ALLOWED_PROVIDER_TOOLS
            or list(provider_probe.get("provider_tool_names") or []) != ALLOWED_PROVIDER_TOOLS
            or not provider_probe.get("provider_tool_set_exact")
        ):
            raise TaskAdmissionError("provider tool capability lock is invalid")
        image_id = str(runtime_lock.get("image_id") or "")
        identity_kind = str(runtime_lock.get("identity_kind") or "docker_image")
        if (
            not image_id.startswith("sha256:")
            or identity_kind != self._expected_identity_kind
            or (self._expected_runtime_identity and image_id != self._expected_runtime_identity)
        ):
            raise TaskAdmissionError("runtime image identity is absent from the contract lock")
        inventory_hash = str(tools_lock.get("inventory_hash") or "")
        if len(inventory_hash) != 64:
            raise TaskAdmissionError("tool inventory hash is absent from the contract lock")
        return TaskAdmission(
            constraints=body,
            disabled_tools=disabled_tools,
            prompt_hash=prompt_hash,
            skill_content_hash=str(skill_lock.get("skill_content_hash") or ""),
            tool_inventory_hash=inventory_hash,
            activation_mode="adapter_injected",
            runtime_image_id=image_id,
        )

    def submit_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post("/api/tasks", json=payload, timeout=10.0)
            response.raise_for_status()
            value = response.json()
        except httpx.HTTPError as exc:
            raise _typed_transport_error(exc, phase="submit") from exc
        except json.JSONDecodeError as exc:
            raise ManagedTaskTransportError(
                "managed Task API admission response is invalid",
                phase="submit",
                failure=TaskTransportFailure.INVALID_RESPONSE,
                acceptance_ambiguous=True,
            ) from exc
        if not isinstance(value, dict) or not value.get("ok"):
            raise ManagedTaskTransportError(
                "managed Task API admission response is invalid",
                phase="submit",
                failure=TaskTransportFailure.INVALID_RESPONSE,
                acceptance_ambiguous=True,
            )
        return value

    def task(self, task_id: str) -> dict[str, Any]:
        try:
            response = self._client.get(f"/api/tasks/{task_id}")
            response.raise_for_status()
            value = response.json()
        except httpx.HTTPError as exc:
            raise _typed_transport_error(exc, phase="lookup") from exc
        except json.JSONDecodeError as exc:
            raise ManagedTaskTransportError(
                "managed Task API lookup response is invalid",
                phase="lookup",
                failure=TaskTransportFailure.INVALID_RESPONSE,
            ) from exc
        if not isinstance(value, dict):
            raise ManagedTaskTransportError(
                "managed Task API lookup response is invalid",
                phase="lookup",
                failure=TaskTransportFailure.INVALID_RESPONSE,
            )
        return value

    def state(self) -> dict[str, Any]:
        return self._get_object("/api/state")

    def tasks(self) -> dict[str, Any]:
        try:
            response = self._client.get("/api/tasks?limit=500")
            response.raise_for_status()
            value = response.json()
        except httpx.HTTPError as exc:
            raise _typed_transport_error(exc, phase="list") from exc
        except json.JSONDecodeError as exc:
            raise ManagedTaskTransportError(
                "managed Task API list response is invalid",
                phase="list",
                failure=TaskTransportFailure.INVALID_RESPONSE,
            ) from exc
        if not isinstance(value, dict):
            raise ManagedTaskTransportError(
                "managed Task API list response is invalid",
                phase="list",
                failure=TaskTransportFailure.INVALID_RESPONSE,
            )
        return value

    def cancel_task(self, task_id: str) -> None:
        try:
            response = self._client.post(f"/api/tasks/{task_id}/cancel", json={})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise _typed_transport_error(exc, phase="cancel") from exc

    def task_events_text(self, task_id: str) -> str:
        try:
            response = self._client.get(f"/api/tasks/{task_id}/events?cursor=0&wait=0")
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as exc:
            raise _typed_transport_error(exc, phase="events") from exc

    def close(self) -> None:
        self._client.close()
