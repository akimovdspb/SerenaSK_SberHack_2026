from __future__ import annotations

import hashlib
import json
import os
import pathlib
import queue
import re
import tempfile
from datetime import UTC, datetime
from typing import Any, cast, get_type_hints

from provider_profiles import CANONICAL_PROFILE_NAME, requested_provider_profile

ALLOWED_TOOL_NAMES = {
    "mcp_factory__cf_context_get",
    "mcp_factory__cf_draft_save",
}
MARKER = "COMMUNICATION_FACTORY_CONTRACT_V1"
SKILL_PATH = pathlib.Path("/skills/communication_factory/SKILL.md")
RUNTIME_LOCK_PATH = pathlib.Path("/opt/communication-factory/ouroboros.lock")
VERSION_PATH = pathlib.Path("/opt/ouroboros/VERSION")
RUNTIME_CONTEXT_HEADER = "## Runtime context\n\n"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def schema_name(schema: dict[str, Any]) -> str:
    return str((schema.get("function") or {}).get("name") or "").strip()


def message_content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content or "")


def decoded_runtime_context(system_text: str) -> dict[str, Any]:
    """Decode the authoritative runtime JSON embedded in the system message."""
    if system_text.count(RUNTIME_CONTEXT_HEADER) != 1:
        raise RuntimeError("first provider system message must contain one runtime context")
    encoded = system_text.split(RUNTIME_CONTEXT_HEADER, 1)[1].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(encoded)
    except json.JSONDecodeError as exc:
        raise RuntimeError("first provider runtime context is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("first provider runtime context must be an object")
    return cast(dict[str, Any], value)


def redacted_mcp_settings(settings: dict[str, Any]) -> dict[str, Any]:
    servers: list[dict[str, Any]] = []
    for raw in settings.get("MCP_SERVERS") or []:
        if not isinstance(raw, dict):
            continue
        servers.append(
            {
                "id": str(raw.get("id") or ""),
                "name": str(raw.get("name") or ""),
                "enabled": bool(raw.get("enabled")),
                "transport": str(raw.get("transport") or ""),
                "url": str(raw.get("url") or ""),
                "auth_header": str(raw.get("auth_header") or ""),
                "auth_configured": bool(str(raw.get("auth_token") or "").strip()),
                "allowed_tools": sorted(str(item) for item in raw.get("allowed_tools") or []),
            }
        )
    return {
        "MCP_ENABLED": bool(settings.get("MCP_ENABLED")),
        "MCP_TOOL_TIMEOUT_SEC": int(settings.get("MCP_TOOL_TIMEOUT_SEC") or 0),
        "MCP_SERVERS": servers,
    }


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


def load_skill_contract(drive_root: pathlib.Path) -> tuple[dict[str, Any], str]:
    from ouroboros.skill_loader import (  # type: ignore[import-not-found]
        compute_content_hash,
        find_skill,
    )
    from ouroboros.skill_readiness import (  # type: ignore[import-not-found]
        skill_readiness_for_execution,
    )

    files = sorted(path.name for path in SKILL_PATH.parent.iterdir() if path.is_file())
    if files != ["SKILL.md"]:
        raise RuntimeError("instruction skill directory must contain only SKILL.md")
    skill = find_skill(drive_root, "communication_factory", repo_path="/skills")
    if skill is None:
        raise RuntimeError("communication_factory skill was not discovered")
    if skill.load_error:
        raise RuntimeError("communication_factory skill has a load error")
    if skill.manifest.type != "instruction" or skill.manifest.version != "1.0.0":
        raise RuntimeError("communication_factory manifest identity does not match")
    if not skill.manifest.body.startswith(f"{MARKER}\n"):
        raise RuntimeError("communication_factory marker is missing from the skill body")
    body_bytes = skill.manifest.body.strip().encode("utf-8") + b"\n"
    projection_path = pathlib.Path(
        os.environ.get(
            "CONTRACT_PROJECTION_PATH",
            "/projection/communication_factory.ru.md",
        )
    )
    if projection_path.read_bytes() != body_bytes:
        raise RuntimeError("generated prompt projection differs from reviewed skill body")
    official_hash = compute_content_hash(SKILL_PATH.parent)
    if official_hash != skill.content_hash:
        raise RuntimeError("official skill content hash is inconsistent")
    readiness = skill_readiness_for_execution(drive_root, skill)
    if not readiness.ready:
        categories = sorted(
            {
                str(blocker).split(":", 1)[0]
                for blocker in readiness.blockers
                if str(blocker).strip()
            }
        )
        raise RuntimeError(f"skill lifecycle is not ready: {','.join(categories)}")
    gate = readiness.review_gate
    record = {
        "name": skill.name,
        "version": skill.manifest.version,
        "type": skill.manifest.type,
        "source": skill.source,
        "skill_file_sha256": sha256_bytes(SKILL_PATH.read_bytes()),
        "skill_content_hash": skill.content_hash,
        "prompt_hash": sha256_bytes(body_bytes),
        "activation_mode": "adapter_injected",
        "enabled": skill.enabled,
        "review_status": skill.review.status,
        "review_stale": skill.review.is_stale_for(skill.content_hash),
        "executable_review": bool(gate.get("executable_review")),
        "review_profile": skill.review.review_profile,
        "grants_all_granted": bool(readiness.grant_status.get("all_granted", False)),
        "ready": readiness.ready,
        "marker_present": True,
        "projection_byte_equal": True,
    }
    return record, skill.manifest.body.strip()


def extension_catalog(drive_root: pathlib.Path) -> tuple[list[dict[str, Any]], str]:
    from ouroboros.skill_loader import discover_skills
    from ouroboros.skill_readiness import skill_readiness_for_execution

    rows: list[dict[str, Any]] = []
    live_extension_names: list[str] = []
    for skill in discover_skills(drive_root, repo_path="/skills"):
        readiness = skill_readiness_for_execution(drive_root, skill)
        row = {
            "name": skill.name,
            "type": skill.manifest.type,
            "version": skill.manifest.version,
            "content_hash": skill.content_hash,
            "enabled": skill.enabled,
            "review_status": skill.review.status,
            "review_stale": skill.review.is_stale_for(skill.content_hash),
            "executable_review": bool(readiness.review_gate.get("executable_review")),
            "ready": readiness.ready,
            "load_error": bool(skill.load_error),
            "source": skill.source,
        }
        rows.append(row)
        if skill.manifest.is_extension() and readiness.ready:
            live_extension_names.append(skill.name)
    if live_extension_names:
        raise RuntimeError("production profile must not have live extension skills")
    rows.sort(key=lambda item: str(item["name"]))
    return rows, hash_json(rows)


class CaptureLlm:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def default_model() -> str:
        return str(os.environ.get("OUROBOROS_MODEL") or "openai::gpt-5.4-mini")

    def chat(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        self.calls.append(kwargs)
        return (
            {
                "content": (
                    'FINAL ANSWER: {"campaign_id":"cmp_contract_probe",'
                    '"operation":"initial","iteration":1,"draft_id":"probe",'
                    '"status":"NO_FORWARD_PROBE","blockers":[],"warnings":[]}'
                ),
                "tool_calls": [],
            },
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "cost": 0.0,
            },
        )


def provider_seam_probe(
    *,
    registry: Any,
    body: str,
    disabled_tools: list[str],
    drive_root: pathlib.Path,
) -> dict[str, Any]:
    import ouroboros.agent as agent_module  # type: ignore[import-not-found]
    from ouroboros.agent import Env, OuroborosAgent
    from ouroboros.loop import run_llm_loop  # type: ignore[import-not-found]
    from strict_tool_adapter import hash_json as strict_hash_json
    from strict_tool_adapter import strict_function_tool_issues

    from ouroboros import pricing  # type: ignore[attr-defined]

    agent_module._worker_boot_logged = True
    agent = OuroborosAgent(Env(repo_dir=pathlib.Path("/opt/ouroboros"), drive_root=drive_root))
    agent.tools = registry
    capture = CaptureLlm()
    agent.llm = capture
    task = {
        "id": "contract_probe_task",
        "type": "task",
        "description": "Кампания cmp_contract_probe; операция initial; итерация 1.",
        "context": "Синтетическая задача без отправки; данные доступны только через контекст.",
        "expected_output": "FINAL ANSWER: компактный JSON результата операции.",
        "constraints": body,
        "disabled_tools": disabled_tools,
        "allowed_resources": {"network": True},
        "answer_protocol": "final_answer_line",
        "project_id": "campaign_contract_probe",
        "memory_mode": "forked",
        "timeout_sec": 25,
        "source": "communication_factory_contract_probe",
        "metadata": {
            "campaign_id": "cmp_contract_probe",
            "operation": "initial",
            "iteration": 1,
            "idempotency_key": "contract-probe-idempotency-0001",
        },
    }
    agent._current_task_type = "task"
    agent._current_task_id = "contract_probe_task"
    ctx, messages, _ = agent._prepare_task_context(task)
    original_get_pricing = pricing.get_pricing

    def static_pricing(*, allow_live_fetch: bool = True) -> dict[str, tuple[float, ...]]:
        del allow_live_fetch
        return cast(dict[str, tuple[float, ...]], original_get_pricing(allow_live_fetch=False))

    pricing.get_pricing = static_pricing
    try:
        run_llm_loop(
            messages=messages,
            tools=agent.tools,
            llm=capture,
            drive_logs=drive_root / "logs",
            emit_progress=lambda _: None,
            incoming_messages=queue.Queue(),
            task_type="task",
            task_id="contract_probe_task",
            budget_remaining_usd=1.0,
            event_queue=None,
            initial_effort="low",
            drive_root=drive_root,
        )
    finally:
        pricing.get_pricing = original_get_pricing
    if not capture.calls:
        raise RuntimeError("no-forward transport seam did not capture a provider request")
    first = capture.calls[0]
    first_messages = first.get("messages") or []
    system_messages = [item for item in first_messages if item.get("role") == "system"]
    if len(system_messages) != 1:
        raise RuntimeError("first provider request must contain one system-role message")
    system_text = message_content_text(system_messages[0])
    runtime_context = decoded_runtime_context(system_text)
    provider_contract = runtime_context.get("task_contract")
    provider_constraints = (
        provider_contract.get("constraints") if isinstance(provider_contract, dict) else None
    )
    if (
        not isinstance(provider_constraints, str)
        or provider_constraints != body
        or system_text.count(MARKER) != 1
    ):
        raise RuntimeError("authoritative task constraints were not injected exactly once")
    contract = ctx.task_contract
    if contract.get("constraints") != body or not isinstance(contract.get("constraints"), str):
        raise RuntimeError("task_contract.constraints is not the exact skill body string")
    provider_tools = first.get("tools") or []
    tool_names = sorted(schema_name(item) for item in provider_tools)
    if set(tool_names) != ALLOWED_TOOL_NAMES or len(tool_names) != 2:
        raise RuntimeError("first provider request did not receive the exact two-tool set")
    provider_schema_hashes: dict[str, str] = {}
    for tool in provider_tools:
        if not isinstance(tool, dict):
            raise RuntimeError("provider request contains a malformed tool schema")
        issues = strict_function_tool_issues(tool)
        if issues:
            raise RuntimeError(
                f"provider request contains a non-strict schema: {'; '.join(issues)}"
            )
        provider_schema_hashes[schema_name(tool)] = strict_hash_json(tool)
    return {
        "forwarded_to_provider": False,
        "capture_call_count": len(capture.calls),
        "first_request_system_role_count": 1,
        "system_runtime_context_decoded": True,
        "provider_task_contract_constraints_is_string": True,
        "provider_task_contract_constraints_exact_body": True,
        "task_contract_constraints_is_string": True,
        "task_contract_constraints_exact_body": True,
        "contract_marker_count": 1,
        "prompt_hash": sha256_bytes(body.strip().encode("utf-8") + b"\n"),
        "provider_tool_names": tool_names,
        "provider_tool_set_exact": True,
        "provider_tools_strict": True,
        "provider_main_max_tokens": int(first.get("max_tokens") or 0),
        "provider_schema_hashes": provider_schema_hashes,
        "description_contains_contract_marker": MARKER in str(task["description"]),
    }


def task_api_contract() -> dict[str, Any]:
    from ouroboros.gateway.contracts import TaskCreateRequest  # type: ignore[import-not-found]

    hints = get_type_hints(TaskCreateRequest)
    required = sorted(getattr(TaskCreateRequest, "__required_keys__", set()))
    required_fields = {
        "description",
        "constraints",
        "disabled_tools",
        "answer_protocol",
        "project_id",
        "memory_mode",
        "timeout_sec",
        "source",
        "metadata",
    }
    if not required_fields.issubset(hints):
        raise RuntimeError("pinned TaskCreateRequest is missing required adapter fields")
    if hints["constraints"] is not str:
        raise RuntimeError("TaskCreateRequest.constraints is not a string")
    return {
        "field_names": sorted(hints),
        "required_field_names": required,
        "constraints_type": "str",
        "answer_protocol_supported": "answer_protocol" in hints,
        "disabled_tools_supported": "disabled_tools" in hints,
        "memory_mode_supported": "memory_mode" in hints,
        "project_id_supported": "project_id" in hints,
        "timeout_sec_supported": "timeout_sec" in hints,
    }


def atomic_write_lock(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = pathlib.Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o640)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    from strict_tool_adapter import (
        adapter_metadata,
        install_strict_tool_adapter,
        strict_function_tool_issues,
        strict_schema_metrics,
    )
    from strict_tool_adapter import (
        hash_json as strict_hash_json,
    )

    installed_adapter = install_strict_tool_adapter()
    from runtime_launcher import install_glm_main_loop_output_cap

    install_glm_main_loop_output_cap()
    from ouroboros.config import (  # type: ignore[import-not-found]
        apply_settings_to_env,
        load_settings,
    )
    from ouroboros.mcp_client import (  # type: ignore[import-not-found]
        get_manager,
        reset_manager_for_tests,
    )
    from ouroboros.tools.registry import ToolContext, ToolRegistry  # type: ignore[import-not-found]

    drive_root = pathlib.Path(os.environ.get("OUROBOROS_DATA_DIR", "/data"))
    probe_drive = pathlib.Path(os.environ.get("CONTRACT_PROBE_DRIVE", "/probe-drive"))
    probe_drive.mkdir(parents=True, exist_ok=True)
    (probe_drive / "logs").mkdir(parents=True, exist_ok=True)
    runtime_lock = json.loads(RUNTIME_LOCK_PATH.read_text(encoding="utf-8"))
    identity_kind = str(os.environ.get("CONTRACT_IDENTITY_KIND") or "docker_image").strip()
    if identity_kind not in {"docker_image", "railway_deployment"}:
        raise RuntimeError("CONTRACT_IDENTITY_KIND is unsupported")
    image_id = str(os.environ.get("CONTRACT_IMAGE_ID") or "").strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise RuntimeError("CONTRACT_IMAGE_ID must be a sha256 runtime identity")
    if VERSION_PATH.read_text(encoding="utf-8").strip() != runtime_lock["tag"].removeprefix("v"):
        raise RuntimeError("runtime VERSION does not match the pinned lock")

    settings = load_settings()
    apply_settings_to_env(settings)
    redacted_settings = redacted_mcp_settings(settings)
    if redacted_settings["MCP_TOOL_TIMEOUT_SEC"] != 5:
        raise RuntimeError("effective MCP tool timeout is not 5 seconds")
    reset_manager_for_tests()
    manager = get_manager()
    manager.reconfigure(settings)
    refresh = manager.refresh_all()
    outcomes = refresh.get("refreshed") or {}
    if set(outcomes) != {"factory"} or not outcomes["factory"].get("ok"):
        raise RuntimeError("factory MCP discovery failed")
    mcp_status = manager.status_payload()
    discovered_tools = manager.list_tools_for_registry()
    discovered_names = sorted(str(item.get("name") or "") for item in discovered_tools)
    if set(discovered_names) != ALLOWED_TOOL_NAMES or len(discovered_names) != 2:
        raise RuntimeError("MCP discovery did not return exactly two prefixed tools")
    if int(mcp_status.get("tool_timeout_sec") or 0) != 5:
        raise RuntimeError("MCP status readback timeout differs from settings")

    skill_record, body = load_skill_contract(drive_root)
    catalog, catalog_hash = extension_catalog(drive_root)
    registry = ToolRegistry(repo_dir=pathlib.Path("/opt/ouroboros"), drive_root=probe_drive)
    pre_context = ToolContext(
        repo_dir=pathlib.Path("/opt/ouroboros"),
        drive_root=probe_drive,
        memory_mode="forked",
        project_id="campaign_contract_probe",
        task_contract={"allowed_resources": {"network": True}, "disabled_tools": []},
    )
    registry.set_context(pre_context)
    raw_schema_method = getattr(type(registry).schemas, "__wrapped__", None)
    if not callable(raw_schema_method):
        raise RuntimeError("strict adapter did not preserve the pinned registry seam")
    raw_pre_schemas = raw_schema_method(registry, core_only=False)
    pre_schemas = registry.schemas()
    pre_names = [schema_name(item) for item in pre_schemas]
    if not pre_names or any(not name for name in pre_names):
        raise RuntimeError("pre-deny tool inventory contains an unnamed schema")
    if len(pre_names) != len(set(pre_names)):
        raise RuntimeError("pre-deny tool inventory contains a schema collision")
    if not ALLOWED_TOOL_NAMES.issubset(pre_names):
        raise RuntimeError("pre-deny inventory is missing a required factory tool")
    raw_pre_by_name = {schema_name(item): item for item in raw_pre_schemas}
    pre_by_name = {schema_name(item): item for item in pre_schemas}
    non_target_names = set(pre_names) - ALLOWED_TOOL_NAMES
    if any(
        canonical_json(raw_pre_by_name.get(name)) != canonical_json(pre_by_name.get(name))
        for name in non_target_names
    ):
        raise RuntimeError("strict adapter changed a non-target provider schema")
    schema_hashes = {
        schema_name(item): hash_json(item) for item in sorted(pre_schemas, key=schema_name)
    }
    baseline_schema_hashes = {
        schema_name(item): hash_json(item) for item in sorted(raw_pre_schemas, key=schema_name)
    }
    disabled_tools = sorted(set(pre_names) - ALLOWED_TOOL_NAMES)
    post_context = ToolContext(
        repo_dir=pathlib.Path("/opt/ouroboros"),
        drive_root=probe_drive,
        memory_mode="forked",
        project_id="campaign_contract_probe",
        task_contract={
            "allowed_resources": {"network": True},
            "disabled_tools": disabled_tools,
        },
    )
    registry.set_context(post_context)
    post_schemas = registry.schemas()
    post_names = sorted(schema_name(item) for item in post_schemas)
    if set(post_names) != ALLOWED_TOOL_NAMES or len(post_names) != 2:
        raise RuntimeError("generated denylist did not reduce schemas to exact factory tools")
    strict_provider_schemas: dict[str, dict[str, Any]] = {}
    for tool in post_schemas:
        name = schema_name(tool)
        issues = strict_function_tool_issues(tool)
        if issues:
            raise RuntimeError(
                f"strict provider schema audit failed for {name}: {'; '.join(issues)}"
            )
        function = tool.get("function") or {}
        strict_provider_schemas[name] = {
            "strict": function.get("strict") is True,
            "supported_subset": True,
            "schema_hash": strict_hash_json(tool),
            "parameters_hash": strict_hash_json(function.get("parameters") or {}),
            "metrics": strict_schema_metrics(function.get("parameters") or {}),
            "normalized_schema": tool,
        }
    denied_execution = registry.execute(disabled_tools[0], {})
    if "task_contract.disabled_tools withholds" not in denied_execution:
        raise RuntimeError("disabled tool execution did not fail closed")

    provider_probe = provider_seam_probe(
        registry=registry,
        body=body,
        disabled_tools=disabled_tools,
        drive_root=probe_drive,
    )
    if provider_probe["prompt_hash"] != skill_record["prompt_hash"]:
        raise RuntimeError("provider seam prompt hash differs from reviewed skill hash")
    selected_profile = requested_provider_profile(
        dict(os.environ),
        variable="CF_PROVIDER_PROFILE",
        default=CANONICAL_PROFILE_NAME,
    )
    if provider_probe["provider_main_max_tokens"] != selected_profile.main_loop_max_tokens:
        raise RuntimeError("provider seam main-loop output cap differs from the selected profile")
    safety_call_timeout = int(settings.get("OUROBOROS_SAFETY_CALL_TIMEOUT_SEC") or 0)
    tool_call_timeout = int(settings.get("OUROBOROS_TOOL_TIMEOUT_SEC") or 0)
    if safety_call_timeout != selected_profile.safety_call_timeout_seconds:
        raise RuntimeError("runtime safety-call timeout differs from the selected profile")
    if tool_call_timeout != selected_profile.tool_call_timeout_seconds:
        raise RuntimeError("runtime tool-call timeout differs from the selected profile")
    if provider_probe["provider_schema_hashes"] != {
        name: row["schema_hash"] for name, row in strict_provider_schemas.items()
    }:
        raise RuntimeError("provider seam schemas differ from the audited strict schemas")

    lock = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime": {
            "tag": runtime_lock["tag"],
            "commit": runtime_lock["commit"],
            "source_archive_sha256": runtime_lock["source_archive_sha256"],
            "base_image_index_digest": runtime_lock["base_image_index_digest"],
            "requirements_lock_sha256": runtime_lock["requirements_lock_sha256"],
            "image_id": image_id,
            "identity_kind": identity_kind,
            "version_readback": VERSION_PATH.read_text(encoding="utf-8").strip(),
            "main_loop_max_tokens": provider_probe["provider_main_max_tokens"],
            "safety_call_timeout_seconds": safety_call_timeout,
            "tool_call_timeout_seconds": tool_call_timeout,
            "expected_profile": {
                "runtime_mode": "light",
                "context_mode": "low",
                "safety_mode": "full",
                "evolution_enabled": False,
                "background_enabled": False,
            },
        },
        "task_api": task_api_contract(),
        "skill": skill_record,
        "mcp": {
            "settings": redacted_settings,
            "settings_hash": hash_json(redacted_settings),
            "admission_projection": mcp_admission_projection(mcp_status),
            "admission_hash": hash_json(mcp_admission_projection(mcp_status)),
            "status_readback": {
                "enabled": bool(mcp_status.get("enabled")),
                "sdk_available": bool(mcp_status.get("sdk_available")),
                "tool_timeout_sec": int(mcp_status.get("tool_timeout_sec") or 0),
                "server_count": len(mcp_status.get("servers") or []),
                "discovered_prefixed_names": discovered_names,
            },
        },
        "extensions": {
            "catalog_hash": catalog_hash,
            "catalog_count": len(catalog),
            "catalog_names": sorted(str(item.get("name") or "") for item in catalog),
            "admission_projection": extension_admission_projection(catalog),
            "admission_hash": hash_json(extension_admission_projection(catalog)),
            "live_extension_names": [],
        },
        "tools": {
            "effective_tool_names": sorted(pre_names),
            "schema_hashes": schema_hashes,
            "baseline_schema_hashes": baseline_schema_hashes,
            "inventory_hash": hash_json(
                {"names": sorted(pre_names), "schema_hashes": schema_hashes}
            ),
            "disabled_tools": disabled_tools,
            "denylist_hash": hash_json(disabled_tools),
            "post_deny_tool_names": post_names,
            "post_deny_schema_hash": hash_json(post_schemas),
            "disabled_execution_blocked": True,
            "non_target_schemas_byte_equal": True,
            "strict_adapter": {**adapter_metadata(), **installed_adapter},
            "strict_provider_schemas": strict_provider_schemas,
        },
        "provider_probe": provider_probe,
    }
    output_dir = pathlib.Path(os.environ.get("CONTRACT_LOCK_DIR", "/contract-lock"))
    output = output_dir / "communication_factory.lock.json"
    atomic_write_lock(output, lock)
    print(
        "contract-probe: PASS "
        f"runtime={runtime_lock['tag']} inventory={len(pre_names)} "
        f"effective={','.join(post_names)} prompt_sha256={skill_record['prompt_hash']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
