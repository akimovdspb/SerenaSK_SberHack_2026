from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

import httpx
import pytest

from apps.api.app.ouroboros_client import (
    ManagedTaskTransportError,
    OuroborosTaskAdapter,
    TaskAdmission,
    TaskAdmissionError,
    TaskTransportFailure,
    build_campaign_task,
    extension_admission_projection,
    extract_skill_body,
    hash_json,
    mcp_admission_projection,
    parse_retry_after,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _responses() -> dict[str, dict[str, Any]]:
    extension_rows = [
        {
            "name": "communication_factory",
            "type": "instruction",
            "version": "1.0.0",
            "enabled": True,
            "review_status": "clean",
            "review_stale": False,
            "executable_review": True,
            "load_error": "",
            "source": "user_repo",
            "grants": {"all_granted": True},
        },
        {
            "name": "unix_computer_use",
            "type": "extension",
            "version": "0.2.0",
            "enabled": False,
            "review_status": "pending",
            "review_stale": True,
            "executable_review": False,
            "load_error": None,
            "source": "native",
        },
    ]
    mcp = {
        "enabled": True,
        "sdk_available": True,
        "tool_timeout_sec": 5,
        "servers": [
            {
                "id": "factory",
                "name": "Communication Factory",
                "enabled": True,
                "transport": "streamable_http",
                "url": "http://app:8000/internal/mcp",
                "auth_configured": True,
                "last_error": "",
                "tools": [
                    {
                        "name": "cf_context_get",
                        "prefixed_name": "mcp_factory__cf_context_get",
                    },
                    {
                        "name": "cf_draft_save",
                        "prefixed_name": "mcp_factory__cf_draft_save",
                    },
                ],
            }
        ],
    }
    return {
        "/api/state": {
            "supervisor_ready": True,
            "supervisor_error": None,
            "workers_alive": 10,
            "workers_total": 10,
            "runtime_mode": "light",
            "context_mode": "low",
            "safety_mode": "full",
            "evolution_enabled": False,
            "bg_consciousness_enabled": False,
        },
        "/api/extensions/communication_factory/manifest": {
            "name": "communication_factory",
            "content_hash": "official-content-hash",
            "load_error": "",
            "manifest": {
                "name": "communication_factory",
                "version": "1.0.0",
                "type": "instruction",
                "permissions": [],
            },
        },
        "/api/extensions": {"skills": extension_rows},
        "/api/mcp/status": mcp,
    }


def _adapter(
    tmp_path: pathlib.Path,
    responses: dict[str, dict[str, Any]],
    *,
    expected_identity_kind: str = "docker_image",
) -> OuroborosTaskAdapter:
    skill_path = ROOT / "ouroboros" / "skills" / "communication_factory" / "SKILL.md"
    skill_raw = skill_path.read_bytes()
    body = extract_skill_body(skill_raw)
    extension_rows = responses["/api/extensions"]["skills"]
    mcp = responses["/api/mcp/status"]
    effective = ["built_in_a", "mcp_factory__cf_context_get", "mcp_factory__cf_draft_save"]
    lock = {
        "schema_version": 1,
        "runtime": {
            "image_id": f"sha256:{'a' * 64}",
            "expected_profile": {
                "runtime_mode": "light",
                "context_mode": "low",
                "safety_mode": "full",
                "evolution_enabled": False,
                "background_enabled": False,
            },
        },
        "skill": {
            "skill_file_sha256": hashlib.sha256(skill_raw).hexdigest(),
            "prompt_hash": hashlib.sha256(body.encode() + b"\n").hexdigest(),
            "skill_content_hash": "official-content-hash",
            "activation_mode": "adapter_injected",
            "ready": True,
            "version": "1.0.0",
        },
        "extensions": {
            "catalog_names": ["communication_factory", "unix_computer_use"],
            "admission_hash": hash_json(extension_admission_projection(extension_rows)),
        },
        "mcp": {"admission_hash": hash_json(mcp_admission_projection(mcp))},
        "tools": {
            "effective_tool_names": effective,
            "inventory_hash": "f" * 64,
            "disabled_tools": ["built_in_a"],
            "post_deny_tool_names": [
                "mcp_factory__cf_context_get",
                "mcp_factory__cf_draft_save",
            ],
        },
        "provider_probe": {
            "provider_tool_names": [
                "mcp_factory__cf_context_get",
                "mcp_factory__cf_draft_save",
            ],
            "provider_tool_set_exact": True,
        },
    }
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path])

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://ouroboros:8765",
    )
    return OuroborosTaskAdapter(
        base_url="http://ouroboros:8765",
        lock_path=lock_path,
        skill_path=skill_path,
        expected_identity_kind=expected_identity_kind,
        client=client,
    )


