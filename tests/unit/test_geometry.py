"""Geometry kernel unit tests."""

from __future__ import annotations

import math

import pytest

from cad_harness.domain.errors import InvalidFeatureParametersError, InvalidGeometryError
from cad_harness.geometry.intersections import angular_spacing_deg, segment_intersection
from cad_harness.geometry.patterns import bolt_circle, rectangular_grid, slot_outline
from cad_harness.geometry.predicates import (
    is_orthogonal_rectangle,
    minimum_edge_distance,
    polyline_self_intersects,
)
from cad_harness.geometry.primitives import Circle2D, Point2D, Polyline2D
from cad_harness.geometry.tolerance import DEMO_TOLERANCE


class TestBoltCircle:
    def test_hole_count_and_radius(self) -> None:
        centers = bolt_circle(Point2D(0.0, 0.0), pcd_mm=120.0, count=8)
        assert len(centers) == 8
        for center in centers:
            assert DEMO_TOLERANCE.length_close(center.distance_to(Point2D(0.0, 0.0)), 60.0)

    def test_angular_spacing_is_uniform(self) -> None:
        center = Point2D(10.0, -5.0)
        centers = bolt_circle(center, pcd_mm=120.0, count=6)
        for gap in angular_spacing_deg(center, centers):
            assert DEMO_TOLERANCE.angle_close_deg(gap, 60.0)

    def test_start_angle_positions_first_hole(self) -> None:
        centers = bolt_circle(Point2D(0.0, 0.0), pcd_mm=100.0, count=4, start_angle_deg=90.0)
        assert DEMO_TOLERANCE.length_close(centers[0].x, 0.0)
        assert DEMO_TOLERANCE.length_close(centers[0].y, 50.0)

    def test_clockwise_reverses_direction(self) -> None:
        ccw = bolt_circle(Point2D(0.0, 0.0), 100.0, 4)
        cw = bolt_circle(Point2D(0.0, 0.0), 100.0, 4, clockwise=True)
        assert DEMO_TOLERANCE.length_close(ccw[1].y, 50.0)
        assert DEMO_TOLERANCE.length_close(cw[1].y, -50.0)

    @pytest.mark.parametrize(("pcd", "count"), [(0.0, 4), (-10.0, 4), (100.0, 0), (100.0, -1)])
    def test_invalid_parameters_are_rejected(self, pcd: float, count: int) -> None:
        with pytest.raises(InvalidFeatureParametersError):
            bolt_circle(Point2D(0.0, 0.0), pcd, count)


class TestRectangularGrid:
    def test_reference_case_hole_centers(self) -> None:
        """160x100 plate, 20 mm edge offsets, 2x2 holes -> the documented coordinates."""
        centers = rectangular_grid(Point2D(20.0, 20.0), 2, 2, 120.0, 60.0)
        assert [c.as_tuple() for c in centers] == [
            (20.0, 20.0),
            (140.0, 20.0),
            (20.0, 80.0),
            (140.0, 80.0),
        ]

    def test_single_hole_needs_no_pitch(self) -> None:
        centers = rectangular_grid(Point2D(5.0, 7.0), 1, 1, 0.0, 0.0)
        assert centers == (Point2D(5.0, 7.0),)

    def test_ordering_is_row_major(self) -> None:
        """Order is part of the plan hash, so it is pinned by a test."""
        centers = rectangular_grid(Point2D(0.0, 0.0), 3, 2, 10.0, 20.0)
        assert [c.as_tuple() for c in centers] == [
            (0.0, 0.0),
            (10.0, 0.0),
            (20.0, 0.0),
            (0.0, 20.0),
            (10.0, 20.0),
            (20.0, 20.0),
        ]

    def test_zero_pitch_with_multiple_holes_is_rejected(self) -> None:
        with pytest.raises(InvalidFeatureParametersError):
            rectangular_grid(Point2D(0.0, 0.0), 2, 2, 0.0, 10.0)


