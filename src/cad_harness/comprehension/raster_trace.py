"""Deterministic, local-only raster-to-vector tracing.

The tracer deliberately produces review candidates rather than pretending that image
recognition is CAD truth.  Pixels are decoded locally with OpenCV, every accepted result is
bound to the source digest, and no filesystem path is returned in the wire report.
"""

from __future__ import annotations

import html
import math
import os
import struct
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from cad_harness.domain.canonical import canonical_json
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    CircleGeometry,
    LineGeometry,
    PolylineGeometry,
    PolylineVertex,
)
from cad_harness.domain.models.operation_plan import Operation, OperationType
from cad_harness.domain.models.raster import (
    RasterCalibration,
    RasterCandidateStatus,
    RasterFormat,
    RasterSource,
    RasterTraceAcceptance,
    RasterTraceReport,
    RasterVectorCandidate,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_TIFF_LE_MAGIC = b"II*\x00"
_TIFF_BE_MAGIC = b"MM\x00*"
_OVERLAY_PREFIX = "raster-overlay:"
_MIN_COMPONENT_PIXELS = 12
_ALGORITHM_VERSION = "opencv-components-v1"
_ALGORITHM_IDENTITY = f"{_ALGORITHM_VERSION}:opencv-{cv2.__version__}"


@dataclass(frozen=True, slots=True)
class RasterTraceLimits:
    """Hard resource bounds applied before OpenCV allocates the decoded raster."""

    max_bytes: int = 16 * 1024 * 1024
    max_pixels: int = 20_000_000
    max_dimension_px: int = 20_000

    def __post_init__(self) -> None:
        if self.max_bytes <= 0 or self.max_pixels <= 0 or self.max_dimension_px <= 0:
            raise ValueError("raster trace limits must be positive")


@dataclass(frozen=True, slots=True)
class _DetectedShape:
    kind: str
    values: tuple[Any, ...]
    confidence: float
    fit_error_px: float
    support_pixels: int
    bbox: tuple[float, float, float, float]
    warnings: tuple[str, ...] = ()


class LocalRasterTracer:
    """Bounded OpenCV tracer whose only write is a deterministic SVG overlay."""

    def __init__(
        self,
        output_root: Path,
        *,
        limits: RasterTraceLimits | None = None,
        confidence_threshold: float = 0.72,
    ) -> None:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be between zero and one")
        root = Path(output_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise ValueError("output_root must resolve to a directory")
        self._output_root = root
        self._limits = limits or RasterTraceLimits()
        self._confidence_threshold = confidence_threshold

    def trace(
        self,
        payload: bytes,
        *,
        display_name: str,
        calibration: RasterCalibration | None = None,
    ) -> RasterTraceReport:
        """Inspect one in-memory PNG/JPEG/TIFF and emit deterministic review candidates."""
        if not isinstance(payload, bytes | bytearray | memoryview):
            raise TypeError("payload must be bytes-like")
        data = bytes(payload)
        if not data or len(data) > self._limits.max_bytes:
            raise ValueError("raster byte size is outside the configured limit")
        if (
            Path(display_name).name != display_name
            or not display_name.strip()
            or any(separator in display_name for separator in ("/", "\\"))
        ):
            raise ValueError("display_name must be a non-empty basename")

        raster_format, declared_width, declared_height = _read_image_header(data)
        _validate_dimensions(declared_width, declared_height, self._limits)
        encoded = np.frombuffer(data, dtype=np.uint8)
        cv2.setNumThreads(1)
        cv2.setRNGSeed(0)
        cv2.ocl.setUseOpenCL(False)
        decoded = cv2.imdecode(
            encoded,
            cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION,
        )
        image = cast(NDArray[np.uint8] | None, decoded)
        if image is None or image.ndim != 2:
            raise ValueError("OpenCV could not decode the declared raster")
        height, width = (int(image.shape[0]), int(image.shape[1]))
        if (width, height) != (declared_width, declared_height):
            raise ValueError("decoded dimensions do not match the raster header")

        source_digest = f"sha256:{sha256(data).hexdigest()}"
        source = RasterSource(
            source_sha256=source_digest,
            format=raster_format,
            byte_size=len(data),
            width_px=width,
            height_px=height,
            display_name=display_name,
        )
        detected = _detect_shapes(image)
        candidates = tuple(self._candidate(source_digest, shape, calibration) for shape in detected)
        report_warnings: list[str] = []
        if calibration is None:
            report_warnings.append("calibration_required_before_production_acceptance")
        if not candidates:
            report_warnings.append("no_trace_candidates_detected")

        trace_digest = _trace_digest(
            source_digest,
            calibration,
            candidates,
            self._confidence_threshold,
            tuple(report_warnings),
        )
        trace_id = f"raster-trace-{trace_digest.removeprefix('sha256:')[:24]}"
        artifact_ref = f"{_OVERLAY_PREFIX}{trace_id}.svg"
        overlay = _overlay_svg(width, height, source_digest, candidates, detected)
        self._write_overlay(trace_id, overlay)
        return RasterTraceReport(
            trace_id=trace_id,
            source=source,
            calibration=calibration,
            candidates=candidates,
            overlay_artifact_ref=artifact_ref,
            trace_digest=trace_digest,
            confidence_threshold=self._confidence_threshold,
            warnings=tuple(report_warnings),
        )

    def resolve_overlay_path(self, report: RasterTraceReport) -> Path:
        """Resolve this tracer's opaque overlay reference without trusting it as a path."""
        expected = f"{_OVERLAY_PREFIX}{report.trace_id}.svg"
        if report.overlay_artifact_ref != expected:
            raise ValueError("overlay artifact reference is not bound to this trace")
        target = (self._output_root / f"{report.trace_id}.svg").resolve()
        if target.parent != self._output_root:
            raise ValueError("overlay artifact escaped the configured output root")
        return target

    def _candidate(
        self,
        source_digest: str,
        shape: _DetectedShape,
        calibration: RasterCalibration | None,
    ) -> RasterVectorCandidate:
        geometry = _geometry(shape, calibration)
        evidence_bbox = cast(
            tuple[float, float, float, float],
            tuple(round(value, 6) for value in shape.bbox),
        )
        fit_error = round(max(0.0, shape.fit_error_px), 9)
        candidate_id = _candidate_identifier(
            source_digest,
            geometry,
            evidence_bbox,
            fit_error,
            shape.support_pixels,
        )
        warnings = list(shape.warnings)
        if calibration is None:
            status = RasterCandidateStatus.AMBIGUOUS
            warnings.append("coordinates_are_uncalibrated_pixel_proxies")
        elif shape.confidence >= self._confidence_threshold:
            status = RasterCandidateStatus.PROPOSED
        elif shape.confidence >= self._confidence_threshold * 0.6:
            status = RasterCandidateStatus.AMBIGUOUS
            warnings.append("confidence_below_proposal_threshold")
        else:
            status = RasterCandidateStatus.REJECTED
            warnings.append("fit_rejected")
        return RasterVectorCandidate(
            candidate_id=candidate_id,
            geometry=geometry,
            confidence=round(min(1.0, max(0.0, shape.confidence)), 9),
            fit_error_px=fit_error,
            support_pixels=shape.support_pixels,
            evidence_bbox_px=evidence_bbox,
            status=status,
            warnings=tuple(warnings),
        )

    def _write_overlay(self, trace_id: str, contents: str) -> None:
        target = (self._output_root / f"{trace_id}.svg").resolve()
        if target.parent != self._output_root:
            raise ValueError("overlay target escaped the configured output root")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{trace_id}.", suffix=".tmp", dir=self._output_root
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except BaseException:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            finally:
                raise


def accept_trace(
    report: RasterTraceReport,
    *,
    accepted_candidate_ids: tuple[str, ...],
    accepted_by: str,
) -> RasterTraceAcceptance:
    """Create a source/digest-bound acceptance; uncalibrated traces fail closed."""
    _require_report_integrity(report)
    if report.calibration is None:
        raise ValueError("a raster trace must be calibrated before production acceptance")
    if not accepted_candidate_ids or len(set(accepted_candidate_ids)) != len(
        accepted_candidate_ids
    ):
        raise ValueError("accepted candidate ids must be non-empty and unique")
    by_id = {candidate.candidate_id: candidate for candidate in report.candidates}
    for candidate_id in accepted_candidate_ids:
        candidate = by_id.get(candidate_id)
        if candidate is None:
            raise ValueError("acceptance names a candidate outside the bound trace")
        if candidate.status is not RasterCandidateStatus.PROPOSED:
            raise ValueError("only proposed candidates may be accepted")
    return RasterTraceAcceptance(
        trace_id=report.trace_id,
        trace_digest=report.trace_digest,
        source_sha256=report.source.source_sha256,
        accepted_candidate_ids=accepted_candidate_ids,
        accepted_by=accepted_by,
    )


def verify_acceptance(
    report: RasterTraceReport,
    acceptance: RasterTraceAcceptance,
) -> tuple[RasterVectorCandidate, ...]:
    """Return accepted candidates only when every signed binding still matches."""
    _require_report_integrity(report)
    if report.calibration is None:
        raise ValueError("uncalibrated raster traces cannot enter production")
    if (
        acceptance.trace_id != report.trace_id
        or acceptance.trace_digest != report.trace_digest
        or acceptance.source_sha256 != report.source.source_sha256
    ):
        raise ValueError("raster acceptance does not match the source trace")
    by_id = {candidate.candidate_id: candidate for candidate in report.candidates}
    accepted: list[RasterVectorCandidate] = []
    for candidate_id in acceptance.accepted_candidate_ids:
        candidate = by_id.get(candidate_id)
        if candidate is None or candidate.status is not RasterCandidateStatus.PROPOSED:
            raise ValueError("accepted raster candidate is missing or no longer proposed")
        accepted.append(candidate)
    return tuple(accepted)


def _candidate_identifier(
    source_digest: str,
    geometry: LineGeometry | CircleGeometry | ArcGeometry | PolylineGeometry,
    bbox: tuple[float, float, float, float],
    fit_error_px: float,
    support_pixels: int,
) -> str:
    identity = {
        "algorithm_version": _ALGORITHM_IDENTITY,
        "source_sha256": source_digest,
        "geometry": geometry.model_dump(mode="json"),
        "bbox": bbox,
        "fit_error_px": fit_error_px,
        "support_pixels": support_pixels,
    }
    digest = sha256(canonical_json(identity).encode()).hexdigest()
    return f"raster-candidate-{digest[:24]}"


def _trace_digest(
    source_digest: str,
    calibration: RasterCalibration | None,
    candidates: tuple[RasterVectorCandidate, ...],
    confidence_threshold: float,
    warnings: tuple[str, ...],
) -> str:
    material = {
        "algorithm_version": _ALGORITHM_IDENTITY,
        "source_sha256": source_digest,
        "calibration": calibration.model_dump(mode="json") if calibration is not None else None,
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        "confidence_threshold": confidence_threshold,
        "warnings": list(warnings),
    }
    return f"sha256:{sha256(canonical_json(material).encode()).hexdigest()}"


def _require_report_integrity(report: RasterTraceReport) -> None:
    expected_digest = _trace_digest(
        report.source.source_sha256,
        report.calibration,
        report.candidates,
        report.confidence_threshold,
        report.warnings,
    )
    expected_trace_id = f"raster-trace-{expected_digest.removeprefix('sha256:')[:24]}"
    if (
        report.trace_digest != expected_digest
        or report.trace_id != expected_trace_id
        or report.overlay_artifact_ref != f"{_OVERLAY_PREFIX}{expected_trace_id}.svg"
    ):
        raise ValueError("raster trace digest or identity does not match its contents")
    for candidate in report.candidates:
        expected_candidate_id = _candidate_identifier(
            report.source.source_sha256,
            candidate.geometry,
            candidate.evidence_bbox_px,
            candidate.fit_error_px,
            candidate.support_pixels,
        )
        if candidate.candidate_id != expected_candidate_id:
            raise ValueError("raster candidate identity does not match its evidence")


def accepted_operations(
    report: RasterTraceReport,
    acceptance: RasterTraceAcceptance,
    *,
    layer: str = "RASTER_TRACE",
) -> tuple[Operation, ...]:
    """Convert reviewed candidates into deterministic draft operations."""
    if not layer.strip() or len(layer) > 256 or any(character in layer for character in "\r\n"):
        raise ValueError("layer must be a bounded single-line name")
    operations: list[Operation] = []
    for candidate in verify_acceptance(report, acceptance):
        geometry = candidate.geometry
        operation_type: OperationType
        wire_geometry: dict[str, Any]
        expected: dict[str, Any]
        if isinstance(geometry, LineGeometry):
            operation_type = OperationType.CREATE_LINE
            wire_geometry = {
                "start_mm": list(geometry.start_mm),
                "end_mm": list(geometry.end_mm),
            }
            expected = {"length_mm": math.dist(geometry.start_mm, geometry.end_mm)}
        elif isinstance(geometry, CircleGeometry):
            operation_type = OperationType.CREATE_CIRCLE
            wire_geometry = {
                "center_mm": list(geometry.center_mm),
                "diameter_mm": geometry.radius_mm * 2.0,
            }
            expected = {
                "center_mm": list(geometry.center_mm),
                "radius_mm": geometry.radius_mm,
                "diameter_mm": geometry.radius_mm * 2.0,
            }
        elif isinstance(geometry, ArcGeometry):
            operation_type = OperationType.CREATE_ARC
            wire_geometry = {
                "center_mm": list(geometry.center_mm),
                "radius_mm": geometry.radius_mm,
                "start_angle_deg": geometry.start_angle_deg,
                "end_angle_deg": geometry.end_angle_deg,
            }
            expected = {"radius_mm": geometry.radius_mm}
        elif isinstance(geometry, PolylineGeometry) and geometry.closed:
            operation_type = OperationType.CREATE_CLOSED_POLYLINE
            wire_geometry = {"vertices_mm": [list(vertex.point_mm) for vertex in geometry.vertices]}
            expected = {"closed": True, "vertex_count": len(geometry.vertices)}
        else:  # RasterGeometry currently contains no other accepted shape.
            raise ValueError("accepted raster geometry has no safe draft operation")
        operations.append(
            Operation(
                operation_id=f"op:raster:{candidate.candidate_id.removeprefix('raster-candidate-')}",
                feature_id=candidate.candidate_id,
                type=operation_type,
                layer=layer,
                geometry=wire_geometry,
                expected=expected,
            )
        )
    return tuple(operations)


def _read_image_header(data: bytes) -> tuple[RasterFormat, int, int]:
    if data.startswith(_PNG_MAGIC):
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise ValueError("PNG is missing its canonical IHDR header")
        width, height = struct.unpack(">II", data[16:24])
        return RasterFormat.PNG, width, height
    if data.startswith(_JPEG_MAGIC):
        width, height = _jpeg_dimensions(data)
        return RasterFormat.JPEG, width, height
    if data.startswith((_TIFF_LE_MAGIC, _TIFF_BE_MAGIC)):
        width, height = _tiff_dimensions(data)
        return RasterFormat.TIFF, width, height
    raise ValueError("raster magic bytes are not PNG, JPEG, or TIFF")


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    position = 2
    start_of_frame = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0x01, *range(0xD0, 0xD9)}:
            continue
        if position + 2 > len(data):
            break
        segment_length = int.from_bytes(data[position : position + 2], "big")
        if segment_length < 2 or position + segment_length > len(data):
            break
        if marker in start_of_frame:
            if segment_length < 7:
                break
            height = int.from_bytes(data[position + 3 : position + 5], "big")
            width = int.from_bytes(data[position + 5 : position + 7], "big")
            return width, height
        if marker == 0xDA:
            break
        position += segment_length
    raise ValueError("JPEG dimensions are missing or malformed")


def _tiff_dimensions(data: bytes) -> tuple[int, int]:
    little = data.startswith(_TIFF_LE_MAGIC)
    byte_order: Literal["little", "big"] = "little" if little else "big"
    if len(data) < 8:
        raise ValueError("TIFF header is truncated")
    ifd_offset = int.from_bytes(data[4:8], byte_order)
    if ifd_offset < 8 or ifd_offset + 2 > len(data):
        raise ValueError("TIFF IFD offset is invalid")
    entry_count = int.from_bytes(data[ifd_offset : ifd_offset + 2], byte_order)
    if entry_count > 4096 or ifd_offset + 2 + entry_count * 12 > len(data):
        raise ValueError("TIFF IFD is outside the bounded payload")
    dimensions: dict[int, int] = {}
    for index in range(entry_count):
        start = ifd_offset + 2 + index * 12
        tag = int.from_bytes(data[start : start + 2], byte_order)
        if tag not in {256, 257}:
            continue
        value_type = int.from_bytes(data[start + 2 : start + 4], byte_order)
        count = int.from_bytes(data[start + 4 : start + 8], byte_order)
        if count != 1 or value_type not in {3, 4}:
            raise ValueError("TIFF dimensions use an unsupported representation")
        size = 2 if value_type == 3 else 4
        dimensions[tag] = int.from_bytes(data[start + 8 : start + 8 + size], byte_order)
    if 256 not in dimensions or 257 not in dimensions:
        raise ValueError("TIFF dimensions are missing")
    return dimensions[256], dimensions[257]


def _validate_dimensions(width: int, height: int, limits: RasterTraceLimits) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("raster dimensions must be positive")
    if width > limits.max_dimension_px or height > limits.max_dimension_px:
        raise ValueError("raster dimension exceeds the configured limit")
    if width * height > limits.max_pixels:
        raise ValueError("raster pixel count exceeds the configured limit")


def _detect_shapes(image: NDArray[np.uint8]) -> tuple[_DetectedShape, ...]:
    blurred = cv2.GaussianBlur(image, (3, 3), 0)
    _, ink = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    if int(np.count_nonzero(ink)) > ink.size // 2:
        ink = cv2.bitwise_not(ink)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    components: list[tuple[int, int, int, int, int, int]] = []
    for label in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[label])
        if area >= _MIN_COMPONENT_PIXELS:
            components.append((y, x, width, height, area, label))
    components.sort()
    return tuple(
        _classify_component(labels == label, (x, y, width, height, area))
        for y, x, width, height, area, label in components
    )


