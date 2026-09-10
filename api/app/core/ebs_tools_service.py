"""Invoke the embedded EBSMCP tools from OraEBSAgent's agents.

This is the layer that replaces generated SQL with deterministic tool
calls — the whole point of the merge. An agent lists the available EBS
tools, the LLM picks one by name with typed arguments, and this service
executes it through EBSMCP's governed pipeline (identity -> entitlement ->
instance routing -> audit). The model never writes SQL, so it cannot
hallucinate a schema.

Decoupled from OraEBSAgent's database on purpose: it takes a ready
ToolContext (built by app.core.ebs_bridge.build_tool_context) rather than
reaching for a session itself, so it can be unit-tested against a mock
context with no Postgres or EBS.

The tools live on an in-process MCPServer reached over the SDK's in-memory
transport — the same path EBSMCP's own tests use — so the tool schemas the
LLM sees are exactly the ones the server enforces, with no second copy to
drift.
"""

from __future__ import annotations

import json
from typing import Any

from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from app.ebsmcp.context import set_current_subject
from app.ebsmcp.tools import (
    CONCURRENT_PROCESSING_TOOLSET,
    DIAGNOSE_REQUEST_TOOLSET,
    HEALTH_TOOLSET,
    HIGH_AVAILABILITY_DR_TOOLSET,
    INDEX_HEALTH_TOOLSET,
    INSTANCE_HEALTH_TOOLSET,
    MEMORY_TOOLSET,
    NOTIFICATION_DIAGNOSIS_TOOLSET,
    OUTPUT_PRINTING_TOOLSET,
    PATCH_VERSION_TRACKING_TOOLSET,
    REDO_ARCHIVE_BACKUP_TOOLSET,
    SECURITY_CONFIGURATION_TOOLSET,
    WORKFLOW_TOOLSET,
    ToolContext,
    mount_toolsets,
)

# The DBA toolsets exposed to OraEBSAgent's agents. HEALTH_TOOLSET
# (server_health) is deliberately omitted: it uses the functional "ebs"
# persona rather than "ebs_dba", so it only muddies tool selection for a
# DBA-diagnostics agent. Trim this list per-agent to keep selection sharp
# (Microsoft's own guidance: response quality drops past ~10 tools).
_DBA_TOOLSETS = [
    INSTANCE_HEALTH_TOOLSET,
    CONCURRENT_PROCESSING_TOOLSET,
    DIAGNOSE_REQUEST_TOOLSET,
    WORKFLOW_TOOLSET,
    NOTIFICATION_DIAGNOSIS_TOOLSET,
    OUTPUT_PRINTING_TOOLSET,
    SECURITY_CONFIGURATION_TOOLSET,
    MEMORY_TOOLSET,
    REDO_ARCHIVE_BACKUP_TOOLSET,
    HIGH_AVAILABILITY_DR_TOOLSET,
    PATCH_VERSION_TRACKING_TOOLSET,
    INDEX_HEALTH_TOOLSET,
]


def build_ebs_mcp_app(ctx: ToolContext, toolsets=None) -> MCPServer:
    """A fresh in-process MCP server with the EBS toolsets mounted on the
    given context. No auth wiring — the caller's subject is supplied per
    request via the contextvar (set_current_subject), not a bearer token.
    """
    app = MCPServer("oraebsagent-ebs")
    mount_toolsets(app, ctx, toolsets or _DBA_TOOLSETS)
    return app


async def list_ebs_tools(ctx: ToolContext, toolsets=None) -> list[dict[str, Any]]:
    """The available EBS tools as LLM-callable specs: name, description,
    JSON input schema. Feed these to the model as its choices.
    """
    app = build_ebs_mcp_app(ctx, toolsets)
    transport = InMemoryTransport(app)
    async with transport._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            return [
                {"name": t.name, "description": t.description or "",
                 "input_schema": t.input_schema}
                for t in tools
            ]


async def call_ebs_tool(
    ctx: ToolContext, subject: str, name: str, arguments: dict[str, Any] | None = None,
    toolsets=None,
) -> dict[str, Any]:
    """Execute one EBS tool as `subject`, through the governed pipeline.

    Returns {ok, tool, result|error}. `result` is the tool's structured
    payload — never generated SQL. Denials and unknown tools come back as
    ok=False with the real reason, so the agent can tell the user plainly
    rather than fabricating an answer.
    """
    set_current_subject(subject)
    app = build_ebs_mcp_app(ctx, toolsets)
    transport = InMemoryTransport(app)
    async with transport._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(name, arguments or {})
            text = res.content[0].text if res.content else ""
            if res.is_error:
                return {"ok": False, "tool": name, "error": text}
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                payload = text
            return {"ok": True, "tool": name, "result": payload}
