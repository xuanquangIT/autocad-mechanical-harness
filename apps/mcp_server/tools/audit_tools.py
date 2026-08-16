"""Existing-drawing standards audit tool."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context as McpContext
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from apps.mcp_server.context import ServerContext, failure
from apps.mcp_server.tools.permissions import ToolPermissionGuard
from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.drawing_model import DrawingModel
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus
from cad_harness.domain.models.validation import DrawingAuditEvidence
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id


def _request_id(value: str | None) -> str:
    return value or new_id(IdPrefix.REQUEST)


def _actor_id(request_context: McpContext[Any, Any, Any]) -> str:
    try:
        return request_context.client_id or "anonymous"
    except ValueError:
        return "anonymous"


class DrawingAuditToolResponse(ToolResponse):
    """Envelope whose successful payload is persisted drawing-audit evidence."""

    data: DrawingAuditEvidence | dict[str, Any] = Field(  # type: ignore[assignment]
        default_factory=dict
    )


def register(mcp: FastMCP, context: ServerContext, guard: ToolPermissionGuard) -> None:

    @guard.tool(mcp)
    def cad_audit(
        request_context: McpContext[Any, Any, Any],
        model: DrawingModel,
        profile_ref: str | None = None,
        request_id: str | None = None,
    ) -> DrawingAuditToolResponse:
        """Audit a DrawingModel against versioned company and geometry rules.

        Returns persisted audit evidence, including ``audit_id``, suitable for the
        remediation compiler. It does not modify the source drawing.
        """

        resolved_request_id = _request_id(request_id)
        try:
            profile = context.company_profile if profile_ref is None else load_profile(profile_ref)
            expected_layers_by_ref = {
                mapping.entity_ref: mapping.expected_layer
                for mapping in context.service.store.entity_mappings_for(model.document_id)
                if mapping.expected_layer is not None
            }
            evidence = context.drawing_audit_service.audit_with_evidence(
                model,
                profile=profile,
                tolerance=profile.tolerance(),
                actor_id=_actor_id(request_context),
                expected_layers_by_ref=expected_layers_by_ref,
            )
            return DrawingAuditToolResponse(
                status=ToolStatus.OK,
                data=evidence.model_dump(mode="json"),
                request_id=resolved_request_id,
            )
        except Exception as exc:
            return DrawingAuditToolResponse.model_validate(
                failure(exc, request_id=resolved_request_id)
            )


__all__ = ["register"]
