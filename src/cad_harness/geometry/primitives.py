"""2D primitives in canonical millimetres. All types are immutable."""

from __future__ import annotations

import math
from dataclasses import dataclass

from cad_harness.domain.errors import InvalidGeometryError
from cad_harness.geometry.tolerance import ToleranceProfile


def require_finite(*values: float) -> None:
    """Reject NaN/Infinity at the kernel boundary (architecture section 15.1)."""
    for value in values:
        if not math.isfinite(value):
            raise InvalidGeometryError(
                "Non-finite coordinate rejected",
                required_action="Supply finite numeric coordinates",
                details={"value": repr(value)},
            )


@dataclass(frozen=True, slots=True)
class Vector2D:
    dx: float
    dy: float

    def __post_init__(self) -> None:
        require_finite(self.dx, self.dy)

    @property
    def length(self) -> float:
        return math.hypot(self.dx, self.dy)

    def scaled(self, factor: float) -> Vector2D:
        return Vector2D(self.dx * factor, self.dy * factor)

    def normalized(self) -> Vector2D:
        length = self.length
        if length == 0.0:
            raise InvalidGeometryError("Cannot normalize a zero-length vector")
        return Vector2D(self.dx / length, self.dy / length)

    def dot(self, other: Vector2D) -> float:
        return self.dx * other.dx + self.dy * other.dy

    def cross(self, other: Vector2D) -> float:
        return self.dx * other.dy - self.dy * other.dx


@dataclass(frozen=True, slots=True)
class Point2D:
    x: float
    y: float

    def __post_init__(self) -> None:
        require_finite(self.x, self.y)

    def translated(self, dx: float, dy: float) -> Point2D:
        return Point2D(self.x + dx, self.y + dy)

    def rotated(self, angle_deg: float, about: Point2D | None = None) -> Point2D:
        """Rotate counter-clockwise about ``about`` (origin by default)."""
        pivot = about or Point2D(0.0, 0.0)
        radians = math.radians(angle_deg)
        cos_a, sin_a = math.cos(radians), math.sin(radians)
        dx, dy = self.x - pivot.x, self.y - pivot.y
        return Point2D(pivot.x + dx * cos_a - dy * sin_a, pivot.y + dx * sin_a + dy * cos_a)

    def distance_to(self, other: Point2D) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def vector_to(self, other: Point2D) -> Vector2D:
        return Vector2D(other.x - self.x, other.y - self.y)

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass(frozen=True, slots=True)
class BoundingBox:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @classmethod
    def from_points(cls, points: list[Point2D]) -> BoundingBox:
        if not points:
            raise InvalidGeometryError("Cannot build a bounding box from zero points")
        xs = [p.x for p in points]
        ys = [p.y for p in points]
        return cls(min(xs), min(ys), max(xs), max(ys))

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def contains_point(self, point: Point2D, *, margin_mm: float = 0.0) -> bool:
        """True when ``point`` lies inside, shrunk inward by ``margin_mm``."""
        return (
            self.min_x + margin_mm <= point.x <= self.max_x - margin_mm
            and self.min_y + margin_mm <= point.y <= self.max_y - margin_mm
        )

    def distance_to_edge(self, point: Point2D) -> float:
        """Shortest distance from an interior point to any edge (negative if outside)."""
        return min(
            point.x - self.min_x,
            self.max_x - point.x,
            point.y - self.min_y,
            self.max_y - point.y,
        )


@dataclass(frozen=True, slots=True)
class Circle2D:
    center: Point2D
    diameter_mm: float

    def __post_init__(self) -> None:
        require_finite(self.diameter_mm)
        if self.diameter_mm <= 0.0:
            raise InvalidGeometryError(
                "Circle diameter must be positive",
                details={"diameter_mm": self.diameter_mm},
            )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def area_mm2(self) -> float:
        return math.pi * self.radius_mm**2

    def contains_point(self, point: Point2D) -> bool:
        return self.center.distance_to(point) <= self.radius_mm


@dataclass(frozen=True, slots=True)
class Polyline2D:
    vertices: tuple[Point2D, ...]
    closed: bool = False

    def __post_init__(self) -> None:
        if len(self.vertices) < 2:
            raise InvalidGeometryError(
                "A polyline needs at least two vertices",
                details={"vertex_count": len(self.vertices)},
            )

    @property
    def segments(self) -> list[tuple[Point2D, Point2D]]:
        pairs = list(zip(self.vertices, self.vertices[1:], strict=False))
        if self.closed:
            pairs.append((self.vertices[-1], self.vertices[0]))
        return pairs

    def perimeter(self) -> float:
        return sum(a.distance_to(b) for a, b in self.segments)

    def signed_area(self) -> float:
        """Shoelace area. Positive for counter-clockwise vertex order."""
        total = 0.0
        vertices = list(self.vertices)
        for a, b in zip(vertices, vertices[1:] + vertices[:1], strict=True):
            total += a.x * b.y - b.x * a.y
        return total / 2.0

    def area(self) -> float:
        """Absolute enclosed area. Meaningful only for a closed, simple polyline."""
        return abs(self.signed_area())

    def bounding_box(self) -> BoundingBox:
        return BoundingBox.from_points(list(self.vertices))

    def has_zero_length_segment(self, tolerance: ToleranceProfile) -> bool:
        return any(tolerance.is_zero_length(a.distance_to(b)) for a, b in self.segments)
