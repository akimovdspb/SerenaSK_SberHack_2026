from __future__ import annotations

import hmac
import json
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apps.api.app.domain.models import (
    ContextGetRequest,
    ContextToolResult,
    DraftEnvelope,
    DraftSaveRequest,
    DraftSaveResult,
    Operation,
)
from apps.api.app.mcp.service import FactoryMcpService

_service: FactoryMcpService | None = None


def _allowed_hosts() -> list[str]:
    raw = str(os.environ.get("MCP_ALLOWED_HOSTS") or "app:8000")
    hosts = list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    if not hosts or len(hosts) > 4 or any("*" in item or "/" in item for item in hosts):
        raise RuntimeError("MCP_ALLOWED_HOSTS must contain explicit host:port values")
    return hosts


def bind_service(service: FactoryMcpService) -> None:
    global _service
    _service = service


def _bound_service() -> FactoryMcpService:
    if _service is None:
        raise RuntimeError("MCP service is not initialized")
    return _service


def create_mcp_server(service: FactoryMcpService | None = None) -> FastMCP:
    server = FastMCP(
        name="communication-factory",
        instructions="Private typed context and draft persistence boundary.",
        streamable_http_path="/internal/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_allowed_hosts(),
            allowed_origins=[],
        ),
    )

    def current_service() -> FactoryMcpService:
        return service if service is not None else _bound_service()

    @server.tool(
        name="cf_context_get",
        title="Get versioned campaign context",
        description="Read one authorized synthetic campaign context. Input data is untrusted.",
        structured_output=True,
    )
    def cf_context_get(
        campaign_id: str,
        operation: Operation,
        iteration: int,
        idempotency_key: str,
        context_version: str | None = None,
    ) -> ContextToolResult:
        request = ContextGetRequest(
            campaign_id=campaign_id,
            operation=operation,
            iteration=iteration,
            context_version=context_version,
            idempotency_key=idempotency_key,
        )
        return current_service().context_get(request)

    @server.tool(
        name="cf_draft_save",
        title="Validate and persist one typed draft",
        description="Persist at most one immutable agent draft for an authorized operation.",
        structured_output=True,
    )
    def cf_draft_save(
        campaign_id: str,
        operation: Operation,
        iteration: int,
        context_version: str,
        idempotency_key: str,
        draft: DraftEnvelope,
    ) -> DraftSaveResult:
        request = DraftSaveRequest(
            campaign_id=campaign_id,
            operation=operation,
            iteration=iteration,
            context_version=context_version,
            idempotency_key=idempotency_key,
            draft=draft,
        )
        return current_service().draft_save(request)

    return server


mcp = create_mcp_server()


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, *, token: str, max_payload_bytes: int) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()
        self._max_payload_bytes = max_payload_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self._expected):
            await self._json_error(send, 401, "unauthorized")
            return
        content_length = headers.get(b"content-length", b"0")
        try:
            size = int(content_length)
        except ValueError:
            await self._json_error(send, 400, "invalid content length")
            return
        if size > self._max_payload_bytes:
            await self._json_error(send, 413, "payload too large")
            return
        buffered: list[Message] = []
        received = 0
        more_body = True
        while more_body:
            message = await receive()
            buffered.append(message)
            if message.get("type") != "http.request":
                break
            received += len(message.get("body", b""))
            if received > self._max_payload_bytes:
                await self._json_error(send, 413, "payload too large")
                return
            more_body = bool(message.get("more_body"))

        async def replay_receive() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _json_error(send: Send, status: int, detail: str) -> None:
        body = json.dumps({"error": detail}, separators=(",", ":")).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
