"""Exact signed areas for mixed line/curve contours and nested contour forests."""

from __future__ import annotations

import math
from dataclasses import dataclass

from cad_harness.domain.errors import InvalidGeometryError
from cad_harness.geometry.curves import CurveParams, linearize_curve
from cad_harness.geometry.predicates import contains_contour
from cad_harness.geometry.primitives import Point2D, Polyline2D
from cad_harness.geometry.tolerance import ToleranceProfile


@dataclass(frozen=True, slots=True)
class LineEdge:
    start: Point2D
    end: Point2D


type ContourEdge = LineEdge | CurveParams


@dataclass(frozen=True, slots=True)
class CurveContour:
    """Ordered line/curve boundary; every edge endpoint must meet the next."""

    edges: tuple[ContourEdge, ...]

    def __post_init__(self) -> None:
        if not self.edges:
            raise InvalidGeometryError("A contour needs at least one edge")

    def vertices(self, chord_tolerance_mm: float = 0.01) -> tuple[Point2D, ...]:
        points: list[Point2D] = []
        for edge in self.edges:
            edge_points = (
                (edge.start, edge.end)
                if isinstance(edge, LineEdge)
                else linearize_curve(edge, chord_tolerance_mm)
            )
            if not points:
                points.extend(edge_points)
            else:
                points.extend(edge_points[1:])
        if len(points) > 1 and points[0].distance_to(points[-1]) <= chord_tolerance_mm:
            points.pop()
        return tuple(points)


type Contour = Polyline2D | CurveContour


def _cross(a: Point2D, b: Point2D) -> float:
    return a.x * b.y - b.x * a.y


def signed_contour_area(contour: Contour) -> float:
    """Green's theorem; curved-edge contributions are analytic, not tessellated."""
    if isinstance(contour, Polyline2D):
        if not contour.closed:
            raise InvalidGeometryError("Area is undefined for an open contour")
        return contour.signed_area()

    total = 0.0
    for edge in contour.edges:
        if isinstance(edge, LineEdge):
            total += _cross(edge.start, edge.end) / 2.0
            continue
        delta = math.radians(edge.sweep_deg)
        if edge.radius_mm is not None:
            axis_product = edge.radius_mm**2
        else:
            assert edge.semi_major_mm is not None and edge.semi_minor_mm is not None
            axis_product = edge.semi_major_mm * edge.semi_minor_mm
        total += (_cross(edge.center, edge.end_point) - _cross(edge.center, edge.start_point)) / 2.0
        total += axis_product * delta / 2.0
    return total


def contour_area(contour: Contour) -> float:
    return abs(signed_contour_area(contour))


def _ellipse_arc_length(curve: CurveParams) -> float:
    assert curve.semi_major_mm is not None and curve.semi_minor_mm is not None
    # Bound to locals: attribute narrowing does not carry into the nested closure.
    semi_major = curve.semi_major_mm
    semi_minor = curve.semi_minor_mm
    start = math.radians(curve.start_angle_deg)
    end = math.radians(curve.end_angle_deg)
    # Fixed even subdivision keeps output deterministic. Simpson convergence is fast
    # for this smooth periodic integrand, including highly eccentric ellipses.
    count = 4096
    step = (end - start) / count

    def speed(parameter: float) -> float:
        return math.hypot(semi_major * math.sin(parameter), semi_minor * math.cos(parameter))

    total = speed(start) + speed(end)
    for index in range(1, count):
        total += (4.0 if index % 2 else 2.0) * speed(start + index * step)
    return abs(total * step / 3.0)


def curve_length(curve: CurveParams) -> float:
    if curve.radius_mm is not None:
        return curve.radius_mm * abs(math.radians(curve.sweep_deg))
    return _ellipse_arc_length(curve)


def contour_perimeter(contour: Contour) -> float:
    if isinstance(contour, Polyline2D):
        if not contour.closed:
            raise InvalidGeometryError("Perimeter is undefined for an open contour")
        return contour.perimeter()
    return sum(
        edge.start.distance_to(edge.end) if isinstance(edge, LineEdge) else curve_length(edge)
        for edge in contour.edges
    )


def _as_polyline(contour: Contour, tolerance: ToleranceProfile) -> Polyline2D:
    if isinstance(contour, Polyline2D):
        return contour
    return Polyline2D(contour.vertices(tolerance.arc_chord_tolerance_mm), closed=True)


@dataclass(frozen=True, slots=True)
class ContourNode:
    contour: Contour
    parent_index: int | None
    depth: int
    area_mm2: float


@dataclass(frozen=True, slots=True)
class ContourForest:
    """Deterministic containment forest with even/odd material semantics."""

    nodes: tuple[ContourNode, ...]

    @classmethod
    def build(cls, contours: tuple[Contour, ...], tolerance: ToleranceProfile) -> ContourForest:
        if any(isinstance(contour, Polyline2D) and not contour.closed for contour in contours):
            raise InvalidGeometryError("Contour forests accept only closed contours")
        areas = tuple(contour_area(contour) for contour in contours)
        polylines = tuple(_as_polyline(contour, tolerance) for contour in contours)
        parents: list[int | None] = []
        for index, child in enumerate(polylines):
            containers = [
                candidate
                for candidate, parent in enumerate(polylines)
                if candidate != index
                and areas[candidate] > areas[index]
                and contains_contour(parent, child, tolerance)
            ]
            parents.append(min(containers, key=lambda item: (areas[item], item), default=None))

        depths: list[int] = []
        for index in range(len(contours)):
            depth = 0
            parent = parents[index]
            seen = {index}
            while parent is not None:
                if parent in seen:
                    raise InvalidGeometryError("Contour containment cycle detected")
                seen.add(parent)
                depth += 1
                parent = parents[parent]
            depths.append(depth)
        return cls(
            tuple(
                ContourNode(contour, parents[index], depths[index], areas[index])
                for index, contour in enumerate(contours)
            )
        )

    @property
    def roots(self) -> tuple[ContourNode, ...]:
        return tuple(node for node in self.nodes if node.parent_index is None)

    @property
    def max_depth(self) -> int:
        return max((node.depth for node in self.nodes), default=0)

    @property
    def net_area_mm2(self) -> float:
        return sum(node.area_mm2 * (1.0 if node.depth % 2 == 0 else -1.0) for node in self.nodes)

    @property
    def total_perimeter_mm(self) -> float:
        return sum(contour_perimeter(node.contour) for node in self.nodes)
