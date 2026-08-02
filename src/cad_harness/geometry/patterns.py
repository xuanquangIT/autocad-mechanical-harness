"""Pattern generators. These formulas are the reason the LLM never computes coordinates."""

from __future__ import annotations

import math

from cad_harness.domain.errors import InvalidFeatureParametersError
from cad_harness.geometry.primitives import Point2D, require_finite


def bolt_circle(
    center: Point2D,
    pcd_mm: float,
    count: int,
    start_angle_deg: float = 0.0,
    *,
    clockwise: bool = False,
) -> tuple[Point2D, ...]:
    """Return ``count`` hole centres equally spaced on a pitch circle diameter.

    Angles advance counter-clockwise from ``start_angle_deg`` by ``360 / count``.
    Every returned point is exactly ``pcd_mm / 2`` from ``center`` within float
    precision, which the property tests assert.
    """
    require_finite(pcd_mm, start_angle_deg)
    if pcd_mm <= 0.0:
        raise InvalidFeatureParametersError(
            "Pitch circle diameter must be positive", details={"pcd_mm": pcd_mm}
        )
    if count < 1:
        raise InvalidFeatureParametersError(
            "Bolt circle hole count must be a positive integer", details={"count": count}
        )

    radius = pcd_mm / 2.0
    step = 360.0 / count
    direction = -1.0 if clockwise else 1.0
    points: list[Point2D] = []
    for index in range(count):
        angle_deg = start_angle_deg + direction * step * index
        radians = math.radians(angle_deg)
        points.append(
            Point2D(center.x + radius * math.cos(radians), center.y + radius * math.sin(radians))
        )
    return tuple(points)


def rectangular_grid(
    origin: Point2D,
    count_x: int,
    count_y: int,
    pitch_x_mm: float,
    pitch_y_mm: float,
) -> tuple[Point2D, ...]:
    """Row-major grid of points starting at ``origin``.

    Ordering is deterministic (y outer, x inner) because the plan hash depends on it.
    """
    if count_x < 1 or count_y < 1:
        raise InvalidFeatureParametersError(
            "Grid counts must be positive integers",
            details={"count_x": count_x, "count_y": count_y},
        )
    require_finite(pitch_x_mm, pitch_y_mm)
    if (count_x > 1 and pitch_x_mm <= 0.0) or (count_y > 1 and pitch_y_mm <= 0.0):
        raise InvalidFeatureParametersError(
            "Grid pitch must be positive when more than one hole is requested",
            details={"pitch_x_mm": pitch_x_mm, "pitch_y_mm": pitch_y_mm},
        )

    return tuple(
        Point2D(origin.x + ix * pitch_x_mm, origin.y + iy * pitch_y_mm)
        for iy in range(count_y)
        for ix in range(count_x)
    )


def slot_outline(
    center: Point2D,
    length_mm: float,
    width_mm: float,
    angle_deg: float = 0.0,
) -> tuple[Point2D, ...]:
    """Obround slot approximated by its four tangent corner points.

    The arc ends are added by the feature compiler, which knows whether the target
    representation is a polyline with bulges or line/arc entities.
    """
    require_finite(length_mm, width_mm, angle_deg)
    if width_mm <= 0.0:
        raise InvalidFeatureParametersError(
            "Slot width must be positive", details={"width_mm": width_mm}
        )
    if length_mm <= width_mm:
        raise InvalidFeatureParametersError(
            "Slot length must exceed its width, otherwise specify a circular hole",
            details={"length_mm": length_mm, "width_mm": width_mm},
        )

    half_straight = (length_mm - width_mm) / 2.0
    half_width = width_mm / 2.0
    local = (
        Point2D(-half_straight, half_width),
        Point2D(half_straight, half_width),
        Point2D(half_straight, -half_width),
        Point2D(-half_straight, -half_width),
    )
    return tuple(p.rotated(angle_deg).translated(center.x, center.y) for p in local)
