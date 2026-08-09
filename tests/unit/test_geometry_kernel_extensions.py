"""Focused examples for the extended geometry kernel (task 8)."""

from __future__ import annotations

import math

import pytest

from cad_harness.domain.errors import InvalidFeatureParametersError
from cad_harness.geometry.areas import (
    ContourForest,
    CurveContour,
    LineEdge,
    contour_area,
    contour_perimeter,
)
from cad_harness.geometry.curves import (
    chord_error_bound,
    chord_segment_count,
    normalize_arc,
    normalize_bulge,
    normalize_circle,
)
from cad_harness.geometry.fillet_chamfer import chamfer_vertex, fillet_is_tangent, fillet_vertex
from cad_harness.geometry.measure import (
    center_center_distance,
    entity_entity_distance,
    entity_set_bounding_box,
    hole_boundary_distance,
    line_angle_deg,
    point_entity_distance,
)
from cad_harness.geometry.predicates import contains_contour, is_closed_within, self_intersects
from cad_harness.geometry.primitives import Circle2D, Point2D, Polyline2D
from cad_harness.geometry.tolerance import DEMO_TOLERANCE
from cad_harness.geometry.transforms import rotate_about, translate


def square(origin: float, side: float) -> Polyline2D:
    return Polyline2D(
        (
            Point2D(origin, origin),
            Point2D(origin + side, origin),
            Point2D(origin + side, origin + side),
            Point2D(origin, origin + side),
        ),
        closed=True,
    )


class TestCurvesAndAreas:
    def test_bulge_normalizes_to_ccw_semicircle(self) -> None:
        curve = normalize_bulge(Point2D(-5.0, 0.0), Point2D(5.0, 0.0), 1.0)
        assert DEMO_TOLERANCE.length_close(curve.center.x, 0.0)
        assert DEMO_TOLERANCE.length_close(curve.center.y, 0.0)
        assert DEMO_TOLERANCE.length_close(curve.radius_mm or 0.0, 5.0)
        assert DEMO_TOLERANCE.angle_close_deg(curve.sweep_deg, 180.0)

    def test_segment_count_is_deterministic_and_meets_sagitta(self) -> None:
        curve = normalize_circle(Point2D(0.0, 0.0), 100.0)
        first = chord_segment_count(curve, 0.01)
        assert first == chord_segment_count(curve, 0.01)
        assert chord_error_bound(curve, first) <= 0.01

    def test_semicircular_contour_has_exact_area_and_perimeter(self) -> None:
        arc = normalize_arc(Point2D(0.0, 0.0), 5.0, 0.0, sweep_deg=180.0)
        contour = CurveContour((LineEdge(Point2D(-5.0, 0.0), Point2D(5.0, 0.0)), arc))
        assert DEMO_TOLERANCE.area_close(contour_area(contour), math.pi * 25.0 / 2.0)
        assert DEMO_TOLERANCE.length_close(contour_perimeter(contour), 10.0 + math.pi * 5.0)

    def test_contour_forest_uses_even_odd_depth(self) -> None:
        forest = ContourForest.build(
            (square(0.0, 20.0), square(5.0, 10.0), square(8.0, 4.0)), DEMO_TOLERANCE
        )
        assert [node.depth for node in forest.nodes] == [0, 1, 2]
        assert DEMO_TOLERANCE.area_close(forest.net_area_mm2, 400.0 - 100.0 + 16.0)


