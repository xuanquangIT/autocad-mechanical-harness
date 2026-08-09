"""Tolerance-aware measurements over kernel entities."""

from __future__ import annotations

import math
from itertools import combinations, pairwise

from cad_harness.domain.errors import InvalidFeatureParametersError, InvalidGeometryError
from cad_harness.geometry.areas import (
    Contour,
    CurveContour,
    LineEdge,
    contour_area,
    contour_perimeter,
    curve_length,
)
from cad_harness.geometry.curves import CurveParams, linearize_curve
from cad_harness.geometry.intersections import point_to_segment_distance, segment_intersection
from cad_harness.geometry.primitives import BoundingBox, Circle2D, Point2D, Polyline2D
from cad_harness.geometry.tolerance import ToleranceProfile

type MeasurableEntity = Circle2D | Polyline2D | CurveParams | LineEdge | CurveContour


def point_point_distance(first: Point2D, second: Point2D) -> float:
    return first.distance_to(second)


def _segments(
    entity: MeasurableEntity, tolerance: ToleranceProfile
) -> list[tuple[Point2D, Point2D]]:
    if isinstance(entity, Polyline2D):
        return entity.segments
    if isinstance(entity, LineEdge):
        return [(entity.start, entity.end)]
    if isinstance(entity, CurveParams):
        points = linearize_curve(entity, tolerance.arc_chord_tolerance_mm)
        return list(pairwise(points))
    if isinstance(entity, CurveContour):
        points = entity.vertices(tolerance.arc_chord_tolerance_mm)
        return Polyline2D(points, closed=True).segments
    raise InvalidFeatureParametersError("A circle does not expose line segments")


def point_entity_distance(
    point: Point2D, entity: MeasurableEntity, tolerance: ToleranceProfile
) -> float:
    """Shortest distance to an entity boundary, in millimetres."""
    if isinstance(entity, Circle2D):
        return abs(point.distance_to(entity.center) - entity.radius_mm)
    return min(
        point_to_segment_distance(point, start, end) for start, end in _segments(entity, tolerance)
    )


def _segment_distance(
    first: tuple[Point2D, Point2D],
    second: tuple[Point2D, Point2D],
    tolerance: ToleranceProfile,
) -> float:
    if segment_intersection(*first, *second, tolerance) is not None:
        return 0.0
    value = min(
        point_to_segment_distance(first[0], *second),
        point_to_segment_distance(first[1], *second),
        point_to_segment_distance(second[0], *first),
        point_to_segment_distance(second[1], *first),
    )
    return 0.0 if tolerance.is_zero_length(value) else value


def entity_entity_distance(
    first: MeasurableEntity,
    second: MeasurableEntity,
    tolerance: ToleranceProfile,
) -> float:
    if isinstance(first, Circle2D) and isinstance(second, Circle2D):
        separation = first.center.distance_to(second.center) - first.radius_mm - second.radius_mm
        return 0.0 if separation <= tolerance.absolute_length_mm else separation
    if isinstance(first, Circle2D):
        separation = point_entity_distance(first.center, second, tolerance) - first.radius_mm
        return 0.0 if separation <= tolerance.absolute_length_mm else separation
    if isinstance(second, Circle2D):
        return entity_entity_distance(second, first, tolerance)
    return min(
        _segment_distance(a, b, tolerance)
        for a in _segments(first, tolerance)
        for b in _segments(second, tolerance)
    )


def line_angle_deg(
    first_start: Point2D,
    first_end: Point2D,
    second_start: Point2D,
    second_end: Point2D,
    tolerance: ToleranceProfile,
) -> float:
    first = first_start.vector_to(first_end)
    second = second_start.vector_to(second_end)
    if tolerance.is_zero_length(first.length) or tolerance.is_zero_length(second.length):
        raise InvalidGeometryError("Line angle is undefined for a zero-length line")
    cosine = abs(first.dot(second) / (first.length * second.length))
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def arc_length(curve: CurveParams) -> float:
    return curve_length(curve)


def _curve_bbox(curve: CurveParams, tolerance: ToleranceProfile) -> BoundingBox:
    if curve.is_full and curve.radius_mm is not None:
        radius = curve.radius_mm
        return BoundingBox(
            curve.center.x - radius,
            curve.center.y - radius,
            curve.center.x + radius,
            curve.center.y + radius,
        )
    return BoundingBox.from_points(list(linearize_curve(curve, tolerance.arc_chord_tolerance_mm)))


def entity_bounding_box(entity: MeasurableEntity, tolerance: ToleranceProfile) -> BoundingBox:
    if isinstance(entity, Circle2D):
        return BoundingBox(
            entity.center.x - entity.radius_mm,
            entity.center.y - entity.radius_mm,
            entity.center.x + entity.radius_mm,
            entity.center.y + entity.radius_mm,
        )
    if isinstance(entity, Polyline2D):
        return entity.bounding_box()
    if isinstance(entity, LineEdge):
        return BoundingBox.from_points([entity.start, entity.end])
    if isinstance(entity, CurveParams):
        return _curve_bbox(entity, tolerance)
    return BoundingBox.from_points(list(entity.vertices(tolerance.arc_chord_tolerance_mm)))


def entity_set_bounding_box(
    entities: tuple[MeasurableEntity, ...], tolerance: ToleranceProfile
) -> BoundingBox:
    if not entities:
        raise InvalidGeometryError("Cannot measure an empty entity set")
    boxes = [entity_bounding_box(entity, tolerance) for entity in entities]
    return BoundingBox(
        min(box.min_x for box in boxes),
        min(box.min_y for box in boxes),
        max(box.max_x for box in boxes),
        max(box.max_y for box in boxes),
    )


def hole_boundary_distance(
    hole: Circle2D, boundary: Polyline2D | CurveContour, tolerance: ToleranceProfile
) -> float:
    """Remaining material from the hole edge to the nearest boundary."""
    return point_entity_distance(hole.center, boundary, tolerance) - hole.radius_mm


def center_center_distance(first: Circle2D, second: Circle2D) -> float:
    return first.center.distance_to(second.center)


def contour_length(contour: Contour) -> float:
    return contour_perimeter(contour)


def closed_contour_area(contour: Contour) -> float:
    return contour_area(contour)


def minimum_pair_distance(
    entities: tuple[MeasurableEntity, ...], tolerance: ToleranceProfile
) -> float:
    if len(entities) < 2:
        raise InvalidFeatureParametersError("At least two entities are required")
    return min(entity_entity_distance(a, b, tolerance) for a, b in combinations(entities, 2))
