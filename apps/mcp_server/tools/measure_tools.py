"""Deterministic measurement tool over revision-pinned drawing models."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context as McpContext
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from apps.mcp_server.context import ServerContext, failure
from apps.mcp_server.tools.permissions import ToolPermissionGuard
from cad_harness.domain.models.drawing_model import DrawingModel
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus
from cad_harness.domain.models.measurement import MeasurementRequest, MeasurementResult
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id


def _request_id(value: str | None) -> str:
    return value or new_id(IdPrefix.REQUEST)


class MeasurementToolResponse(ToolResponse):
    """Envelope whose successful payload is a provenance-carrying measurement."""

    data: MeasurementResult | dict[str, Any] = Field(  # type: ignore[assignment]
        default_factory=dict
    )


def register(mcp: FastMCP, context: ServerContext, guard: ToolPermissionGuard) -> None:

    @guard.tool(mcp)
    def cad_measure(
        request_context: McpContext[Any, Any, Any],
        model: DrawingModel,
        request: MeasurementRequest,
        request_id: str | None = None,
    ) -> MeasurementToolResponse:
        """Measure distances, angles, curves, contours, holes or bounding boxes.

        Measurements are computed only from normalized geometry in the supplied
        DrawingModel and always report the source document revision and tolerance.
        """

        del request_context
        resolved_request_id = _request_id(request_id)
        try:
            result = context.measurement_service.measure(
                model,
                request,
                tolerance=context.tolerance_profile,
            )
            return MeasurementToolResponse(
                status=ToolStatus.OK,
                data=result.model_dump(mode="json"),
                request_id=resolved_request_id,
            )
        except Exception as exc:
            return MeasurementToolResponse.model_validate(
                failure(exc, request_id=resolved_request_id)
            )


__all__ = ["register"]
