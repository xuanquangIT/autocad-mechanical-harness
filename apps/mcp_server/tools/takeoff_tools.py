"""Material take-off creation and allowlisted export tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import Context as McpContext
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from apps.mcp_server.context import ServerContext, failure
from apps.mcp_server.tools.permissions import ToolPermissionGuard
from cad_harness.domain.models.base import ContractModel
from cad_harness.domain.models.drawing_model import DrawingModel
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus
from cad_harness.domain.models.takeoff import TakeoffReport, TakeoffRequest
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


class TakeoffToolResponse(ToolResponse):
    """Envelope whose successful payload is a traceable take-off report."""

    data: TakeoffReport | dict[str, Any] = Field(  # type: ignore[assignment]
        default_factory=dict
    )


class TakeoffExportData(ContractModel):
    artifact_ref: str
    format: Literal["json", "csv"]


class TakeoffExportToolResponse(ToolResponse):
    """Envelope for an allowlisted take-off artifact."""

    data: TakeoffExportData | dict[str, Any] = Field(  # type: ignore[assignment]
        default_factory=dict
    )


def register(mcp: FastMCP, context: ServerContext, guard: ToolPermissionGuard) -> None:

    @guard.tool(mcp)
    def cad_takeoff(
        request_context: McpContext[Any, Any, Any],
        model: DrawingModel,
        request: TakeoffRequest,
        request_id: str | None = None,
    ) -> TakeoffToolResponse:
        """Compute a traceable BOM and fabrication take-off from a DrawingModel.

        Thickness, material and quantity must be supplied explicitly. Every reported
        quantity retains its source entity references and drawing revision.
        """

        resolved_request_id = _request_id(request_id)
        try:
            report = context.takeoff_service.create(
                model,
                request,
                tolerance=context.tolerance_profile,
                actor_id=_actor_id(request_context),
            )
            return TakeoffToolResponse(
                status=ToolStatus.OK,
                data=report.model_dump(mode="json"),
                request_id=resolved_request_id,
            )
        except Exception as exc:
            return TakeoffToolResponse.model_validate(failure(exc, request_id=resolved_request_id))

    @guard.tool(mcp)
    def cad_takeoff_export(
        request_context: McpContext[Any, Any, Any],
        report: TakeoffReport,
        target_path: str,
        format: Literal["json", "csv"],
        overwrite: bool = False,
        request_id: str | None = None,
    ) -> TakeoffExportToolResponse:
        """Export a take-off report to an allowlisted JSON or CSV destination.

        This tool writes a file but never changes DWG content. It is therefore in the
        approval-required client profile and refuses overwrite unless explicit.
        """

        resolved_request_id = _request_id(request_id)
        try:
            exported = context.takeoff_service.export(
                report,
                Path(target_path),
                format=format,
                overwrite=overwrite,
            )
            artifact_ref = redact_path(exported)
            audit_event_id = context.service.audit.append(
                event_type=AuditEventType.EXPORT_CREATED.value,
                job_id=None,
                actor_type="ai_client",
                actor_id=_actor_id(request_context),
                payload={
                    "document_id": report.document_id,
                    "revision": report.revision,
                    "artifact_ref": artifact_ref,
                    "format": format,
                    "report_type": "takeoff",
                },
            )
            return TakeoffExportToolResponse(
                status=ToolStatus.OK,
                data={"artifact_ref": artifact_ref, "format": format},
                request_id=resolved_request_id,
                audit_event_id=audit_event_id,
            )
        except Exception as exc:
            return TakeoffExportToolResponse.model_validate(
                failure(exc, request_id=resolved_request_id)
            )


__all__ = ["register"]