def test_task_admission_binds_current_runtime_skill_mcp_and_denylist(
    tmp_path: pathlib.Path,
) -> None:
    adapter = _adapter(tmp_path, _responses())

    admission = adapter.admit()

    assert admission.constraints.startswith("COMMUNICATION_FACTORY_CONTRACT_V1\n")
    assert admission.disabled_tools == ["built_in_a"]
    assert admission.skill_content_hash == "official-content-hash"
    assert admission.tool_inventory_hash == "f" * 64
    assert admission.activation_mode == "adapter_injected"
    assert admission.runtime_image_id == f"sha256:{'a' * 64}"


def test_task_admission_fails_before_provider_on_mcp_drift(tmp_path: pathlib.Path) -> None:
    responses = _responses()
    adapter = _adapter(tmp_path, responses)
    responses["/api/mcp/status"]["tool_timeout_sec"] = 6

    with pytest.raises(TaskAdmissionError, match="MCP settings"):
        adapter.admit()


def test_task_admission_rejects_a_contract_from_another_deployment_profile(
    tmp_path: pathlib.Path,
) -> None:
    adapter = _adapter(
        tmp_path,
        _responses(),
        expected_identity_kind="railway_deployment",
    )

    with pytest.raises(TaskAdmissionError, match="runtime image identity"):
        adapter.admit()


def test_campaign_task_matches_pinned_adapter_contract_and_keeps_data_in_mcp() -> None:
    admission = TaskAdmission(
        constraints="COMMUNICATION_FACTORY_CONTRACT_V1\nПолный проверенный контракт.",  # noqa: RUF001
        disabled_tools=["run_command", "web_search"],
        prompt_hash="a" * 64,
        skill_content_hash="b" * 64,
        tool_inventory_hash="c" * 64,
        activation_mode="adapter_injected",
        runtime_image_id=f"sha256:{'d' * 64}",
    )

    payload = build_campaign_task(
        task_id="task_campaign_0001",
        run_id="run_campaign_0001",
        campaign_id="campaign_0001",
        operation="initial",
        iteration=1,
        idempotency_key="campaign-operation-key-0001",
        context_version="e" * 64,
        project_id="campaign_project_0001",
        admission=admission,
    )

    assert payload["constraints"] == admission.constraints
    assert isinstance(payload["constraints"], str)
    assert payload["disabled_tools"] == admission.disabled_tools
    assert payload["allowed_resources"] == {"network": True}
    assert payload["memory_mode"] == "forked"
    assert payload["timeout_sec"] == 25
    assert "campaign-operation-key-0001" in payload["description"]
    assert payload["metadata"]["context_version"] == "e" * 64
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "fact_term_14_days" not in serialized
    assert "14 дней" not in serialized


def test_task_submit_exposes_typed_429_and_retry_after_without_response_body(
    tmp_path: pathlib.Path,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"}, json={"secret": "ignored"})

    adapter = OuroborosTaskAdapter(
        base_url="http://ouroboros:8765",
        lock_path=tmp_path / "unused-lock.json",
        skill_path=tmp_path / "unused-skill.md",
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://ouroboros:8765",
        ),
    )

    with pytest.raises(ManagedTaskTransportError) as caught:
        adapter.submit_task({"task_id": "task_typed_429"})

    assert caught.value.failure is TaskTransportFailure.HTTP_STATUS
    assert caught.value.http_status == 429
    assert caught.value.retry_after_seconds == 7
    assert caught.value.acceptance_ambiguous is False
    assert "secret" not in str(caught.value)


def test_task_submit_read_timeout_is_ambiguous_and_lookup_404_is_explicit(
    tmp_path: pathlib.Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(404, json={"detail": "not found"})

    adapter = OuroborosTaskAdapter(
        base_url="http://ouroboros:8765",
        lock_path=tmp_path / "unused-lock.json",
        skill_path=tmp_path / "unused-skill.md",
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://ouroboros:8765",
        ),
    )

    with pytest.raises(ManagedTaskTransportError) as submit:
        adapter.submit_task({"task_id": "task_ambiguous"})
    with pytest.raises(ManagedTaskTransportError) as lookup:
        adapter.task("task_ambiguous")

    assert submit.value.failure is TaskTransportFailure.READ_TIMEOUT
    assert submit.value.acceptance_ambiguous is True
    assert lookup.value.task_not_found is True
    assert lookup.value.http_status == 404


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", 0.0), ("1.5", 1.5), ("-1", None), ("not-a-date", None)],
)
def test_retry_after_parser_rejects_invalid_or_negative_values(
    value: str,
    expected: float | None,
) -> None:
    assert parse_retry_after(value) == expected
