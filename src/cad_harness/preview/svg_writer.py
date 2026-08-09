"""Minimal dependency-free SVG preview for quick human review.

Deliberately hand-rolled: the SVG is for eyeballing only. Pass/fail decisions come
from measurements and rules, never from an image.
"""

from __future__ import annotations

from pathlib import Path

from cad_harness.domain.models.operation_plan import OperationPlan, OperationType
from cad_harness.geometry.primitives import BoundingBox, Point2D

STROKE_NEW = "#2e7d32"
STROKE_MODIFIED = "#f9a825"
STROKE_DELETED = "#c62828"
MARGIN_MM = 20.0


def _plan_points(plan: OperationPlan) -> list[Point2D]:
    points: list[Point2D] = []
    for operation in plan.operations:
        for key in ("vertices_mm", "centers_mm"):
            for raw in operation.geometry.get(key, []):
                points.append(Point2D(float(raw[0]), float(raw[1])))
        for key in ("center_mm", "start_mm", "end_mm", "position_mm", "text_position_mm"):
            raw = operation.geometry.get(key)
            if raw is not None:
                points.append(Point2D(float(raw[0]), float(raw[1])))
    return points


def write_svg(plan: OperationPlan, target: Path, *, scale: float = 2.0) -> Path:
    """Write a flat SVG of ``plan``. Y is flipped so the drawing reads correctly."""
    points = _plan_points(plan)
    box = BoundingBox.from_points(points) if points else BoundingBox(0.0, 0.0, 100.0, 100.0)
    width = (box.width + 2 * MARGIN_MM) * scale
    height = (box.height + 2 * MARGIN_MM) * scale

    def sx(x: float) -> float:
        return (x - box.min_x + MARGIN_MM) * scale

    def sy(y: float) -> float:
        return (box.max_y - y + MARGIN_MM) * scale

    stroke_width = max(0.6, 0.35 * scale)
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.1f}" height="{height:.1f}" '
        f'viewBox="0 0 {width:.1f} {height:.1f}" role="img" '
        f'aria-label="Preview of operation plan {plan.plan_id}">',
        f"<title>Operation plan preview {plan.plan_id}</title>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<g fill="none" stroke="{STROKE_NEW}" stroke-width="{stroke_width:.2f}" '
        'stroke-linejoin="round">',
    ]

    for operation in plan.operations:
        if operation.type in {
            OperationType.CREATE_CLOSED_POLYLINE,
            OperationType.CREATE_POLYLINE,
        }:
            coords = " ".join(
                f"{sx(float(v[0])):.2f},{sy(float(v[1])):.2f}"
                for v in operation.geometry["vertices_mm"]
            )
            tag = (
                "polygon" if operation.type is OperationType.CREATE_CLOSED_POLYLINE else "polyline"
            )
            parts.append(f'<{tag} points="{coords}"/>')
        elif operation.type is OperationType.CREATE_CIRCLES:
            radius = float(operation.geometry["diameter_mm"]) / 2.0 * scale
            for center in operation.geometry["centers_mm"]:
                parts.append(
                    f'<circle cx="{sx(float(center[0])):.2f}" cy="{sy(float(center[1])):.2f}" '
                    f'r="{radius:.2f}"/>'
                )
        elif operation.type is OperationType.CREATE_CIRCLE:
            center = operation.geometry["center_mm"]
            radius = float(operation.geometry["diameter_mm"]) / 2.0 * scale
            parts.append(
                f'<circle cx="{sx(float(center[0])):.2f}" cy="{sy(float(center[1])):.2f}" '
                f'r="{radius:.2f}"/>'
            )
        elif operation.type is OperationType.CREATE_ARC:
            from cad_harness.geometry.curves import linearize_curve, normalize_arc

            center = operation.geometry["center_mm"]
            curve = normalize_arc(
                Point2D(float(center[0]), float(center[1])),
                float(operation.geometry["radius_mm"]),
                float(operation.geometry["start_angle_deg"]),
                float(operation.geometry["end_angle_deg"]),
            )
            arc_points = linearize_curve(curve, 0.01)
            coords = " ".join(f"{sx(point.x):.2f},{sy(point.y):.2f}" for point in arc_points)
            parts.append(f'<polyline points="{coords}"/>')
        elif operation.type in {
            OperationType.CREATE_LINE,
            OperationType.CREATE_CENTERLINE,
            OperationType.CREATE_LINEAR_DIMENSION,
            OperationType.CREATE_ALIGNED_DIMENSION,
        }:
            start, end = operation.geometry["start_mm"], operation.geometry["end_mm"]
            parts.append(
                f'<line x1="{sx(float(start[0])):.2f}" y1="{sy(float(start[1])):.2f}" '
                f'x2="{sx(float(end[0])):.2f}" y2="{sy(float(end[1])):.2f}"/>'
            )
        elif operation.type is OperationType.CREATE_TEXT:
            position = operation.geometry["position_mm"]
            parts.append(
                f'<text x="{sx(float(position[0])):.2f}" y="{sy(float(position[1])):.2f}" '
                f'fill="{STROKE_NEW}" stroke="none">{operation.geometry["text"]}</text>'
            )

    parts.append("</g></svg>")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(parts), encoding="utf-8")
    return target
