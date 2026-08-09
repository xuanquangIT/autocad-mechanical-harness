"""Pure vertex fillet and chamfer construction."""

from __future__ import annotations

import math
from dataclasses import dataclass

from cad_harness.domain.errors import InvalidFeatureParametersError
from cad_harness.geometry.curves import CurveParams, normalize_arc
from cad_harness.geometry.primitives import Point2D, Polyline2D, Vector2D, require_finite
from cad_harness.geometry.tolerance import ToleranceProfile


@dataclass(frozen=True, slots=True)
class FilletResult:
    tangent_in: Point2D
    tangent_out: Point2D
    arc: CurveParams
    maximum_radius_mm: float


@dataclass(frozen=True, slots=True)
class ChamferResult:
    point_on_first: Point2D
    point_on_second: Point2D
    distance_first_mm: float
    distance_second_mm: float
    angle_deg: float


def _unit_from_vertex(vertex: Point2D, point: Point2D) -> Vector2D:
    vector = vertex.vector_to(point)
    if vector.length <= 0.0:
        raise InvalidFeatureParametersError("Adjacent edge has zero length")
    return vector.normalized()


def _interior_angle(first: Vector2D, second: Vector2D) -> float:
    cosine = max(-1.0, min(1.0, first.dot(second)))
    angle = math.acos(cosine)
    if angle <= 1.0e-12 or math.pi - angle <= 1.0e-12:
        raise InvalidFeatureParametersError("Fillet/chamfer requires a non-collinear vertex")
    return angle


def _offset(vertex: Point2D, direction: Vector2D, distance_mm: float) -> Point2D:
    return Point2D(vertex.x + direction.dx * distance_mm, vertex.y + direction.dy * distance_mm)


def fillet_vertex(
    previous: Point2D,
    vertex: Point2D,
    following: Point2D,
    radius_mm: float,
    tolerance: ToleranceProfile,
) -> FilletResult:
    """Replace a vertex with a tangent circular arc.

    Requirement 8.7 caps the public modifier at half the shorter edge.  The angular
    tangent limit may be tighter for an acute corner, so both constraints apply.
    """
    require_finite(radius_mm)
    if radius_mm <= 0.0:
        raise InvalidFeatureParametersError("Fillet radius must be positive")
    first = _unit_from_vertex(vertex, previous)
    second = _unit_from_vertex(vertex, following)
    angle = _interior_angle(first, second)
    shorter = min(vertex.distance_to(previous), vertex.distance_to(following))
    maximum = shorter / 2.0
    if radius_mm > maximum and not tolerance.length_close(radius_mm, maximum):
        raise InvalidFeatureParametersError(
            "Fillet radius exceeds the feasible maximum",
            required_action="Reduce the radius or lengthen both adjacent edges",
            details={
                "radius_mm": radius_mm,
                "maximum_allowed_mm": maximum,
                "maximum_radius_mm": maximum,
            },
        )

    tangent_distance = radius_mm / math.tan(angle / 2.0)
    tangent_in = _offset(vertex, first, tangent_distance)
    tangent_out = _offset(vertex, second, tangent_distance)
    bisector = Vector2D(first.dx + second.dx, first.dy + second.dy).normalized()
    center_distance = radius_mm / math.sin(angle / 2.0)
    center = _offset(vertex, bisector, center_distance)
    start_angle = math.degrees(math.atan2(tangent_in.y - center.y, tangent_in.x - center.x))
    incoming = previous.vector_to(vertex)
    outgoing = vertex.vector_to(following)
    turn = incoming.cross(outgoing)
    magnitude = 180.0 - math.degrees(angle)
    sweep = magnitude if turn > 0.0 else -magnitude
    arc = normalize_arc(center, radius_mm, start_angle, sweep_deg=sweep)
    result = FilletResult(tangent_in, tangent_out, arc, maximum)
    if not fillet_is_tangent(result, previous, vertex, following, tolerance):
        raise InvalidFeatureParametersError("Constructed fillet failed its tangency check")
    return result


def fillet_is_tangent(
    fillet: FilletResult,
    previous: Point2D,
    vertex: Point2D,
    following: Point2D,
    tolerance: ToleranceProfile,
) -> bool:
    incoming = previous.vector_to(vertex).normalized()
    outgoing = vertex.vector_to(following).normalized()
    first_radius = fillet.arc.center.vector_to(fillet.tangent_in)
    second_radius = fillet.arc.center.vector_to(fillet.tangent_out)
    first_error = abs(first_radius.dot(incoming))
    second_error = abs(second_radius.dot(outgoing))
    scale = max(fillet.arc.maximum_radius_mm, 1.0)
    return tolerance.is_zero_length(first_error / scale) and tolerance.is_zero_length(
        second_error / scale
    )


