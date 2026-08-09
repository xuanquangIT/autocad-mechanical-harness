"""Canonical curved-edge parameters and deterministic chord approximations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from cad_harness.domain.errors import InvalidGeometryError
from cad_harness.geometry.primitives import Point2D, require_finite


class CurveKind(StrEnum):
    ARC = "arc"
    CIRCLE = "circle"
    ELLIPSE = "ellipse"


@dataclass(frozen=True, slots=True)
class CurveParams:
    """A curve in millimetres, X+ angular origin and CCW-positive degrees."""

    kind: CurveKind
    center: Point2D
    start_angle_deg: float
    sweep_deg: float
    radius_mm: float | None = None
    semi_major_mm: float | None = None
    semi_minor_mm: float | None = None
    rotation_deg: float = 0.0

    def __post_init__(self) -> None:
        values = [self.start_angle_deg, self.sweep_deg, self.rotation_deg]
        values.extend(
            value
            for value in (self.radius_mm, self.semi_major_mm, self.semi_minor_mm)
            if value is not None
        )
        require_finite(*values)
        if not 0.0 < abs(self.sweep_deg) <= 360.0:
            raise InvalidGeometryError("Curve sweep must be non-zero and at most 360 degrees")
        if self.kind in (CurveKind.ARC, CurveKind.CIRCLE):
            if self.radius_mm is None or self.radius_mm <= 0.0:
                raise InvalidGeometryError("Circular curve radius must be positive")
            if self.semi_major_mm is not None or self.semi_minor_mm is not None:
                raise InvalidGeometryError("Circular curves cannot carry ellipse axes")
        elif (
            self.semi_major_mm is None
            or self.semi_minor_mm is None
            or self.semi_major_mm <= 0.0
            or self.semi_minor_mm <= 0.0
            or self.semi_minor_mm > self.semi_major_mm
            or self.radius_mm is not None
        ):
            raise InvalidGeometryError("Ellipse axes must satisfy major >= minor > 0")

    @property
    def end_angle_deg(self) -> float:
        return self.start_angle_deg + self.sweep_deg

    @property
    def is_full(self) -> bool:
        return math.isclose(abs(self.sweep_deg), 360.0, rel_tol=0.0, abs_tol=1.0e-12)

    @property
    def maximum_radius_mm(self) -> float:
        if self.radius_mm is not None:
            return self.radius_mm
        assert self.semi_major_mm is not None
        return self.semi_major_mm

    def point_at_fraction(self, fraction: float) -> Point2D:
        if not 0.0 <= fraction <= 1.0:
            raise InvalidGeometryError("Curve fraction must lie in [0, 1]")
        parameter = math.radians(self.start_angle_deg + self.sweep_deg * fraction)
        if self.radius_mm is not None:
            return Point2D(
                self.center.x + self.radius_mm * math.cos(parameter),
                self.center.y + self.radius_mm * math.sin(parameter),
            )
        assert self.semi_major_mm is not None and self.semi_minor_mm is not None
        rotation = math.radians(self.rotation_deg)
        local_x = self.semi_major_mm * math.cos(parameter)
        local_y = self.semi_minor_mm * math.sin(parameter)
        return Point2D(
            self.center.x + local_x * math.cos(rotation) - local_y * math.sin(rotation),
            self.center.y + local_x * math.sin(rotation) + local_y * math.cos(rotation),
        )

    @property
    def start_point(self) -> Point2D:
        return self.point_at_fraction(0.0)

    @property
    def end_point(self) -> Point2D:
        return self.point_at_fraction(1.0)


def normalize_circle(center: Point2D, radius_mm: float) -> CurveParams:
    return CurveParams(CurveKind.CIRCLE, center, 0.0, 360.0, radius_mm=radius_mm)


def normalize_arc(
    center: Point2D,
    radius_mm: float,
    start_angle_deg: float,
    end_angle_deg: float | None = None,
    *,
    sweep_deg: float | None = None,
) -> CurveParams:
    if (end_angle_deg is None) is (sweep_deg is None):
        raise InvalidGeometryError("Provide exactly one of end_angle_deg or sweep_deg")
    resolved_sweep = sweep_deg
    if resolved_sweep is None:
        assert end_angle_deg is not None
        resolved_sweep = (end_angle_deg - start_angle_deg) % 360.0
        if math.isclose(resolved_sweep, 0.0, rel_tol=0.0, abs_tol=1.0e-12):
            resolved_sweep = 360.0
    return CurveParams(
        CurveKind.ARC,
        center,
        start_angle_deg,
        resolved_sweep,
        radius_mm=radius_mm,
    )


def normalize_ellipse(
    center: Point2D,
    semi_major_mm: float,
    semi_minor_mm: float,
    rotation_deg: float,
    start_angle_deg: float = 0.0,
    sweep_deg: float = 360.0,
) -> CurveParams:
    return CurveParams(
        CurveKind.ELLIPSE,
        center,
        start_angle_deg,
        sweep_deg,
        semi_major_mm=semi_major_mm,
        semi_minor_mm=semi_minor_mm,
        rotation_deg=rotation_deg,
    )


def normalize_bulge(start: Point2D, end: Point2D, bulge: float) -> CurveParams:
    """Convert a DXF bulge (tan(sweep/4)) into a signed circular arc."""
    require_finite(bulge)
    chord = start.distance_to(end)
    if chord <= 0.0 or math.isclose(bulge, 0.0, rel_tol=0.0, abs_tol=1.0e-15):
        raise InvalidGeometryError("A bulge needs distinct endpoints and a non-zero factor")
    dx, dy = end.x - start.x, end.y - start.y
    midpoint = Point2D((start.x + end.x) / 2.0, (start.y + end.y) / 2.0)
    offset = chord * (1.0 - bulge * bulge) / (4.0 * bulge)
    center = Point2D(midpoint.x - dy * offset / chord, midpoint.y + dx * offset / chord)
    radius = chord * (1.0 + bulge * bulge) / (4.0 * abs(bulge))
    start_angle = math.degrees(math.atan2(start.y - center.y, start.x - center.x))
    sweep = math.degrees(4.0 * math.atan(bulge))
    return CurveParams(CurveKind.ARC, center, start_angle, sweep, radius_mm=radius)


def chord_segment_count(curve: CurveParams, chord_tolerance_mm: float) -> int:
    """Smallest deterministic count whose maximum sagitta meets the tolerance.

    An ellipse is an affine image of the unit circle.  Bounding that map by its
    semi-major axis makes the circular sagitta formula conservative for ellipses.
    """
    require_finite(chord_tolerance_mm)
    if chord_tolerance_mm <= 0.0:
        raise InvalidGeometryError("Chord tolerance must be positive")
    radius = curve.maximum_radius_mm
    if chord_tolerance_mm >= radius:
        max_step = math.pi
    else:
        max_step = 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - chord_tolerance_mm / radius)))
    sweep = abs(math.radians(curve.sweep_deg))
    return max(1, math.ceil(sweep / max_step))


def linearize_curve(curve: CurveParams, chord_tolerance_mm: float) -> tuple[Point2D, ...]:
    count = chord_segment_count(curve, chord_tolerance_mm)
    return tuple(curve.point_at_fraction(index / count) for index in range(count + 1))


def chord_error_bound(curve: CurveParams, segment_count: int) -> float:
    """Conservative maximum deviation used to prove a chosen segmentation."""
    if segment_count < 1:
        raise InvalidGeometryError("A curve approximation needs at least one segment")
    half_step = abs(math.radians(curve.sweep_deg)) / (2.0 * segment_count)
    return curve.maximum_radius_mm * (1.0 - math.cos(half_step))
