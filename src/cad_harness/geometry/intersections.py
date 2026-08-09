"""Segment and circle intersection helpers used by validation rules."""

from __future__ import annotations

import math
from itertools import pairwise

from cad_harness.geometry.primitives import Point2D, Polyline2D
from cad_harness.geometry.tolerance import ToleranceProfile


def segment_intersection(
    a1: Point2D, a2: Point2D, b1: Point2D, b2: Point2D, tolerance: ToleranceProfile
) -> Point2D | None:
    """Proper intersection of two segments, or ``None``.

    Collinear overlaps return ``None``; they are reported separately as duplicate or
    overlapping geometry, which is a different finding than a crossing.
    """
    d1 = a1.vector_to(a2)
    d2 = b1.vector_to(b2)
    denominator = d1.cross(d2)
    if abs(denominator) < 1e-12:
        return None

    offset = a1.vector_to(b1)
    t = offset.cross(d2) / denominator
    u = offset.cross(d1) / denominator
    if not (0.0 <= t <= 1.0 and 0.0 <= u <= 1.0):
        return None
    return Point2D(a1.x + d1.dx * t, a1.y + d1.dy * t)


def line_intersection(
    a1: Point2D,
    a2: Point2D,
    b1: Point2D,
    b2: Point2D,
    tolerance: ToleranceProfile,
) -> Point2D | None:
    """Intersection of two unbounded lines, or ``None`` when effectively parallel."""
    first = a1.vector_to(a2)
    second = b1.vector_to(b2)
    scale = first.length * second.length
    if tolerance.is_zero_length(first.length) or tolerance.is_zero_length(second.length):
        return None
    denominator = first.cross(second)
    angular_limit = math.sin(math.radians(tolerance.angular_deg))
    if abs(denominator) / scale <= angular_limit:
        return None
    offset = a1.vector_to(b1)
    parameter = offset.cross(second) / denominator
    return Point2D(a1.x + first.dx * parameter, a1.y + first.dy * parameter)


def point_to_segment_distance(point: Point2D, a: Point2D, b: Point2D) -> float:
    """Shortest distance from ``point`` to segment ``ab``."""
    segment = a.vector_to(b)
    length_squared = segment.dx**2 + segment.dy**2
    if length_squared <= 0.0:
        return point.distance_to(a)
    to_point = a.vector_to(point)
    t = max(0.0, min(1.0, to_point.dot(segment) / length_squared))
    projection = Point2D(a.x + segment.dx * t, a.y + segment.dy * t)
    return point.distance_to(projection)


def circles_overlap(
    center_a: Point2D,
    diameter_a_mm: float,
    center_b: Point2D,
    diameter_b_mm: float,
    *,
    minimum_ligament_mm: float = 0.0,
) -> bool:
    """True when two holes are closer than the required material between them."""
    required = (diameter_a_mm + diameter_b_mm) / 2.0 + minimum_ligament_mm
    return center_a.distance_to(center_b) < required


def angular_spacing_deg(center: Point2D, points: tuple[Point2D, ...]) -> list[float]:
    """Consecutive angular gaps, sorted by angle. Used to verify bolt patterns."""
    angles = sorted(
        math.degrees(math.atan2(p.y - center.y, p.x - center.x)) % 360.0 for p in points
    )
    if len(angles) < 2:
        return []
    gaps = [b - a for a, b in pairwise(angles)]
    # Close the circle so a uniform pattern yields N identical gaps, not N-1.
    gaps.append(360.0 - angles[-1] + angles[0])
    return gaps


def contour_intersections(
    first: Polyline2D, second: Polyline2D, tolerance: ToleranceProfile
) -> tuple[Point2D, ...]:
    """Return proper crossings in deterministic segment order, deduplicated by tolerance."""
    intersections: list[Point2D] = []
    for first_start, first_end in first.segments:
        for second_start, second_end in second.segments:
            candidate = segment_intersection(
                first_start, first_end, second_start, second_end, tolerance
            )
            if candidate is None:
                continue
            if not any(
                tolerance.is_coincident(candidate.distance_to(item)) for item in intersections
            ):
                intersections.append(candidate)
    return tuple(intersections)
