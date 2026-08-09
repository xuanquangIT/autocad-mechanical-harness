"""Pure extraction of annotation measurements from phase-one geometry operations."""

from __future__ import annotations

from dataclasses import dataclass

from cad_harness.domain.models.operation_plan import Operation, OperationType
from cad_harness.geometry.measure import entity_set_bounding_box
from cad_harness.geometry.primitives import Circle2D, Point2D, Polyline2D
from cad_harness.geometry.tolerance import ToleranceProfile


@dataclass(frozen=True, slots=True)
class Hole:
    feature_id: str
    source_operation_id: str
    center: Point2D
    diameter_mm: float


@dataclass(frozen=True, slots=True)
class HoleGroup:
    diameter_mm: float
    holes: tuple[Hole, ...]


@dataclass(frozen=True, slots=True)
class GeometryMeasurements:
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    hole_groups: tuple[HoleGroup, ...]

    @property
    def width_mm(self) -> float:
        return self.max_x - self.min_x

    @property
    def height_mm(self) -> float:
        return self.max_y - self.min_y


def _entities(operation: Operation) -> list[Circle2D | Polyline2D]:
    geometry = operation.geometry
    if operation.type in {OperationType.CREATE_POLYLINE, OperationType.CREATE_CLOSED_POLYLINE}:
        vertices = tuple(Point2D(float(p[0]), float(p[1])) for p in geometry["vertices_mm"])
        return [Polyline2D(vertices, closed=operation.type is OperationType.CREATE_CLOSED_POLYLINE)]
    if operation.type is OperationType.CREATE_CIRCLES:
        diameter = float(geometry["diameter_mm"])
        return [
            Circle2D(Point2D(float(p[0]), float(p[1])), diameter) for p in geometry["centers_mm"]
        ]
    if operation.type is OperationType.CREATE_CIRCLE:
        center = geometry["center_mm"]
        return [
            Circle2D(
                Point2D(float(center[0]), float(center[1])), float(geometry["diameter_mm"]) / 2.0
            )
        ]
    return []


def measure_geometry(
    operations: tuple[Operation, ...],
    *,
    hole_layer: str,
    tolerance: ToleranceProfile,
) -> GeometryMeasurements:
    """Measure extents and hole groups only from compiled geometry."""
    measurable = tuple(entity for operation in operations for entity in _entities(operation))
    box = entity_set_bounding_box(measurable, tolerance)
    holes: list[Hole] = []
    for operation in operations:
        is_hole_geometry = operation.type is OperationType.CREATE_CIRCLES or (
            operation.type is OperationType.CREATE_CIRCLE
            and not operation.operation_id.endswith(":outer")
            and (
                operation.layer == hole_layer
                or "hole" in operation.operation_id
                or "bore" in operation.operation_id
            )
        )
        if not is_hole_geometry:
            continue
        for entity in _entities(operation):
            if isinstance(entity, Circle2D):
                holes.append(
                    Hole(
                        feature_id=operation.feature_id,
                        source_operation_id=operation.operation_id,
                        center=entity.center,
                        diameter_mm=entity.radius_mm * 2.0,
                    )
                )
    holes.sort(key=lambda hole: (hole.diameter_mm, hole.center.x, hole.center.y, hole.feature_id))
    groups: list[list[Hole]] = []
    for hole in holes:
        matching = next(
            (
                group
                for group in groups
                if tolerance.length_close(group[0].diameter_mm, hole.diameter_mm)
            ),
            None,
        )
        if matching is None:
            groups.append([hole])
        else:
            matching.append(hole)
    return GeometryMeasurements(
        min_x=box.min_x,
        min_y=box.min_y,
        max_x=box.max_x,
        max_y=box.max_y,
        hole_groups=tuple(HoleGroup(group[0].diameter_mm, tuple(group)) for group in groups),
    )


def aligned_hole_pairs(
    holes: tuple[Hole, ...], tolerance: ToleranceProfile
) -> tuple[tuple[Hole, Hole], ...]:
    """Return deterministic pairs sharing a projected horizontal or vertical axis."""
    pairs: list[tuple[Hole, Hole]] = []
    for index, first in enumerate(holes):
        for second in holes[index + 1 :]:
            same_x = tolerance.is_coincident(abs(first.center.x - second.center.x))
            same_y = tolerance.is_coincident(abs(first.center.y - second.center.y))
            if same_x or same_y:
                pairs.append((first, second))
    return tuple(pairs)