def chamfer_vertex(
    previous: Point2D,
    vertex: Point2D,
    following: Point2D,
    distance_first_mm: float,
    *,
    distance_second_mm: float | None = None,
    angle_deg: float | None = None,
    tolerance: ToleranceProfile,
) -> ChamferResult:
    """Construct a chamfer from ``(d1,d2)`` or ``(d1,angle)`` exclusively."""
    require_finite(distance_first_mm)
    if (distance_second_mm is None) is (angle_deg is None):
        raise InvalidFeatureParametersError(
            "Provide exactly one of distance_second_mm or angle_deg"
        )
    if distance_first_mm <= 0.0:
        raise InvalidFeatureParametersError("Chamfer distance must be positive")
    first = _unit_from_vertex(vertex, previous)
    second = _unit_from_vertex(vertex, following)
    interior = _interior_angle(first, second)
    resolved_angle: float
    resolved_second: float
    if distance_second_mm is not None:
        require_finite(distance_second_mm)
        if distance_second_mm <= 0.0:
            raise InvalidFeatureParametersError("Chamfer distance must be positive")
        resolved_second = distance_second_mm
        first_point = _offset(vertex, first, distance_first_mm)
        second_point = _offset(vertex, second, resolved_second)
        toward_vertex = first_point.vector_to(vertex).normalized()
        chamfer = first_point.vector_to(second_point).normalized()
        resolved_angle = math.degrees(math.acos(max(-1.0, min(1.0, toward_vertex.dot(chamfer)))))
    else:
        assert angle_deg is not None
        require_finite(angle_deg)
        alpha = math.radians(angle_deg)
        if alpha <= 0.0 or alpha + interior >= math.pi:
            raise InvalidFeatureParametersError("Chamfer angle is incompatible with the vertex")
        resolved_second = distance_first_mm * math.sin(alpha) / math.sin(alpha + interior)
        resolved_angle = angle_deg

    first_length = vertex.distance_to(previous)
    second_length = vertex.distance_to(following)
    if (
        distance_first_mm > first_length
        and not tolerance.length_close(distance_first_mm, first_length)
    ) or (
        resolved_second > second_length
        and not tolerance.length_close(resolved_second, second_length)
    ):
        raise InvalidFeatureParametersError(
            "Chamfer distance exceeds an adjacent edge",
            details={
                "maximum_distance_first_mm": first_length,
                "maximum_distance_second_mm": second_length,
            },
        )
    return ChamferResult(
        _offset(vertex, first, distance_first_mm),
        _offset(vertex, second, resolved_second),
        distance_first_mm,
        resolved_second,
        resolved_angle,
    )


def l_bracket_outline(
    origin: Point2D,
    leg_a_mm: float,
    leg_b_mm: float,
    thickness_mm: float,
    tolerance: ToleranceProfile,
    *,
    inner_fillet_radius_mm: float | None = None,
) -> Polyline2D:
    """Construct a closed six-sided L profile, optionally filleting its re-entrant corner."""
    from cad_harness.geometry.curves import linearize_curve

    require_finite(leg_a_mm, leg_b_mm, thickness_mm)
    if leg_a_mm <= 0.0 or leg_b_mm <= 0.0 or thickness_mm <= 0.0:
        raise InvalidFeatureParametersError(
            "Bracket legs and thickness must be positive",
            details={
                "leg_a_mm": leg_a_mm,
                "leg_b_mm": leg_b_mm,
                "thickness_mm": thickness_mm,
            },
        )
    if thickness_mm >= min(leg_a_mm, leg_b_mm):
        raise InvalidFeatureParametersError(
            "Bracket thickness must be smaller than both legs",
            required_action="Increase both legs or reduce the bracket thickness",
            details={
                "thickness_mm": thickness_mm,
                "maximum_thickness_mm": min(leg_a_mm, leg_b_mm),
            },
        )

    lower_right = Point2D(origin.x + leg_a_mm, origin.y)
    upper_right = Point2D(origin.x + leg_a_mm, origin.y + thickness_mm)
    inner = Point2D(origin.x + thickness_mm, origin.y + thickness_mm)
    upper_inner = Point2D(origin.x + thickness_mm, origin.y + leg_b_mm)
    upper_left = Point2D(origin.x, origin.y + leg_b_mm)
    vertices: tuple[Point2D, ...] = (
        origin,
        lower_right,
        upper_right,
        inner,
        upper_inner,
        upper_left,
    )
    if inner_fillet_radius_mm is None:
        return Polyline2D(vertices, closed=True)

    fillet = fillet_vertex(
        upper_right,
        inner,
        upper_inner,
        inner_fillet_radius_mm,
        tolerance,
    )
    arc_vertices = linearize_curve(fillet.arc, tolerance.arc_chord_tolerance_mm)
    return Polyline2D(
        (origin, lower_right, upper_right, *arc_vertices, upper_inner, upper_left),
        closed=True,
    )