class TestFilletAndChamfer:
    def test_right_angle_fillet_is_tangent(self) -> None:
        previous = Point2D(0.0, 0.0)
        vertex = Point2D(10.0, 0.0)
        following = Point2D(10.0, 10.0)
        result = fillet_vertex(previous, vertex, following, 2.0, DEMO_TOLERANCE)
        # Tangent points are trigonometric results, so they land a few ULPs off the exact
        # value. Non-negotiable 8: assert through a tolerance predicate, never with ``==``.
        assert DEMO_TOLERANCE.is_coincident(result.tangent_in.distance_to(Point2D(8.0, 0.0)))
        assert DEMO_TOLERANCE.is_coincident(result.tangent_out.distance_to(Point2D(10.0, 2.0)))
        assert fillet_is_tangent(result, previous, vertex, following, DEMO_TOLERANCE)

    def test_radius_above_half_shorter_edge_is_rejected(self) -> None:
        with pytest.raises(InvalidFeatureParametersError) as excinfo:
            fillet_vertex(
                Point2D(0.0, 0.0),
                Point2D(10.0, 0.0),
                Point2D(10.0, 10.0),
                5.1,
                DEMO_TOLERANCE,
            )
        assert DEMO_TOLERANCE.length_close(float(excinfo.value.details["maximum_radius_mm"]), 5.0)

    def test_chamfer_supports_both_parameterizations(self) -> None:
        points = (Point2D(0.0, 0.0), Point2D(10.0, 0.0), Point2D(10.0, 10.0))
        distances = chamfer_vertex(*points, 2.0, distance_second_mm=3.0, tolerance=DEMO_TOLERANCE)
        angled = chamfer_vertex(*points, 2.0, angle_deg=45.0, tolerance=DEMO_TOLERANCE)
        assert DEMO_TOLERANCE.length_close(distances.distance_second_mm, 3.0)
        assert DEMO_TOLERANCE.length_close(angled.distance_second_mm, 2.0)


class TestTransformsMeasurementsAndPredicates:
    def test_translate_and_rotate_cover_circle_polyline_and_curve(self) -> None:
        circle = Circle2D(Point2D(2.0, 3.0), 10.0)
        outline = square(0.0, 10.0)
        curve = normalize_arc(Point2D(0.0, 0.0), 3.0, 10.0, sweep_deg=90.0)
        moved_circle = translate(circle, 4.0, -2.0)
        rotated_outline = rotate_about(outline, 90.0, Point2D(0.0, 0.0))
        rotated_curve = rotate_about(curve, 90.0, Point2D(0.0, 0.0))
        assert isinstance(moved_circle, Circle2D)
        assert DEMO_TOLERANCE.is_coincident(moved_circle.center.distance_to(Point2D(6.0, 1.0)))
        assert isinstance(rotated_outline, Polyline2D)
        assert DEMO_TOLERANCE.area_close(rotated_outline.area(), outline.area())
        assert DEMO_TOLERANCE.angle_close_deg(rotated_curve.start_angle_deg, 100.0)  # type: ignore[union-attr]

    def test_measurement_surface(self) -> None:
        boundary = square(0.0, 20.0)
        first = Circle2D(Point2D(5.0, 5.0), 4.0)
        second = Circle2D(Point2D(15.0, 5.0), 4.0)
        assert DEMO_TOLERANCE.length_close(
            point_entity_distance(Point2D(5.0, 5.0), first, DEMO_TOLERANCE), 2.0
        )
        assert DEMO_TOLERANCE.length_close(
            entity_entity_distance(first, second, DEMO_TOLERANCE), 6.0
        )
        assert DEMO_TOLERANCE.length_close(center_center_distance(first, second), 10.0)
        assert DEMO_TOLERANCE.length_close(
            hole_boundary_distance(first, boundary, DEMO_TOLERANCE), 3.0
        )
        assert DEMO_TOLERANCE.angle_close_deg(
            line_angle_deg(
                Point2D(0.0, 0.0),
                Point2D(5.0, 0.0),
                Point2D(0.0, 0.0),
                Point2D(0.0, 5.0),
                DEMO_TOLERANCE,
            ),
            90.0,
        )
        box = entity_set_bounding_box((first, second, boundary), DEMO_TOLERANCE)
        for actual, expected in zip(
            (box.min_x, box.min_y, box.max_x, box.max_y), (0.0, 0.0, 20.0, 20.0), strict=True
        ):
            assert DEMO_TOLERANCE.length_close(actual, expected)

    def test_closure_intersection_and_containment_predicates(self) -> None:
        outer = square(0.0, 20.0)
        inner = square(5.0, 3.0)
        open_but_coincident = Polyline2D(
            (Point2D(0.0, 0.0), Point2D(2.0, 0.0), Point2D(0.0, 0.0)), closed=False
        )
        bowtie = Polyline2D(
            (Point2D(0.0, 0.0), Point2D(2.0, 2.0), Point2D(2.0, 0.0), Point2D(0.0, 2.0)),
            closed=True,
        )
        assert is_closed_within(open_but_coincident, DEMO_TOLERANCE)
        assert self_intersects(bowtie, DEMO_TOLERANCE)
        assert contains_contour(outer, inner, DEMO_TOLERANCE)
