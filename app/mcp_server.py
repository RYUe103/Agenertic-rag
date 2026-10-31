from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool
from starlette.concurrency import run_in_threadpool

from app.integrations import ExternalToolCall
from app.tool_registry import describe_tools


def create_mcp_manager(authenticate: Callable[[Request], dict], call_tool: Callable[..., dict],
                       allowed_hosts: list[str], allowed_origins: list[str], list_available: Callable[[Request], list[dict]]) -> StreamableHTTPSessionManager:
    server = Server("agentic-rag-knowledge", version="1.0.0")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        request = server.request_context.request
        integration = authenticate(request)
        return [Tool(name=item["name"], description=item["description"], inputSchema={
            "type": "object", "properties": {
                "knowledge_base_id": {"type": "string", "enum": integration["knowledge_base_ids"]},
                "arguments": item["input_schema"],
                "approval_id": {"type": "string", "description": "通过 REST 审批接口获取的一次性批准编号"},
            }, "required": ["knowledge_base_id", "arguments"], "additionalProperties": False,
        }) for item in list_available(request)]

    @server.call_tool(validate_input=False)
    async def invoke(name: str, arguments: dict[str, Any]) -> dict:
        request = server.request_context.request
        try:
            call = ExternalToolCall.model_validate(arguments)
            return await run_in_threadpool(call_tool, name, call, request)
        except HTTPException as exc:
            raise ValueError(str(exc.detail)) from None

    return StreamableHTTPSessionManager(
        app=server, stateless=True, json_response=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                    allowed_hosts=allowed_hosts, allowed_origins=allowed_origins),
    )
