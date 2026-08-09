from __future__ import annotations

import struct
from pathlib import Path

import cv2
import numpy as np
import pytest

from cad_harness.comprehension.raster_trace import (
    LocalRasterTracer,
    RasterTraceLimits,
    accept_trace,
    accepted_operations,
    verify_acceptance,
)
from cad_harness.domain.models.operation_plan import OperationType
from cad_harness.domain.models.raster import (
    PixelPoint,
    RasterCalibration,
    RasterCandidateStatus,
    RasterFormat,
)


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
    elif kind == "rejected":
        vertices = np.array(
            [(10 + index * 10, 60 + (index % 2) * 100) for index in range(20)],
            np.int32,
        )
        cv2.polylines(image, [vertices], False, 0, 2, cv2.LINE_8)
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(kind)
    return image


def _encode(kind: str, extension: str = ".png") -> bytes:
    success, encoded = cv2.imencode(extension, _image(kind))
    assert success
    return encoded.tobytes()


@pytest.mark.parametrize(
    ("extension", "expected_format"),
    [
        (".png", RasterFormat.PNG),
        (".jpg", RasterFormat.JPEG),
        (".tiff", RasterFormat.TIFF),
    ],
)
def test_magic_bytes_source_digest_and_local_opaque_overlay(
    tmp_path: Path,
    extension: str,
    expected_format: RasterFormat,
) -> None:
    payload = _encode("line", extension)
    tracer = LocalRasterTracer(tmp_path / "explicit-output")
    report = tracer.trace(
        payload,
        display_name=f"source{extension}",
        calibration=_calibration(),
    )

    assert report.source.format is expected_format
    assert report.source.width_px == 220
    assert report.source.height_px == 220
    assert report.source.byte_size == len(payload)
    assert report.source.source_sha256.startswith("sha256:")
    assert report.overlay_artifact_ref == f"raster-overlay:{report.trace_id}.svg"
    overlay = tracer.resolve_overlay_path(report)
    assert overlay.is_file()
    assert "<svg" in overlay.read_text(encoding="utf-8")
    assert str(tmp_path) not in report.model_dump_json()
    assert report.requires_engineer_review is True
    assert report.production_ready is False


def test_invalid_magic_byte_and_predecode_resource_limits_fail_closed(tmp_path: Path) -> None:
    payload = _encode("line")
    tracer = LocalRasterTracer(tmp_path)

    with pytest.raises(ValueError, match="magic bytes"):
        tracer.trace(b"not-an-image", display_name="source.png", calibration=_calibration())
    with pytest.raises(ValueError, match="byte size"):
        LocalRasterTracer(
            tmp_path / "bytes",
            limits=RasterTraceLimits(max_bytes=len(payload) - 1),
        ).trace(payload, display_name="source.png", calibration=_calibration())
    with pytest.raises(ValueError, match="pixel count"):
        LocalRasterTracer(
            tmp_path / "pixels",
            limits=RasterTraceLimits(max_pixels=220 * 220 - 1),
        ).trace(payload, display_name="source.png", calibration=_calibration())

    forged_header = bytearray(payload)
    forged_header[16:20] = struct.pack(">I", 100_000)
    with pytest.raises(ValueError, match="dimension"):
        tracer.trace(
            bytes(forged_header),
            display_name="forged.png",
            calibration=_calibration(),
        )
    with pytest.raises(ValueError, match="basename"):
        tracer.trace(payload, display_name="private/source.png", calibration=_calibration())


@pytest.mark.parametrize(
    ("kind", "geometry_kind", "operation_type"),
    [
        ("line", "line", OperationType.CREATE_LINE),
        ("circle", "circle", OperationType.CREATE_CIRCLE),
        ("arc", "arc", OperationType.CREATE_ARC),
        ("polyline", "polyline", OperationType.CREATE_CLOSED_POLYLINE),
    ],
)
def test_clean_primitives_have_calibrated_fit_evidence_and_draft_operations(
    tmp_path: Path,
    kind: str,
    geometry_kind: str,
    operation_type: OperationType,
) -> None:
    report = LocalRasterTracer(tmp_path / kind).trace(
        _encode(kind),
        display_name=f"{kind}.png",
        calibration=_calibration(),
    )
    matching = [
        candidate for candidate in report.candidates if candidate.geometry.kind == geometry_kind
    ]
    assert len(matching) == 1
    candidate = matching[0]
    assert candidate.status is RasterCandidateStatus.PROPOSED
    assert candidate.confidence >= report.confidence_threshold
    assert candidate.fit_error_px >= 0.0
    assert candidate.support_pixels >= 12
    assert candidate.evidence_bbox_px[2] > candidate.evidence_bbox_px[0]

    acceptance = accept_trace(
        report,
        accepted_candidate_ids=(candidate.candidate_id,),
        accepted_by="engineer@example.com",
    )
    assert verify_acceptance(report, acceptance) == (candidate,)
    operations = accepted_operations(report, acceptance, layer="TRACE_REVIEWED")
    assert len(operations) == 1
    assert operations[0].type is operation_type
    assert operations[0].feature_id == candidate.candidate_id


