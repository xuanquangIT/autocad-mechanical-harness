"""End-to-end image trace -> human acceptance -> ordinary plan workflow."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any, cast

import cv2
import numpy as np
import pytest
from apps.mcp_server.server import create_server

from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.raster import PixelPoint, RasterCalibration, RasterTraceReport


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    monkeypatch.setenv("CAD_HARNESS_ADAPTER", "fake")
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "raster-test-secret")
    monkeypatch.setenv("CAD_HARNESS_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("CAD_HARNESS_SQLITE_PATH", str(tmp_path / "harness.db"))
    monkeypatch.setenv("CAD_HARNESS_LOG_LEVEL", "ERROR")
    return create_server(tmp_path / "missing-config.yaml")


def _line_image_base64() -> str:
    image = np.full((220, 220), 255, dtype=np.uint8)
    cv2.line(image, (20, 60), (180, 60), 0, 2, cv2.LINE_8)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _payload(result: object) -> dict[str, Any]:
    if isinstance(result, tuple):
        return cast(dict[str, Any], result[1])
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


def test_image_tools_require_calibration_and_never_expose_acceptance(
    server: tuple[Any, Any],
) -> None:
    mcp, _ = server
    missing = _payload(
        asyncio.run(
            mcp.call_tool(
                "cad_image_trace",
                {"image_base64": _line_image_base64(), "display_name": "line.png"},
            )
        )
    )
    assert missing["status"] == "needs_input"
    assert {item["path"] for item in missing["missing_inputs"]} == {"calibration"}
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "cad_image_accept" not in names
    assert {"cad_image_inspect", "cad_image_trace", "cad_image_draft"} <= names


def test_signed_raster_draft_compiles_through_the_ordinary_job_pipeline(
    server: tuple[Any, Any],
) -> None:
    mcp, context = server
    calibration = RasterCalibration(
        pixel_a=PixelPoint(x=20.0, y=60.0),
        pixel_b=PixelPoint(x=180.0, y=60.0),
        reference_distance_mm=160.0,
    )
    traced = _payload(
        asyncio.run(
            mcp.call_tool(
                "cad_image_trace",
                {
                    "image_base64": _line_image_base64(),
                    "display_name": "line.png",
                    "calibration": calibration.model_dump(mode="json"),
                },
            )
        )
    )
    assert traced["status"] == "ok"
    report = RasterTraceReport.model_validate(traced["data"])
    assert report.production_ready is False
    proposed = tuple(
        candidate.candidate_id for candidate in report.candidates if candidate.status == "proposed"
    )
    assert proposed

    acceptance_service = context.raster_trace_service
    assert acceptance_service is not None
    acceptance, token = acceptance_service.accept(
        report, proposed, "engineer:test", layer="TRACE_REVIEWED"
    )
    document = context.service.inspect_document()
    drafted = _payload(
        asyncio.run(
            mcp.call_tool(
                "cad_image_draft",
                {
                    "document_id": document.document_id,
                    "report": report.model_dump(mode="json"),
                    "acceptance": acceptance.model_dump(mode="json"),
                    "acceptance_token": token,
                    "layer": "TRACE_REVIEWED",
                },
            )
        )
    )
    assert drafted["status"] == "ok"
    spec = DrawingSpec.model_validate(drafted["data"])
    assert spec.features[0].type == "_accepted_raster_trace"

    job = context.service.create_job(document.document_id)
    submitted = context.service.submit_spec(
        job.job_id,
        spec.model_dump(mode="json", exclude={"spec_id", "document_id"}),
    )
    assert submitted["status"] == "ok"
    plan = context.service.store.get_plan(job.job_id)
    assert plan is not None and plan.operations
    assert {operation.layer for operation in plan.operations} == {"TRACE_REVIEWED"}
    assert all(operation.feature_id in proposed for operation in plan.operations)

    # Raster tool calls emit no audit event containing raw bytes or candidate geometry.
    event_text = str(getattr(context.service.audit, "events", ()))
    assert _line_image_base64() not in event_text
    assert "acceptance_token" not in event_text


def test_invalid_base64_is_a_structured_rejection(server: tuple[Any, Any]) -> None:
    mcp, _ = server
    response = _payload(
        asyncio.run(
            mcp.call_tool(
                "cad_image_inspect",
                {"image_base64": "not+canonical=whitespace\n", "display_name": "bad.png"},
            )
        )
    )
    assert response["status"] == "rejected"
    assert response["error"]["code"] == "UNSUPPORTED_INPUT_FORMAT"
