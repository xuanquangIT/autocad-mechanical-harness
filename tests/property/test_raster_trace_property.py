"""Property 65: calibrated raster traces are deterministic and acceptance-bound."""

from __future__ import annotations

import tempfile
from pathlib import Path

import cv2
import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.comprehension.raster_trace import (
    LocalRasterTracer,
    accept_trace,
    accepted_operations,
)
from cad_harness.domain.models.drawing_model import LineGeometry
from cad_harness.domain.models.operation_plan import OperationType
from cad_harness.domain.models.raster import (
    PixelPoint,
    RasterCalibration,
    RasterCandidateStatus,
    RasterTraceReport,
    RasterVectorCandidate,
)


def _line_png() -> bytes:
    image = np.full((220, 220), 255, dtype=np.uint8)
    cv2.line(image, (20, 60), (180, 60), 0, 2, cv2.LINE_8)
    success, encoded = cv2.imencode(".png", image)
    assert success
    return encoded.tobytes()


_LINE_PNG = _line_png()


def _line_candidate(
    report: RasterTraceReport,
) -> tuple[RasterVectorCandidate, LineGeometry]:
    candidates = [
        (candidate, candidate.geometry)
        for candidate in report.candidates
        if isinstance(candidate.geometry, LineGeometry)
    ]
    assert len(candidates) == 1
    return candidates[0]


# Feature: cad-ai-production-roadmap, Property 65
@given(
    origin_x=st.integers(min_value=-500, max_value=500),
    origin_y=st.integers(min_value=-500, max_value=500),
    reference_mm=st.integers(min_value=20, max_value=500),
    translate_x=st.integers(min_value=-200, max_value=200),
    translate_y=st.integers(min_value=-200, max_value=200),
    scale=st.sampled_from((0.5, 0.75, 1.5, 2.0, 3.0)),
)
@settings(max_examples=24, deadline=None)
def test_calibrated_trace_determinism_transform_and_acceptance_safety(
    origin_x: int,
    origin_y: int,
    reference_mm: int,
    translate_x: int,
    translate_y: int,
    scale: float,
) -> None:
    """**Validates: Requirements 22.1-22.8, 22.11**"""
    calibration = RasterCalibration(
        pixel_a=PixelPoint(x=20.0, y=60.0),
        pixel_b=PixelPoint(x=180.0, y=60.0),
        reference_distance_mm=float(reference_mm),
        origin_mm=(float(origin_x), float(origin_y)),
    )
    transformed_calibration = RasterCalibration(
        pixel_a=calibration.pixel_a,
        pixel_b=calibration.pixel_b,
        reference_distance_mm=float(reference_mm) * scale,
        origin_mm=(float(origin_x + translate_x), float(origin_y + translate_y)),
    )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        first_tracer = LocalRasterTracer(root / "first", confidence_threshold=0.72)
        second_tracer = LocalRasterTracer(root / "second", confidence_threshold=0.72)
        first = first_tracer.trace(
            _LINE_PNG,
            display_name="property-line.png",
            calibration=calibration,
        )
        second = second_tracer.trace(
            _LINE_PNG,
            display_name="property-line.png",
            calibration=calibration,
        )

        assert first == second
        assert first_tracer.resolve_overlay_path(first).read_bytes() == (
            second_tracer.resolve_overlay_path(second).read_bytes()
        )
        candidate, candidate_geometry = _line_candidate(first)
        assert candidate.status is RasterCandidateStatus.PROPOSED
        acceptance = accept_trace(
            first,
            accepted_candidate_ids=(candidate.candidate_id,),
            accepted_by="property-engineer",
        )
        operation = accepted_operations(first, acceptance)[0]
        assert operation.type is OperationType.CREATE_LINE
        assert operation.geometry == {
            "start_mm": list(candidate_geometry.start_mm),
            "end_mm": list(candidate_geometry.end_mm),
        }

        transformed = LocalRasterTracer(root / "transformed").trace(
            _LINE_PNG,
            display_name="property-line.png",
            calibration=transformed_calibration,
        )
        transformed_candidate, transformed_geometry = _line_candidate(transformed)
        old_origin = calibration.origin_mm
        new_origin = transformed_calibration.origin_mm
        for original, changed in zip(
            (candidate_geometry.start_mm, candidate_geometry.end_mm),
            (
                transformed_geometry.start_mm,
                transformed_geometry.end_mm,
            ),
            strict=True,
        ):
            expected = (
                new_origin[0] + scale * (original[0] - old_origin[0]),
                new_origin[1] + scale * (original[1] - old_origin[1]),
            )
            assert changed == pytest.approx(expected, abs=1.0e-7)
        assert transformed.trace_digest != first.trace_digest
        assert transformed_candidate.candidate_id != candidate.candidate_id

        tampered_acceptance = acceptance.model_copy(update={"trace_digest": "sha256:" + "f" * 64})
        with pytest.raises(ValueError, match="does not match"):
            accepted_operations(first, tampered_acceptance)

        strict = LocalRasterTracer(root / "strict", confidence_threshold=1.0).trace(
            _LINE_PNG,
            display_name="property-line.png",
            calibration=calibration,
        )
        non_proposed, _ = _line_candidate(strict)
        assert non_proposed.status is RasterCandidateStatus.AMBIGUOUS
        with pytest.raises(ValueError, match="only proposed"):
            accept_trace(
                strict,
                accepted_candidate_ids=(non_proposed.candidate_id,),
                accepted_by="property-engineer",
            )