def test_calibration_maps_a_to_origin_a_to_b_to_positive_x_and_pixel_up_to_positive_y(
    tmp_path: Path,
) -> None:
    report = LocalRasterTracer(tmp_path).trace(
        _encode("line"),
        display_name="axis.png",
        calibration=_calibration(),
    )
    line = next(
        candidate.geometry for candidate in report.candidates if candidate.geometry.kind == "line"
    )
    assert line.start_mm == pytest.approx((10.0, 20.0), abs=2.0)
    assert line.end_mm == pytest.approx((170.0, 20.0), abs=2.0)

    vertical = np.full((220, 220), 255, dtype=np.uint8)
    cv2.line(vertical, (20, 20), (20, 60), 0, 2, cv2.LINE_8)
    success, encoded = cv2.imencode(".png", vertical)
    assert success
    vertical_report = LocalRasterTracer(tmp_path / "vertical").trace(
        encoded.tobytes(),
        display_name="vertical.png",
        calibration=_calibration(),
    )
    geometry = next(
        candidate.geometry
        for candidate in vertical_report.candidates
        if candidate.geometry.kind == "line"
    )
    assert max(geometry.start_mm[1], geometry.end_mm[1]) > 55.0


def test_trace_identity_is_deterministic_and_acceptance_is_fully_bound(tmp_path: Path) -> None:
    payload = _encode("circle")
    tracer = LocalRasterTracer(tmp_path)
    first = tracer.trace(payload, display_name="circle.png", calibration=_calibration())
    first_overlay = tracer.resolve_overlay_path(first).read_bytes()
    second = tracer.trace(payload, display_name="circle.png", calibration=_calibration())

    assert second == first
    assert tracer.resolve_overlay_path(second).read_bytes() == first_overlay
    assert [item.candidate_id for item in first.candidates] == [
        item.candidate_id for item in second.candidates
    ]
    proposed = next(
        candidate
        for candidate in first.candidates
        if candidate.status is RasterCandidateStatus.PROPOSED
    )
    acceptance = accept_trace(
        first,
        accepted_candidate_ids=(proposed.candidate_id,),
        accepted_by="engineer@example.com",
    )
    changed_source = acceptance.model_copy(update={"source_sha256": "sha256:" + "0" * 64})
    with pytest.raises(ValueError, match="does not match"):
        verify_acceptance(first, changed_source)
    changed_digest = acceptance.model_copy(update={"trace_digest": "sha256:" + "1" * 64})
    with pytest.raises(ValueError, match="does not match"):
        verify_acceptance(first, changed_digest)
    tampered_candidate = proposed.model_copy(update={"confidence": 0.0})
    tampered_report = first.model_copy(update={"candidates": (tampered_candidate,)})
    with pytest.raises(ValueError, match="digest or identity"):
        accept_trace(
            tampered_report,
            accepted_candidate_ids=(tampered_candidate.candidate_id,),
            accepted_by="engineer@example.com",
        )


def test_uncalibrated_inspection_is_ambiguous_and_production_acceptance_fails(
    tmp_path: Path,
) -> None:
    report = LocalRasterTracer(tmp_path).trace(
        _encode("line"),
        display_name="uncalibrated.png",
    )
    assert report.calibration is None
    assert report.candidates
    assert all(
        candidate.status is RasterCandidateStatus.AMBIGUOUS for candidate in report.candidates
    )
    assert "calibration_required_before_production_acceptance" in report.warnings
    with pytest.raises(ValueError, match="calibrated"):
        accept_trace(
            report,
            accepted_candidate_ids=(report.candidates[0].candidate_id,),
            accepted_by="engineer@example.com",
        )


def test_unsupported_component_is_retained_as_rejected_evidence(tmp_path: Path) -> None:
    report = LocalRasterTracer(tmp_path).trace(
        _encode("rejected"),
        display_name="zigzag.png",
        calibration=_calibration(),
    )
    rejected = [
        candidate
        for candidate in report.candidates
        if candidate.status is RasterCandidateStatus.REJECTED
    ]
    assert rejected
    assert "fit_rejected" in rejected[0].warnings
    with pytest.raises(ValueError, match="proposed"):
        accept_trace(
            report,
            accepted_candidate_ids=(rejected[0].candidate_id,),
            accepted_by="engineer@example.com",
        )
