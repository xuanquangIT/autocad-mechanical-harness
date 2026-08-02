"""Property-based tests (architecture section 22.3).

These check invariants that must hold for every valid input, not just the examples the
unit tests happen to pick.
"""

from __future__ import annotations

import math

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from cad_harness.domain.canonical import canonical_json, compute_plan_hash
from cad_harness.geometry.intersections import angular_spacing_deg
from cad_harness.geometry.patterns import bolt_circle, rectangular_grid
from cad_harness.geometry.primitives import Point2D, Polyline2D
from cad_harness.geometry.tolerance import DEMO_TOLERANCE

coordinates = st.floats(min_value=-1e4, max_value=1e4, allow_nan=False, allow_infinity=False)
positive_lengths = st.floats(min_value=0.1, max_value=1e4, allow_nan=False, allow_infinity=False)
angles = st.floats(min_value=-360.0, max_value=360.0, allow_nan=False, allow_infinity=False)
hole_counts = st.integers(min_value=1, max_value=64)


@given(cx=coordinates, cy=coordinates, pcd=positive_lengths, count=hole_counts, start=angles)
@settings(max_examples=200, deadline=None)
def test_bolt_circle_points_lie_on_the_pitch_circle(
    cx: float, cy: float, pcd: float, count: int, start: float
) -> None:
    center = Point2D(cx, cy)
    for point in bolt_circle(center, pcd, count, start):
        # Relative comparison: a 10 m PCD cannot be held to a 1 micron absolute check.
        assert math.isclose(center.distance_to(point), pcd / 2.0, rel_tol=1e-9, abs_tol=1e-6)


@given(pcd=positive_lengths, count=hole_counts, start=angles)
@settings(max_examples=200, deadline=None)
def test_bolt_circle_count_is_exact(pcd: float, count: int, start: float) -> None:
    assert len(bolt_circle(Point2D(0.0, 0.0), pcd, count, start)) == count


@given(pcd=positive_lengths, count=st.integers(min_value=2, max_value=36))
@settings(max_examples=100, deadline=None)
def test_bolt_circle_spacing_is_uniform(pcd: float, count: int) -> None:
    center = Point2D(0.0, 0.0)
    points = bolt_circle(center, pcd, count)
    expected_gap = 360.0 / count
    for gap in angular_spacing_deg(center, points):
        assert math.isclose(gap, expected_gap, abs_tol=1e-6)


@given(
    cx=coordinates,
    cy=coordinates,
    pcd=positive_lengths,
    count=hole_counts,
    dx=coordinates,
    dy=coordinates,
    rotation=angles,
)
@settings(max_examples=150, deadline=None)
def test_translation_and_rotation_preserve_intrinsic_radius(
    cx: float, cy: float, pcd: float, count: int, dx: float, dy: float, rotation: float
) -> None:
    """Rigid motion must not change an intrinsic measurement."""
    center = Point2D(cx, cy)
    original = bolt_circle(center, pcd, count)

    moved_center = center.translated(dx, dy).rotated(rotation)
    moved = tuple(p.translated(dx, dy).rotated(rotation) for p in original)

    for point in moved:
        assert math.isclose(moved_center.distance_to(point), pcd / 2.0, rel_tol=1e-7, abs_tol=1e-5)


@given(
    count_x=st.integers(min_value=1, max_value=12),
    count_y=st.integers(min_value=1, max_value=12),
    pitch_x=positive_lengths,
    pitch_y=positive_lengths,
)
@settings(max_examples=150, deadline=None)
def test_rectangular_grid_count_is_the_product(
    count_x: int, count_y: int, pitch_x: float, pitch_y: float
) -> None:
    grid = rectangular_grid(Point2D(0.0, 0.0), count_x, count_y, pitch_x, pitch_y)
    assert len(grid) == count_x * count_y


@given(width=positive_lengths, height=positive_lengths, ox=coordinates, oy=coordinates)
@settings(max_examples=200, deadline=None)
def test_closed_rectangle_area_is_width_times_height(
    width: float, height: float, ox: float, oy: float
) -> None:
    rectangle = Polyline2D(
        (
            Point2D(ox, oy),
            Point2D(ox + width, oy),
            Point2D(ox + width, oy + height),
            Point2D(ox, oy + height),
        ),
        closed=True,
    )
    assert math.isclose(rectangle.area(), width * height, rel_tol=1e-9, abs_tol=1e-6)
    assert rectangle.area() > 0.0


@given(width=positive_lengths, height=positive_lengths)
@settings(max_examples=100, deadline=None)
def test_rectangle_bounding_box_matches_dimensions(width: float, height: float) -> None:
    rectangle = Polyline2D(
        (
            Point2D(0.0, 0.0),
            Point2D(width, 0.0),
            Point2D(width, height),
            Point2D(0.0, height),
        ),
        closed=True,
    )
    box = rectangle.bounding_box()
    assert DEMO_TOLERANCE.length_close(box.width, width)
    assert DEMO_TOLERANCE.length_close(box.height, height)


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=12),
        st.one_of(st.integers(), st.booleans(), st.text(max_size=12), coordinates),
        max_size=8,
    )
)
@settings(max_examples=200, deadline=None)
def test_canonical_json_is_stable_across_key_insertion_order(payload: dict[str, object]) -> None:
    reversed_payload = dict(reversed(list(payload.items())))
    assert canonical_json(payload) == canonical_json(reversed_payload)


@given(
    st.lists(
        st.fixed_dictionaries(
            {
                "operation_id": st.text(min_size=1, max_size=8),
                "diameter_mm": positive_lengths,
            }
        ),
        min_size=1,
        max_size=6,
    )
)
@settings(max_examples=150, deadline=None)
def test_plan_hash_is_deterministic(operations: list[dict[str, object]]) -> None:
    plan = {"operations": operations, "profile_ref": "demo@1.0"}
    assert compute_plan_hash(plan) == compute_plan_hash(dict(plan))


@given(diameter=positive_lengths, delta=st.floats(min_value=0.01, max_value=100.0))
@settings(max_examples=150, deadline=None)
def test_plan_hash_detects_dimensional_change(diameter: float, delta: float) -> None:
    assume(diameter != diameter + delta)
    first = {"operations": [{"diameter_mm": diameter}]}
    second = {"operations": [{"diameter_mm": diameter + delta}]}
    assert compute_plan_hash(first) != compute_plan_hash(second)
