"""Bounded drawing reads and deterministic feature recognition."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context as McpContext
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from apps.mcp_server.context import ServerContext, failure
from apps.mcp_server.tools.permissions import ToolPermissionGuard
from cad_harness.company_rules.loader import load_profile
from cad_harness.comprehension.recognizer import recognize
from cad_harness.domain.models.drawing_model import DrawingModel, DrawingSummary
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus
from cad_harness.domain.models.recognition import RecognitionReport
from cad_harness.domain.ports.drawing_source import DrawingReadRequest
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.observability.audit import AuditEventType
from cad_harness.security.redaction import redact_path


def _request_id(value: str | None) -> str:
    return value or new_id(IdPrefix.REQUEST)


def _actor_id(request_context: McpContext[Any, Any, Any]) -> str:
    try:
        return request_context.client_id or "anonymous"
    except ValueError:
        return "anonymous"


class DrawingReadToolResponse(ToolResponse):
    """Envelope whose successful payload is a concrete drawing read contract."""

    data: DrawingModel | DrawingSummary | dict[str, Any] = Field(  # type: ignore[assignment]
        default_factory=dict
    )


class FeatureRecognitionToolResponse(ToolResponse):
    """Envelope whose successful payload is a recognition report."""

    data: RecognitionReport | dict[str, Any] = Field(  # type: ignore[assignment]
        default_factory=dict
    )


def register(mcp: FastMCP, context: ServerContext, guard: ToolPermissionGuard) -> None:

    @guard.tool(mcp)
    def cad_drawing_read(
        request_context: McpContext[Any, Any, Any],
        request: DrawingReadRequest,
        request_id: str | None = None,
    ) -> DrawingReadToolResponse:
        """Read a bounded configured drawing source into a revision-pinned semantic model.

        Omit ``scope`` for counts-only summary. Detailed reads require an explicit
        scope and enforce configured entity/depth limits before geometry is returned.
        Source paths never cross this MCP boundary.
        """

        resolved_request_id = _request_id(request_id)
        try:
            result = context.drawing_read_service.read(request)
            # Preserve required nullable contract fields such as ``to_mm_factor``;
            # callers must be able to validate this payload back into DrawingModel.
            payload = result.model_dump(mode="json")
            if "display_name" in payload:
                payload["display_name"] = redact_path(payload["display_name"])
            entity_count = len(payload.get("entities", ()))
            if not entity_count:
                entity_count = sum(payload.get("counts_by_entity_type", {}).values())
            audit_event_id = context.service.audit.append(
                event_type=AuditEventType.DRAWING_READ.value,
                job_id=None,
                actor_type="ai_client",
                actor_id=_actor_id(request_context),
                payload={
                    "document_id": payload["document_id"],
                    "revision": payload["revision"],
                    "entity_count": entity_count,
                    "detailed": "entities" in payload,
                },
            )
            return DrawingReadToolResponse(
                status=ToolStatus.OK,
                data=payload,
                request_id=resolved_request_id,
                audit_event_id=audit_event_id,
            )
        except Exception as exc:
            return DrawingReadToolResponse.model_validate(
                failure(exc, request_id=resolved_request_id)
            )

    @guard.tool(mcp)
    def cad_feature_recognize(
        request_context: McpContext[Any, Any, Any],
        model: DrawingModel,
        profile_ref: str | None = None,
        request_id: str | None = None,
    ) -> FeatureRecognitionToolResponse:
        """Recognize supported mechanical features without choosing ambiguities.

        The result preserves source entity references and revision provenance. Any
        ambiguous interpretation is returned as candidates for engineer selection.
        """

        del request_context
        resolved_request_id = _request_id(request_id)
        try:
            profile = context.company_profile if profile_ref is None else load_profile(profile_ref)
            report = recognize(model, tolerance=profile.tolerance(), profile=profile)
            return FeatureRecognitionToolResponse(
                status=ToolStatus.OK,
                data=report.model_dump(mode="json"),
                request_id=resolved_request_id,
            )
        except Exception as exc:
            return FeatureRecognitionToolResponse.model_validate(
                failure(exc, request_id=resolved_request_id)
            )


__all__ = ["register"]
