"""Local-only raster inspection, calibrated tracing and sealed draft creation."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from mcp.server.fastmcp import Context as McpContext
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from apps.mcp_server.context import ServerContext, failure
from apps.mcp_server.tools.permissions import ToolPermissionGuard
from cad_harness.domain.errors import ApprovalRequiredError, UnsupportedInputFormatError
from cad_harness.domain.models.drawing_spec import (
    Annotations,
    Assumption,
    DrawingIntent,
    DrawingSpec,
    FeatureSpec,
    StandardProfileRef,
)
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus
from cad_harness.domain.models.raster import (
    RasterCalibration,
    RasterTraceAcceptance,
    RasterTraceReport,
)
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id


class RasterTraceToolResponse(ToolResponse):
    """Successful payload is the review-only calibrated trace contract."""

    data: RasterTraceReport | dict[str, Any] = Field(default_factory=dict)  # type: ignore[assignment]


class RasterDraftToolResponse(ToolResponse):
    """Successful payload is a DrawingSpec sealed to an engineer acceptance."""

    data: DrawingSpec | dict[str, Any] = Field(default_factory=dict)  # type: ignore[assignment]


def _request_id(value: str | None) -> str:
    return value or new_id(IdPrefix.REQUEST)


def _decode_image(image_base64: str, *, max_bytes: int) -> bytes:
    """Reject oversized/non-canonical base64 before allocating decoded pixels."""
    maximum_encoded = ((max_bytes + 2) // 3) * 4
    if not image_base64 or len(image_base64) > maximum_encoded:
        raise UnsupportedInputFormatError(
            "Raster payload exceeds the configured byte limit",
            required_action="Submit a smaller local PNG, JPEG, or TIFF image",
            details={"maximum_bytes": max_bytes},
        )
    try:
        payload = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UnsupportedInputFormatError(
            "Raster payload is not canonical base64",
            required_action="Base64-encode the local image without whitespace",
        ) from exc
    if not payload or len(payload) > max_bytes:
        raise UnsupportedInputFormatError(
            "Raster payload is empty or exceeds the configured byte limit",
            required_action="Submit a bounded local PNG, JPEG, or TIFF image",
            details={"maximum_bytes": max_bytes},
        )
    return payload


def _trace(
    context: ServerContext,
    image_base64: str,
    display_name: str,
    calibration: RasterCalibration | None,
) -> RasterTraceReport:
    payload = _decode_image(image_base64, max_bytes=context.settings.raster.max_bytes)
    if context.raster_trace_service is not None:
        return context.raster_trace_service.trace(payload, display_name, calibration)
    try:
        return context.raster_tracer.trace(
            payload,
            display_name=display_name,
            calibration=calibration,
        )
    except (TypeError, ValueError) as exc:
        raise UnsupportedInputFormatError(
            "Raster payload could not be decoded within the configured safety limits",
            required_action="Submit a valid bounded PNG, JPEG, or TIFF image",
        ) from exc


def register(mcp: FastMCP, context: ServerContext, guard: ToolPermissionGuard) -> None:

    @guard.tool(mcp)
    def cad_image_inspect(
        request_context: McpContext[Any, Any, Any],
        image_base64: str,
        display_name: str,
        request_id: str | None = None,
    ) -> RasterTraceToolResponse:
        """Inspect a bounded local PNG/JPEG/TIFF and return review-only pixel candidates.

        This call never produces production-ready millimetre geometry. Raw image bytes are
        neither persisted nor written to logs or audit events.
        """

        del request_context
        resolved_request_id = _request_id(request_id)
        try:
            report = _trace(context, image_base64, display_name, None)
            return RasterTraceToolResponse(
                status=ToolStatus.OK,
                data=report,
                request_id=resolved_request_id,
            )
        except Exception as exc:
            return RasterTraceToolResponse.model_validate(
                failure(exc, request_id=resolved_request_id)
            )

    @guard.tool(mcp)
    def cad_image_trace(
        request_context: McpContext[Any, Any, Any],
        image_base64: str,
        display_name: str,
        calibration: RasterCalibration,
        request_id: str | None = None,
    ) -> RasterTraceToolResponse:
        """Trace a local image with engineer-supplied pixel-to-millimetre calibration.

        The result contains proposed, ambiguous and rejected candidates plus an opaque
        overlay reference. It is not permission to create or commit CAD entities.
        """

        del request_context
        resolved_request_id = _request_id(request_id)
        try:
            report = _trace(context, image_base64, display_name, calibration)
            return RasterTraceToolResponse(
                status=ToolStatus.OK,
                data=report,
                request_id=resolved_request_id,
            )
        except Exception as exc:
            return RasterTraceToolResponse.model_validate(
                failure(exc, request_id=resolved_request_id)
            )

    @guard.tool(mcp)
    def cad_image_draft(
        request_context: McpContext[Any, Any, Any],
        document_id: str,
        report: RasterTraceReport,
        acceptance: RasterTraceAcceptance,
        acceptance_token: str,
        layer: str,
        request_id: str | None = None,
    ) -> RasterDraftToolResponse:
        """Create a draft DrawingSpec from a separately signed engineer acceptance.

        This tool does not write a drawing or register a job. Submit the returned spec
        through cad_spec_submit, then use the ordinary preview, validation, human approval,
        commit and post-readback gates. Acceptance tokens are issued only by the local
        engineer review surface, never by an MCP tool.
        """

        del request_context
        resolved_request_id = _request_id(request_id)
        try:
            service = context.raster_trace_service
            if service is None:
                raise ApprovalRequiredError(
                    "Raster drafting requires a configured local approval secret",
                    required_action=(
                        "Set CAD_HARNESS_APPROVAL_SECRET, restart the local harness, and "
                        "obtain a fresh engineer raster acceptance"
                    ),
                )
            # Verify before returning a sealed spec. The compiler repeats this check so a
            # caller cannot forge this internal feature through cad_spec_submit.
            service.draft_operations(report, acceptance, acceptance_token, layer=layer)
            profile = StandardProfileRef(
                profile_id=context.company_profile.profile_id,
                version=context.company_profile.version,
            )
            spec = DrawingSpec(
                spec_id=new_id(IdPrefix.SPEC),
                document_id=document_id,
                standard_profile=profile,
                drawing=DrawingIntent(),
                features=(
                    FeatureSpec(
                        feature_id=f"accepted-{report.trace_id}",
                        type="_accepted_raster_trace",
                        parameters={
                            "report": report.model_dump(mode="json"),
                            "acceptance": acceptance.model_dump(mode="json"),
                            "acceptance_token": acceptance_token,
                            "layer": layer,
                        },
                    ),
                ),
                annotations=Annotations(dimensions="none"),
                assumptions=(
                    Assumption(
                        path="features[0]",
                        statement=(
                            "This is an uncertified reconstruction from a calibrated image; "
                            "dimensions, tolerance, material and design intent were not inferred"
                        ),
                        affects_geometry=True,
                        requires_approval=True,
                    ),
                ),
            )
            return RasterDraftToolResponse(
                status=ToolStatus.OK,
                data=spec,
                request_id=resolved_request_id,
                warnings=("image_derived_geometry_requires_cad_readback",),
            )
        except Exception as exc:
            return RasterDraftToolResponse.model_validate(
                failure(exc, request_id=resolved_request_id)
            )


__all__ = ["register"]
