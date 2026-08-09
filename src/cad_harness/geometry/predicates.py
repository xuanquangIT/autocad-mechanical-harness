"""Tolerance-aware predicates. Validation rules call these instead of comparing floats."""

from __future__ import annotations

import math

from cad_harness.geometry.intersections import point_to_segment_distance, segment_intersection
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
    """Exact polygon containment of the centre plus radial boundary clearance."""
    return _contains_point(outline, hole.center, tolerance) and (
        minimum_edge_distance(hole, outline) >= -tolerance.absolute_length_mm
    )


def minimum_edge_distance(hole: Circle2D, outline: Polyline2D) -> float:
    """Material remaining between a hole's edge and the nearest outline segment."""
    return (
        min(point_to_segment_distance(hole.center, start, end) for start, end in outline.segments)
        - hole.radius_mm
    )


def is_closed_within(polyline: Polyline2D, tolerance: ToleranceProfile) -> bool:
    """Whether closure is explicit or the two open endpoints meet within tolerance."""
    return polyline.closed or points_coincident(
        polyline.vertices[0], polyline.vertices[-1], tolerance
    )


def self_intersects(polyline: Polyline2D, tolerance: ToleranceProfile) -> bool:
    """Public predicate name used by drawing audit and feature recognition."""
    return polyline_self_intersects(polyline, tolerance)


def _point_on_boundary(point: Point2D, contour: Polyline2D, tolerance: ToleranceProfile) -> bool:
    return any(
        tolerance.is_coincident(point_to_segment_distance(point, start, end))
        for start, end in contour.segments
    )


def _contains_point(contour: Polyline2D, point: Point2D, tolerance: ToleranceProfile) -> bool:
    if _point_on_boundary(point, contour, tolerance):
        return True
    inside = False
    for start, end in contour.segments:
        crosses_height = (start.y > point.y) is not (end.y > point.y)
        if not crosses_height:
            continue
        crossing_x = start.x + (point.y - start.y) * (end.x - start.x) / (end.y - start.y)
        if crossing_x > point.x:
            inside = not inside
    return inside


def contains_contour(outer: Polyline2D, inner: Polyline2D, tolerance: ToleranceProfile) -> bool:
    """True only when the complete inner contour lies in/on the outer contour."""
    if not is_closed_within(outer, tolerance) or not is_closed_within(inner, tolerance):
        return False
    probes = list(inner.vertices)
    probes.extend(
        Point2D((start.x + end.x) / 2.0, (start.y + end.y) / 2.0) for start, end in inner.segments
    )
    return all(_contains_point(outer, point, tolerance) for point in probes)


def point_in_contour(contour: Polyline2D, point: Point2D, tolerance: ToleranceProfile) -> bool:
    """Public tolerance-aware point containment predicate, boundary inclusive."""
    return _contains_point(contour, point, tolerance)


def contour_overflow_mm(outer: Polyline2D, inner: Polyline2D, tolerance: ToleranceProfile) -> float:
    """Maximum distance an inner contour extends outside its parent."""
    probes = list(inner.vertices)
    probes.extend(
        Point2D((start.x + end.x) / 2.0, (start.y + end.y) / 2.0) for start, end in inner.segments
    )
    outside = [point for point in probes if not _contains_point(outer, point, tolerance)]
    if not outside:
        return 0.0
    return max(
        min(point_to_segment_distance(point, start, end) for start, end in outer.segments)
        for point in outside
    )


def circle_overflow_mm(outer: Polyline2D, circle: Circle2D, tolerance: ToleranceProfile) -> float:
    """Radial overflow of a circular child beyond a polygonal parent."""
    edge_distance = min(
        point_to_segment_distance(circle.center, start, end) for start, end in outer.segments
    )
    if _contains_point(outer, circle.center, tolerance):
        return max(0.0, circle.radius_mm - edge_distance)
    return circle.radius_mm + edge_distance