class TestSlotOutline:
    def test_corner_points(self) -> None:
        corners = slot_outline(Point2D(0.0, 0.0), length_mm=40.0, width_mm=10.0)
        assert [c.as_tuple() for c in corners] == [
            (-15.0, 5.0),
            (15.0, 5.0),
            (15.0, -5.0),
            (-15.0, -5.0),
        ]

    def test_length_must_exceed_width(self) -> None:
        with pytest.raises(InvalidFeatureParametersError):
            slot_outline(Point2D(0.0, 0.0), length_mm=10.0, width_mm=10.0)


class TestPrimitives:
    def test_non_finite_coordinates_are_rejected(self) -> None:
        with pytest.raises(InvalidGeometryError):
            Point2D(float("nan"), 0.0)
        with pytest.raises(InvalidGeometryError):
            Point2D(0.0, float("inf"))

    def test_zero_diameter_circle_is_rejected(self) -> None:
        with pytest.raises(InvalidGeometryError):
            Circle2D(Point2D(0.0, 0.0), 0.0)

    def test_closed_polyline_area_and_perimeter(self) -> None:
        plate = Polyline2D(
            (Point2D(0, 0), Point2D(160, 0), Point2D(160, 100), Point2D(0, 100)), closed=True
        )
        assert DEMO_TOLERANCE.area_close(plate.area(), 16000.0)
        assert DEMO_TOLERANCE.length_close(plate.perimeter(), 520.0)

    def test_bounding_box_edge_distance(self) -> None:
        plate = Polyline2D(
            (Point2D(0, 0), Point2D(160, 0), Point2D(160, 100), Point2D(0, 100)), closed=True
        )
        hole = Circle2D(Point2D(20.0, 20.0), 14.0)
        assert DEMO_TOLERANCE.length_close(minimum_edge_distance(hole, plate), 13.0)


class TestPredicates:
    def test_rectangle_detection(self) -> None:
        rectangle = Polyline2D(
            (Point2D(0, 0), Point2D(10, 0), Point2D(10, 5), Point2D(0, 5)), closed=True
        )
        skewed = Polyline2D(
            (Point2D(0, 0), Point2D(10, 1), Point2D(10, 5), Point2D(0, 5)), closed=True
        )
        assert is_orthogonal_rectangle(rectangle, DEMO_TOLERANCE)
        assert not is_orthogonal_rectangle(skewed, DEMO_TOLERANCE)

    def test_self_intersection_detection(self) -> None:
        simple = Polyline2D(
            (Point2D(0, 0), Point2D(10, 0), Point2D(10, 10), Point2D(0, 10)), closed=True
        )
        bowtie = Polyline2D(
            (Point2D(0, 0), Point2D(10, 10), Point2D(10, 0), Point2D(0, 10)), closed=True
        )
        assert not polyline_self_intersects(simple, DEMO_TOLERANCE)
        assert polyline_self_intersects(bowtie, DEMO_TOLERANCE)

    def test_segment_intersection(self) -> None:
        crossing = segment_intersection(
            Point2D(0, 0), Point2D(10, 10), Point2D(0, 10), Point2D(10, 0), DEMO_TOLERANCE
        )
        assert crossing is not None
        assert DEMO_TOLERANCE.length_close(crossing.x, 5.0)

        parallel = segment_intersection(
            Point2D(0, 0), Point2D(10, 0), Point2D(0, 5), Point2D(10, 5), DEMO_TOLERANCE
        )
        assert parallel is None


class TestTolerance:
    def test_angle_comparison_wraps(self) -> None:
        assert DEMO_TOLERANCE.angle_close_deg(359.99999, 0.0)
        assert not DEMO_TOLERANCE.angle_close_deg(0.0, 1.0)

    def test_length_comparison_uses_tolerance_not_equality(self) -> None:
        drifted = 0.1 + 0.2
        assert drifted != 0.3  # binary floating point
        assert DEMO_TOLERANCE.length_close(drifted, 0.3)

    def test_pi_derived_area_matches_within_tolerance(self) -> None:
        circle = Circle2D(Point2D(0.0, 0.0), 14.0)
        assert DEMO_TOLERANCE.area_close(circle.area_mm2, math.pi * 49.0)
