"""The 13 high-level MCP tools, grouped by side effect.

Grouping mirrors the permission model: read tools, internal-DB tools, preview tools and
write tools. A client profile can be granted the read groups without the write group.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from apps.mcp_server.context import ServerContext
from apps.mcp_server.tools import job_tools, preview_tools, read_tools, write_tools

#: Canonical tool names, used by the compatibility test suite.
TOOL_NAMES: tuple[str, ...] = (
    "cad_status",
    "cad_document_inspect",
    "cad_selection_inspect",
    "cad_feature_catalog_search",
    "cad_job_create",
    "cad_spec_submit",
    "cad_change_submit",
    "cad_preview",
    "cad_validate",
    "cad_diff_get",
    "cad_commit",
    "cad_rollback",
    "cad_export",
)

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "cad_status",
        "cad_document_inspect",
        "cad_selection_inspect",
        "cad_feature_catalog_search",
        "cad_validate",
        "cad_diff_get",
    }
)

#: Tools that need an engineer's approval, per architecture section 10.
APPROVAL_REQUIRED_TOOLS: frozenset[str] = frozenset({"cad_commit", "cad_rollback", "cad_export"})


def register_all(mcp: FastMCP, context: ServerContext) -> None:
    read_tools.register(mcp, context)
    job_tools.register(mcp, context)
    preview_tools.register(mcp, context)
    write_tools.register(mcp, context)


__all__ = ["APPROVAL_REQUIRED_TOOLS", "READ_ONLY_TOOLS", "TOOL_NAMES", "register_all"]
