"""Tolerance-aware predicates. Validation rules call these instead of comparing floats."""

from __future__ import annotations

import math

from cad_harness.geometry.intersections import segment_intersection
from cad_harness.geometry.primitives import Circle2D, Point2D, Polyline2D
from cad_harness.geometry.tolerance import ToleranceProfile


def all_finite(values: list[float]) -> bool:
    return all(math.isfinite(v) for v in values)


def points_coincident(a: Point2D, b: Point2D, tolerance: ToleranceProfile) -> bool:
    return tolerance.is_coincident(a.distance_to(b))


def has_duplicate_points(points: tuple[Point2D, ...], tolerance: ToleranceProfile) -> bool:
    for index, point in enumerate(points):
        for other in points[index + 1 :]:
            if points_coincident(point, other, tolerance):
                return True
    return False


def polyline_self_intersects(polyline: Polyline2D, tolerance: ToleranceProfile) -> bool:
    """True when two non-adjacent segments cross.

    O(n^2) by design: MVP outlines have few vertices and an auditable implementation
    matters more than speed here.
    """
    segments = polyline.segments
    count = len(segments)
    for i in range(count):
        for j in range(i + 1, count):
            adjacent = j == i + 1 or (i == 0 and j == count - 1 and polyline.closed)
            if adjacent:
                continue
            a1, a2 = segments[i]
            b1, b2 = segments[j]
            if segment_intersection(a1, a2, b1, b2, tolerance) is not None:
                return True
    return False


def is_orthogonal_rectangle(polyline: Polyline2D, tolerance: ToleranceProfile) -> bool:
    """True when the outline is a closed, axis-aligned four-sided rectangle."""
    if not polyline.closed or len(polyline.vertices) != 4:
        return False
    for a, b in polyline.segments:
        horizontal = tolerance.is_zero_length(abs(a.y - b.y))
        vertical = tolerance.is_zero_length(abs(a.x - b.x))
        if not (horizontal or vertical):
            return False
    return True


def hole_inside_outline(hole: Circle2D, outline: Polyline2D, tolerance: ToleranceProfile) -> bool:
    """Conservative containment: the whole hole must sit inside the outline's box.

    Exact polygon containment arrives with the non-rectangular outlines in a later
    phase; until then this errs on the side of reporting a violation.
    """
    box = outline.bounding_box()
    return box.contains_point(hole.center, margin_mm=hole.radius_mm - tolerance.coincidence_mm)


def minimum_edge_distance(hole: Circle2D, outline: Polyline2D) -> float:
    """Material remaining between a hole's edge and the nearest outline edge."""
    return outline.bounding_box().distance_to_edge(hole.center) - hole.radius_mm
