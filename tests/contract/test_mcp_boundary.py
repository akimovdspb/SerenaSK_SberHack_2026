from __future__ import annotations

from typing import Any

import httpx
import pytest

from apps.api.app.mcp.server import BearerAuthMiddleware, create_mcp_server, mcp


@pytest.mark.asyncio
async def test_mcp_inventory_has_exactly_two_raw_tools() -> None:
    tools = await mcp.list_tools()
    schemas = {tool.name: tool.inputSchema for tool in tools}

    assert set(schemas) == {"cf_context_get", "cf_draft_save"}
    assert set(schemas["cf_context_get"]["properties"]) == {
        "campaign_id",
        "operation",
        "iteration",
        "context_version",
        "idempotency_key",
    }
    assert set(schemas["cf_draft_save"]["properties"]) == {
        "campaign_id",
        "operation",
        "iteration",
        "context_version",
        "idempotency_key",
        "draft",
    }


def test_mcp_transport_accepts_only_the_private_compose_host() -> None:
    transport_security = mcp.settings.transport_security

    assert transport_security.enable_dns_rebinding_protection is True
    assert transport_security.allowed_hosts == ["app:8000"]
    assert transport_security.allowed_origins == []


def test_mcp_transport_accepts_the_explicit_railway_loopback_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "127.0.0.1:8000")

    server = create_mcp_server()

    assert server.settings.transport_security.allowed_hosts == ["127.0.0.1:8000"]


@pytest.mark.asyncio
async def test_mcp_auth_and_payload_limit_fail_closed() -> None:
    async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BearerAuthMiddleware(downstream, token="t" * 32, max_payload_bytes=4)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.post("/", content=b"ok")
        oversized = await client.post(
            "/",
            content=b"12345",
            headers={"Authorization": f"Bearer {'t' * 32}"},
        )
        accepted = await client.post(
            "/",
            content=b"1234",
            headers={"Authorization": f"Bearer {'t' * 32}"},
        )

    assert unauthorized.status_code == 401
    assert oversized.status_code == 413
    assert accepted.status_code == 204
