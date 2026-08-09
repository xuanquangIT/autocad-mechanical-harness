"""Rigid transforms for every geometry-kernel primitive."""

from __future__ import annotations

import math
from typing import cast

from cad_harness.geometry.areas import CurveContour, LineEdge
from cad_harness.geometry.curves import CurveKind, CurveParams
from cad_harness.geometry.primitives import (
    BoundingBox,
    Circle2D,
    Point2D,
    Polyline2D,
    Vector2D,
    require_finite,
)

type Transformable = (
    Point2D | Vector2D | BoundingBox | Circle2D | Polyline2D | CurveParams | LineEdge | CurveContour
)


def _curve_with_center(
    curve: CurveParams,
    center: Point2D,
    *,
    angle_delta_deg: float = 0.0,
) -> CurveParams:
    circular_start = curve.start_angle_deg + angle_delta_deg
    ellipse_rotation = curve.rotation_deg + angle_delta_deg
    return CurveParams(
        curve.kind,
        center,
        circular_start if curve.kind is not CurveKind.ELLIPSE else curve.start_angle_deg,
        curve.sweep_deg,
        radius_mm=curve.radius_mm,
        semi_major_mm=curve.semi_major_mm,
        semi_minor_mm=curve.semi_minor_mm,
        rotation_deg=ellipse_rotation,
    )


def translate(entity: Transformable, dx_mm: float, dy_mm: float) -> Transformable:
    require_finite(dx_mm, dy_mm)
    if isinstance(entity, Point2D):
        return entity.translated(dx_mm, dy_mm)
    if isinstance(entity, Vector2D):
        return entity
    if isinstance(entity, BoundingBox):
        return BoundingBox(
            entity.min_x + dx_mm,
            entity.min_y + dy_mm,
            entity.max_x + dx_mm,
            entity.max_y + dy_mm,
        )
    if isinstance(entity, Circle2D):
        return Circle2D(entity.center.translated(dx_mm, dy_mm), entity.diameter_mm)
    if isinstance(entity, Polyline2D):
        return Polyline2D(
            tuple(point.translated(dx_mm, dy_mm) for point in entity.vertices),
            closed=entity.closed,
        )
    if isinstance(entity, CurveParams):
        return _curve_with_center(entity, entity.center.translated(dx_mm, dy_mm))
    if isinstance(entity, LineEdge):
        return LineEdge(entity.start.translated(dx_mm, dy_mm), entity.end.translated(dx_mm, dy_mm))
    return CurveContour(
        tuple(
            cast("LineEdge | CurveParams", translate(edge, dx_mm, dy_mm)) for edge in entity.edges
        )
    )


def rotate_about(entity: Transformable, angle_deg: float, about: Point2D) -> Transformable:
    require_finite(angle_deg)
    if isinstance(entity, Point2D):
        return entity.rotated(angle_deg, about)
    if isinstance(entity, Vector2D):
        radians = math.radians(angle_deg)
        return Vector2D(
            entity.dx * math.cos(radians) - entity.dy * math.sin(radians),
            entity.dx * math.sin(radians) + entity.dy * math.cos(radians),
        )
    if isinstance(entity, BoundingBox):
        corners = [
            Point2D(entity.min_x, entity.min_y),
            Point2D(entity.max_x, entity.min_y),
            Point2D(entity.max_x, entity.max_y),
            Point2D(entity.min_x, entity.max_y),
        ]
        return BoundingBox.from_points([corner.rotated(angle_deg, about) for corner in corners])
    if isinstance(entity, Circle2D):
        return Circle2D(entity.center.rotated(angle_deg, about), entity.diameter_mm)
    if isinstance(entity, Polyline2D):
        return Polyline2D(
            tuple(point.rotated(angle_deg, about) for point in entity.vertices),
            closed=entity.closed,
        )
    if isinstance(entity, CurveParams):
        return _curve_with_center(
            entity, entity.center.rotated(angle_deg, about), angle_delta_deg=angle_deg
        )
    if isinstance(entity, LineEdge):
        return LineEdge(
            entity.start.rotated(angle_deg, about), entity.end.rotated(angle_deg, about)
        )
    return CurveContour(
        tuple(
            cast("LineEdge | CurveParams", rotate_about(edge, angle_deg, about))
            for edge in entity.edges
        )
    )
