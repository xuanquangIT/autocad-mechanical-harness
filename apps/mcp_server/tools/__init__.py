"""The 23 high-level MCP tools, grouped by side effect.

Grouping mirrors the permission model: read tools, internal-DB tools, preview tools and
write tools. A client profile can be granted the read groups without the write group.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from apps.mcp_server.context import ServerContext
from apps.mcp_server.tools import (
    audit_tools,
    comprehension_tools,
    job_tools,
    measure_tools,
    preview_tools,
    raster_tools,
    read_tools,
    takeoff_tools,
    write_tools,
)
from apps.mcp_server.tools.permissions import ToolPermissionGuard
from cad_harness.security.client_profiles import (
    APPROVAL_REQUIRED_TOOLS,
    DWG_MUTATING_TOOLS,
    PLANNING_TOOLS,
    READ_ONLY_TOOLS,
    TOOL_NAMES,
)


def register_all(mcp: FastMCP, context: ServerContext) -> None:
    guard = ToolPermissionGuard(context)
    read_tools.register(mcp, context, guard)
    job_tools.register(mcp, context, guard)
    preview_tools.register(mcp, context, guard)
    write_tools.register(mcp, context, guard)
    comprehension_tools.register(mcp, context, guard)
    takeoff_tools.register(mcp, context, guard)
    audit_tools.register(mcp, context, guard)
    measure_tools.register(mcp, context, guard)
    raster_tools.register(mcp, context, guard)


__all__ = [
    "APPROVAL_REQUIRED_TOOLS",
    "DWG_MUTATING_TOOLS",
    "PLANNING_TOOLS",
    "READ_ONLY_TOOLS",
    "TOOL_NAMES",
    "register_all",
]
