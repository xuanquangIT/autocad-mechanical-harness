"""Pure boundary-cutout and keyway construction in canonical millimetres."""

from __future__ import annotations

import math
from dataclasses import dataclass

from cad_harness.domain.errors import InvalidFeatureParametersError
from cad_harness.geometry.areas import CurveContour, LineEdge, contour_area
from cad_harness.geometry.curves import linearize_curve, normalize_arc
from cad_harness.geometry.primitives import Point2D, Polyline2D, Vector2D, require_finite
from cad_harness.geometry.tolerance import ToleranceProfile


@dataclass(frozen=True, slots=True)
class BoundaryCutoutResult:
    outline: Polyline2D
    removed_contour: Polyline2D
    removed_area_mm2: float


@dataclass(frozen=True, slots=True)
class KeywayResult:
    contour: CurveContour
    preview_outline: Polyline2D
    removed_area_mm2: float
    center: Point2D
    bore_radius_mm: float
    key_width_mm: float
    key_depth_mm: float


def _unit(start: Point2D, end: Point2D) -> Vector2D:
    vector = start.vector_to(end)
    if vector.length <= 0.0:
        raise InvalidFeatureParametersError("Cutout cannot use a zero-length edge")
    return vector.normalized()


def _point(origin: Point2D, direction: Vector2D, distance: float) -> Point2D:
    return Point2D(origin.x + direction.dx * distance, origin.y + direction.dy * distance)


def _replace_vertex(
    outline: Polyline2D, index: int, replacement: tuple[Point2D, ...]
) -> Polyline2D:
    vertices = list(outline.vertices)
    vertices[index : index + 1] = replacement
    return Polyline2D(tuple(vertices), closed=True)


def corner_notch(
    outline: Polyline2D,
    corner_index: int,
    width_mm: float,
    height_mm: float,
    tolerance: ToleranceProfile,
) -> BoundaryCutoutResult:
    """Remove a parallelogram at one corner, preserving contour order."""
    require_finite(width_mm, height_mm)
    count = len(outline.vertices)
    if not outline.closed or not 0 <= corner_index < count:
        raise InvalidFeatureParametersError(
            "Corner notch needs a closed outline and a valid corner index",
            details={"corner_index": corner_index, "vertex_count": count},
        )
    if width_mm <= 0.0 or height_mm <= 0.0:
        raise InvalidFeatureParametersError("Notch width and height must be positive")
    previous = outline.vertices[(corner_index - 1) % count]
    vertex = outline.vertices[corner_index]
    following = outline.vertices[(corner_index + 1) % count]
    first_length = vertex.distance_to(previous)
    second_length = vertex.distance_to(following)
    if (width_mm > first_length and not tolerance.length_close(width_mm, first_length)) or (
        height_mm > second_length and not tolerance.length_close(height_mm, second_length)
    ):
        raise InvalidFeatureParametersError(
            "Corner notch exceeds an adjacent edge",
            details={
                "maximum_width_mm": first_length,
                "maximum_height_mm": second_length,
            },
        )
    toward_previous = _unit(vertex, previous)
    toward_following = _unit(vertex, following)
    first = _point(vertex, toward_previous, width_mm)
    second = _point(vertex, toward_following, height_mm)
    inner = _point(first, toward_following, height_mm)
    removed = Polyline2D((first, vertex, second, inner), closed=True)
    return BoundaryCutoutResult(
        _replace_vertex(outline, corner_index, (first, inner, second)),
        removed,
        removed.area(),
    )


def edge_cutout(
    outline: Polyline2D,
    edge_index: int,
    offset_mm: float,
    width_mm: float,
    depth_mm: float,
    tolerance: ToleranceProfile,
) -> BoundaryCutoutResult:
    """Remove a rectangular pocket from an outline edge toward material."""
    require_finite(offset_mm, width_mm, depth_mm)
    count = len(outline.vertices)
    if not outline.closed or not 0 <= edge_index < count:
        raise InvalidFeatureParametersError(
            "Edge cutout needs a closed outline and a valid edge index",
            details={"edge_index": edge_index, "edge_count": count},
        )
    if offset_mm < 0.0 or width_mm <= 0.0 or depth_mm <= 0.0:
        raise InvalidFeatureParametersError(
            "Cutout offset must be non-negative and width/depth positive"
        )
    start = outline.vertices[edge_index]
    end = outline.vertices[(edge_index + 1) % count]
    direction = _unit(start, end)
    edge_length = start.distance_to(end)
    extent = offset_mm + width_mm
    if extent > edge_length and not tolerance.length_close(extent, edge_length):
        raise InvalidFeatureParametersError(
            "Edge cutout exceeds its target edge",
            details={"maximum_allowed_mm": edge_length, "requested_extent_mm": extent},
        )
    orientation = 1.0 if outline.signed_area() > 0.0 else -1.0
    inward = Vector2D(-direction.dy * orientation, direction.dx * orientation)
    first = _point(start, direction, offset_mm)
    second = _point(start, direction, extent)
    inner_first = _point(first, inward, depth_mm)
    inner_second = _point(second, inward, depth_mm)
    insertion = [first, inner_first, inner_second, second]
    if tolerance.is_coincident(first.distance_to(start)):
        insertion.pop(0)
    if tolerance.is_coincident(second.distance_to(end)):
        insertion.pop()
    vertices = list(outline.vertices)
    vertices[edge_index + 1 : edge_index + 1] = insertion
    removed = Polyline2D((first, second, inner_second, inner_first), closed=True)
    return BoundaryCutoutResult(Polyline2D(tuple(vertices), closed=True), removed, removed.area())


def keyway_contour(
    center: Point2D,
    bore_diameter_mm: float,
    key_width_mm: float,
    key_depth_mm: float,
    tolerance: ToleranceProfile,
) -> KeywayResult:
    """Construct a circular bore with a radial rectangular keyway at +Y."""
    require_finite(bore_diameter_mm, key_width_mm, key_depth_mm)
    if bore_diameter_mm <= 0.0 or key_width_mm <= 0.0 or key_depth_mm <= 0.0:
        raise InvalidFeatureParametersError("Bore diameter and key dimensions must be positive")
    radius = bore_diameter_mm / 2.0
    if key_width_mm >= bore_diameter_mm:
        raise InvalidFeatureParametersError(
            "Key width must be smaller than the bore diameter",
            details={"maximum_allowed_mm": bore_diameter_mm},
        )
    half_width = key_width_mm / 2.0
    y_intersection = math.sqrt(radius * radius - half_width * half_width)
    right = Point2D(center.x + half_width, center.y + y_intersection)
    left = Point2D(center.x - half_width, center.y + y_intersection)
    top_y = center.y + radius + key_depth_mm
    top_left = Point2D(left.x, top_y)
    top_right = Point2D(right.x, top_y)
    left_angle = math.degrees(math.atan2(left.y - center.y, left.x - center.x))
    right_angle = math.degrees(math.atan2(right.y - center.y, right.x - center.x))
    arc = normalize_arc(
        center,
        radius,
        left_angle,
        sweep_deg=360.0 - left_angle + right_angle,
    )
    contour = CurveContour(
        (
            arc,
            LineEdge(right, top_right),
            LineEdge(top_right, top_left),
            LineEdge(top_left, left),
        )
    )
    arc_points = linearize_curve(arc, tolerance.arc_chord_tolerance_mm)
    preview = Polyline2D((*arc_points[:-1], right, top_right, top_left), closed=True)
    return KeywayResult(
        contour,
        preview,
        contour_area(contour),
        center,
        radius,
        key_width_mm,
        key_depth_mm,
    )
