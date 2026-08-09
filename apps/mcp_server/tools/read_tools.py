"""Read-only tools. No side effects, no approval required."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context as McpContext
from mcp.server.fastmcp import FastMCP

from apps.mcp_server.context import ServerContext, failure, ok
from apps.mcp_server.tools.permissions import ToolPermissionGuard


def register(mcp: FastMCP, context: ServerContext, guard: ToolPermissionGuard) -> None:

    @guard.tool(mcp)
    def cad_status(request_context: McpContext[Any, Any, Any]) -> dict[str, Any]:
        """Report server, adapter, AutoCAD and capability status.


        Call this first. It tells you which adapter is active, which features are

        available, and whether the loaded standard profile is company approved.
        """

        try:
            return ok(context.service.status())

        except Exception as exc:
            return failure(exc)

    @guard.tool(mcp)
    def cad_document_inspect(
        request_context: McpContext[Any, Any, Any], document_id: str | None = None
    ) -> dict[str, Any]:
        """Read document metadata, layers, styles and the current revision.


        Required before creating a job: the returned revision is what the commit is

        later checked against.
        """

        try:
            snapshot = context.service.inspect_document(document_id)

            return ok(snapshot.model_dump(mode="json", exclude_none=True))

        except Exception as exc:
            return failure(exc)

    @guard.tool(mcp)
    def cad_selection_inspect(
        request_context: McpContext[Any, Any, Any], document_id: str, max_entities: int = 200
    ) -> dict[str, Any]:
        """Read the engineer's current selection, capped at ``max_entities``.


        Scoped deliberately: this never returns the whole entity database.
        """

        try:
            return ok(context.service.inspect_selection(document_id, max_entities))

        except Exception as exc:
            return failure(exc)

    @guard.tool(mcp)
    def cad_feature_catalog_search(
        request_context: McpContext[Any, Any, Any], query: str = ""
    ) -> dict[str, Any]:
        """Find supported mechanical features and their required parameters.


        Use this before writing a spec. A feature that is not listed is not supported;

        do not substitute a different feature or invent geometry for it.
        """

        try:
            features = context.service.search_features(query)

            return ok({"features": features, "count": len(features)})

        except Exception as exc:
            return failure(exc)