def _classify_component(
    mask: NDArray[np.bool_],
    stats: tuple[int, int, int, int, int],
) -> _DetectedShape:
    x, y, width, height, support = stats
    ys, xs = np.nonzero(mask)
    points = np.column_stack((xs.astype(np.float64), ys.astype(np.float64)))
    bbox = (float(x), float(y), float(x + width - 1), float(y + height - 1))
    circle = _circle_fit(points)
    if circle is not None:
        center_x, center_y, radius, error, start_angle, end_angle, sweep = circle
        tolerance = max(2.0, radius * 0.08)
        fit_score = max(0.0, 1.0 - error / tolerance)
        support_score = min(1.0, support / max(32.0, 2.0 * math.pi * radius))
        if sweep >= math.tau * 0.88 and radius >= 3.0 and fit_score >= 0.45:
            confidence = 0.8 * fit_score + 0.2 * support_score
            return _DetectedShape(
                "circle",
                (center_x, center_y, radius),
                confidence,
                error,
                support,
                bbox,
            )
        diagonal = math.hypot(mask.shape[1], mask.shape[0])
        if (
            math.radians(25.0) <= sweep < math.tau * 0.88
            and 3.0 <= radius <= diagonal * 2.0
            and fit_score >= 0.5
        ):
            confidence = 0.75 * fit_score + 0.15 * support_score + 0.1 * min(1.0, sweep / math.pi)
            return _DetectedShape(
                "arc",
                (center_x, center_y, radius, start_angle, end_angle),
                confidence,
                error,
                support,
                bbox,
                ("arc_direction_requires_engineer_review",),
            )

    line = _line_fit(points)
    if line is not None:
        start, end, error, elongation = line
        length = math.dist(start, end)
        tolerance = max(3.0, length * 0.025)
        fit_score = max(0.0, 1.0 - error / tolerance)
        if length >= 5.0 and elongation >= 10.0 and fit_score >= 0.35:
            confidence = 0.75 * fit_score + 0.25 * min(1.0, math.log10(elongation) / 2.0)
            return _DetectedShape(
                "line",
                (*start, *end),
                confidence,
                error,
                support,
                bbox,
            )

    contours, _ = cv2.findContours(
        mask.astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    contour = max(contours, key=cv2.contourArea) if contours else None
    if contour is not None:
        perimeter = float(cv2.arcLength(contour, True))
        area = float(cv2.contourArea(contour))
        approximation = cv2.approxPolyDP(contour, max(1.0, perimeter * 0.02), True)
        vertices = tuple((float(point[0][0]), float(point[0][1])) for point in approximation)
        circularity = 4.0 * math.pi * area / (perimeter * perimeter) if perimeter > 0.0 else 0.0
        if 3 <= len(vertices) <= 12 and area >= 16.0 and circularity < 0.9:
            confidence = min(0.96, 0.7 + min(area / max(1.0, width * height), 1.0) * 0.2)
            return _DetectedShape(
                "polyline",
                vertices,
                confidence,
                0.0,
                support,
                bbox,
            )
        if len(vertices) >= 2:
            return _DetectedShape(
                "polyline",
                vertices,
                0.15,
                max(width, height) / 2.0,
                support,
                bbox,
                ("component_has_no_supported_primitive_fit",),
            )

    return _DetectedShape(
        "line",
        (float(x), float(y), float(x + width - 1), float(y + height - 1)),
        0.05,
        max(width, height),
        support,
        bbox,
        ("component_has_no_supported_primitive_fit",),
    )


def _circle_fit(
    points: NDArray[np.float64],
) -> tuple[float, float, float, float, float, float, float] | None:
    if len(points) < 8:
        return None
    matrix = np.column_stack((2.0 * points[:, 0], 2.0 * points[:, 1], np.ones(len(points))))
    target = points[:, 0] ** 2 + points[:, 1] ** 2
    try:
        solution, _, rank, _ = np.linalg.lstsq(matrix, target, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if rank < 3:
        return None
    center_x, center_y, constant = (float(value) for value in solution)
    radius_squared = constant + center_x * center_x + center_y * center_y
    if radius_squared <= 0.0 or not math.isfinite(radius_squared):
        return None
    radius = math.sqrt(radius_squared)
    distances = np.hypot(points[:, 0] - center_x, points[:, 1] - center_y)
    error = float(np.sqrt(np.mean((distances - radius) ** 2)))
    angles = np.sort(np.mod(np.arctan2(points[:, 1] - center_y, points[:, 0] - center_x), math.tau))
    gaps = np.diff(np.concatenate((angles, angles[:1] + math.tau)))
    gap_index = int(np.argmax(gaps))
    sweep = float(math.tau - gaps[gap_index])
    start = float(angles[(gap_index + 1) % len(angles)])
    end = float(angles[gap_index])
    return center_x, center_y, radius, error, start, end, sweep


def _line_fit(
    points: NDArray[np.float64],
) -> tuple[tuple[float, float], tuple[float, float], float, float] | None:
    if len(points) < 2:
        return None
    mean = points.mean(axis=0)
    centered = points - mean
    covariance = np.cov(centered, rowvar=False, bias=True)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    if eigenvalues[-1] <= 0.0:
        return None
    direction = eigenvectors[:, -1]
    if direction[0] < 0.0 or (direction[0] == 0.0 and direction[1] < 0.0):
        direction = -direction
    projections = centered @ direction
    start_array = mean + direction * float(projections.min())
    end_array = mean + direction * float(projections.max())
    perpendicular = centered - np.outer(projections, direction)
    error = float(np.sqrt(np.mean(np.sum(perpendicular * perpendicular, axis=1))))
    elongation = float(eigenvalues[-1] / max(eigenvalues[0], 1.0e-12))
    return (
        (float(start_array[0]), float(start_array[1])),
        (float(end_array[0]), float(end_array[1])),
        error,
        elongation,
    )


def _geometry(
    shape: _DetectedShape,
    calibration: RasterCalibration | None,
) -> LineGeometry | CircleGeometry | ArcGeometry | PolylineGeometry:
    transform = _CalibrationTransform(calibration)
    if shape.kind == "line":
        x1, y1, x2, y2 = shape.values
        endpoints = sorted((transform.point(x1, y1), transform.point(x2, y2)))
        return LineGeometry(start_mm=endpoints[0], end_mm=endpoints[1])
    if shape.kind == "circle":
        center_x, center_y, radius = shape.values
        return CircleGeometry(
            center_mm=transform.point(center_x, center_y),
            radius_mm=transform.length(radius),
        )
    if shape.kind == "arc":
        center_x, center_y, radius, start, end = shape.values
        start_point = (center_x + radius * math.cos(start), center_y + radius * math.sin(start))
        end_point = (center_x + radius * math.cos(end), center_y + radius * math.sin(end))
        center = transform.point(center_x, center_y)
        world_start = transform.point(*start_point)
        world_end = transform.point(*end_point)
        start_angle = (
            math.degrees(math.atan2(world_start[1] - center[1], world_start[0] - center[0])) % 360.0
        )
        end_angle = (
            math.degrees(math.atan2(world_end[1] - center[1], world_end[0] - center[0])) % 360.0
        )
        return ArcGeometry(
            center_mm=center,
            radius_mm=transform.length(radius),
            start_angle_deg=round(start_angle, 9),
            end_angle_deg=round(end_angle, 9),
        )
    points = _canonical_closed_points(
        tuple(transform.point(float(point[0]), float(point[1])) for point in shape.values)
    )
    vertices = tuple(PolylineVertex(point_mm=point) for point in points)
    return PolylineGeometry(vertices=vertices, closed=True)


class _CalibrationTransform:
    def __init__(self, calibration: RasterCalibration | None) -> None:
        self._calibration = calibration
        if calibration is None:
            self._ux = 1.0
            self._uy = 0.0
            self._scale = 1.0
            return
        dx = calibration.pixel_b.x - calibration.pixel_a.x
        dy = calibration.pixel_b.y - calibration.pixel_a.y
        distance = math.hypot(dx, dy)
        self._ux = dx / distance
        self._uy = dy / distance
        self._scale = calibration.millimetres_per_pixel

    def point(self, x: float, y: float) -> tuple[float, float]:
        calibration = self._calibration
        if calibration is None:
            return _rounded_point(x, y)
        dx = x - calibration.pixel_a.x
        dy = y - calibration.pixel_a.y
        world_x = calibration.origin_mm[0] + self._scale * (dx * self._ux + dy * self._uy)
        # Pixel Y points down.  This perpendicular keeps CAD +Y counter-clockwise from +X.
        world_y = calibration.origin_mm[1] + self._scale * (dx * self._uy - dy * self._ux)
        return _rounded_point(world_x, world_y)

    def length(self, value: float) -> float:
        return round(float(value) * self._scale, 9)


def _rounded_point(x: float, y: float) -> tuple[float, float]:
    rounded_x = round(float(x), 9)
    rounded_y = round(float(y), 9)
    return (0.0 if rounded_x == 0.0 else rounded_x, 0.0 if rounded_y == 0.0 else rounded_y)


def _canonical_closed_points(
    points: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    compact = tuple(
        point for index, point in enumerate(points) if index == 0 or point != points[index - 1]
    )
    if len(compact) > 1 and compact[0] == compact[-1]:
        compact = compact[:-1]
    if len(set(compact)) < 3:
        return compact
    signed_area = sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(compact, compact[1:] + compact[:1], strict=True)
    )
    oriented = compact if signed_area >= 0.0 else tuple(reversed(compact))
    start = min(range(len(oriented)), key=lambda index: oriented[index])
    return oriented[start:] + oriented[:start]


def _overlay_svg(
    width: int,
    height: int,
    source_digest: str,
    candidates: tuple[RasterVectorCandidate, ...],
    detected: tuple[_DetectedShape, ...],
) -> str:
    colors = {
        RasterCandidateStatus.PROPOSED: "#16a34a",
        RasterCandidateStatus.AMBIGUOUS: "#d97706",
        RasterCandidateStatus.REJECTED: "#dc2626",
    }
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f"<metadata>source={html.escape(source_digest)}</metadata>",
    ]
    for candidate, shape in zip(candidates, detected, strict=True):
        color = colors[candidate.status]
        left, top, right, bottom = shape.bbox
        lines.append(
            f'<rect x="{left:.3f}" y="{top:.3f}" width="{right - left:.3f}" '
            f'height="{bottom - top:.3f}" fill="none" stroke="{color}" stroke-width="1"/>'
        )
        lines.append(
            f'<text x="{left:.3f}" y="{max(8.0, top - 2.0):.3f}" fill="{color}" '
            f'font-size="7">{html.escape(candidate.candidate_id)}</text>'
        )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


__all__ = [
    "LocalRasterTracer",
    "RasterTraceLimits",
    "accept_trace",
    "accepted_operations",
    "verify_acceptance",
]
