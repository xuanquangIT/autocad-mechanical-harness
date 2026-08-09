"""Golden roundtrip from clean raster primitives to a hashed operation plan."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from cad_harness.comprehension.raster_trace import (
    LocalRasterTracer,
    accept_trace,
    accepted_operations,
)
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.raster import PixelPoint, RasterCalibration, RasterCandidateStatus


def _calibration() -> RasterCalibration:
    return RasterCalibration(
        pixel_a=PixelPoint(x=20.0, y=60.0),
        pixel_b=PixelPoint(x=180.0, y=60.0),
        reference_distance_mm=160.0,
        origin_mm=(10.0, 20.0),
    )


def _image(kind: str) -> np.ndarray[tuple[int, int], np.dtype[np.uint8]]:
    image = np.full((220, 220), 255, dtype=np.uint8)
    if kind == "line":
        cv2.line(image, (20, 60), (180, 60), 0, 2, cv2.LINE_8)
    elif kind == "circle":
        cv2.circle(image, (110, 110), 55, 0, 2, cv2.LINE_8)
    elif kind == "arc":
        cv2.ellipse(image, (110, 110), (60, 60), 0, 20, 150, 0, 2, cv2.LINE_8)
    elif kind == "polyline":
        vertices = np.array([[30, 30], [180, 40], [170, 170], [40, 180]], np.int32)
        cv2.polylines(image, [vertices], True, 0, 2, cv2.LINE_8)
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(kind)
    return image


def _encode(kind: str) -> bytes:
    success, encoded = cv2.imencode(".png", _image(kind))
    assert success
    return encoded.tobytes()


def _accepted_operation(tmp_path: Path, kind: str, geometry_kind: str) -> Operation:
    report = LocalRasterTracer(tmp_path / kind).trace(
        _encode(kind),
        display_name=f"{kind}.png",
        calibration=_calibration(),
    )
    candidates = [
        candidate
        for candidate in report.candidates
        if candidate.geometry.kind == geometry_kind
        and candidate.status is RasterCandidateStatus.PROPOSED
    ]
    assert len(candidates) == 1
    acceptance = accept_trace(
        report,
        accepted_candidate_ids=(candidates[0].candidate_id,),
        accepted_by="golden-engineer",
    )
    operations = accepted_operations(report, acceptance, layer="RASTER_GOLDEN")
    assert len(operations) == 1
    return operations[0]


@pytest.mark.golden
def test_raster_primitives_roundtrip_through_hashed_operation_plan(tmp_path: Path) -> None:
    cases = (
        ("line", "line", OperationType.CREATE_LINE, {"start_mm", "end_mm"}),
        ("circle", "circle", OperationType.CREATE_CIRCLE, {"center_mm", "radius_mm"}),
        (
            "arc",
            "arc",
            OperationType.CREATE_ARC,
            {"center_mm", "radius_mm", "start_angle_deg", "end_angle_deg"},
        ),
        (
            "polyline",
            "polyline",
            OperationType.CREATE_CLOSED_POLYLINE,
            {"vertices_mm"},
        ),
    )
    operations = tuple(
        _accepted_operation(tmp_path, kind, geometry_kind) for kind, geometry_kind, _, _ in cases
    )

    assert [operation.type for operation in operations] == [case[2] for case in cases]
    assert [set(operation.geometry) for operation in operations] == [case[3] for case in cases]
    assert all(operation.layer == "RASTER_GOLDEN" for operation in operations)
    assert len({operation.operation_id for operation in operations}) == len(cases)

    line = operations[0]
    assert line.geometry["start_mm"] == pytest.approx([10.0, 20.0], abs=2.0)
    assert line.geometry["end_mm"] == pytest.approx([170.0, 20.0], abs=2.0)
    circle = operations[1]
    assert circle.geometry["center_mm"] == pytest.approx([100.0, -30.0], abs=2.0)
    assert circle.geometry["radius_mm"] == pytest.approx(55.0, abs=2.0)
    assert len(operations[3].geometry["vertices_mm"]) == 4

    plan = OperationPlan(
        plan_id="plan:raster-golden",
        job_id="job:raster-golden",
        document_id="document:raster-golden",
        expected_revision="revision:1",
        profile_ref="profile:raster-golden",
        operations=operations,
    ).with_hash()
    restored = OperationPlan.model_validate_json(plan.model_dump_json())

    assert restored == plan
    assert restored.plan_hash is not None
    assert restored.verify_hash(restored.plan_hash)
    assert (
        restored.model_dump(mode="json")["operations"] == plan.model_dump(mode="json")["operations"]
    )
