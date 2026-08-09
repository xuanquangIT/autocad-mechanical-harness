"""Sanity checks for the shared generators in ``strategies.py``.

These are *not* correctness properties from design.md - they are the contract of the
test infrastructure itself. Every property test relies on these guarantees (an outline
is closed and simple, an escaping path really escapes), so a broken generator would
silently weaken the whole suite instead of failing loudly.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

from hypothesis import find, given, settings
from hypothesis import strategies as st
from tests.property.strategies import (
    MAX_SHORT_EDGE_MM,
    ArcCase,
    AuditEventSequenceCase,
    BaselineSetCase,
    BulgeCase,
    CircleCase,
    EllipseCase,
    absolute_outside_paths,
    audit_event_sequences,
    baseline_sets,
    contained_paths,
    curve_params,
    escaping_paths,
    nested_contour_forests,
    outlines,
    outlines_with_narrow_angle,
    outlines_with_short_edge,
    rigid_motions,
    takeoff_requests,
    traversal_paths,
    unc_paths,
)

from cad_harness.geometry.predicates import polyline_self_intersects
from cad_harness.geometry.primitives import Point2D, Polyline2D
from cad_harness.geometry.tolerance import DEMO_TOLERANCE
from cad_harness.observability.audit import AuditEventType

#: A root that need not exist: ``resolve(strict=False)`` is purely lexical for it.
FAKE_ROOT = Path(tempfile.gettempdir()) / "cad-harness-strategy-root"


@given(case=audit_event_sequences())
@settings(max_examples=100, deadline=None)
def test_audit_event_sequences_are_ordered_and_keep_valid_engineer_intervals(
    case: AuditEventSequenceCase,
) -> None:
    assert case.events[0].event_type == AuditEventType.JOB_CREATED.value
    assert case.events[-1].event_type == AuditEventType.COMMIT_SUCCEEDED.value
    assert [event.created_at for event in case.events] == sorted(
        event.created_at for event in case.events
    )
    assert all(item.started_at <= item.ended_at for item in case.engineer_activity)


@given(case=baseline_sets())
@settings(max_examples=100, deadline=None)
def test_baseline_sets_align_every_generated_effort_to_one_unique_case(
    case: BaselineSetCase,
) -> None:
    baseline_ids = [item.case_id for item in case.baseline]
    effort_ids = [item.case_id for item in case.efforts]
    assert len(baseline_ids) == len(set(baseline_ids))
    assert effort_ids == baseline_ids
    assert {item.work_label for item in case.baseline} <= {"ve_moi", "sua_ban_co_san"}


def _interior_angles_deg(outline: Polyline2D) -> list[float]:
    vertices = outline.vertices
    count = len(vertices)
    angles: list[float] = []
    for index in range(count):
        previous = vertices[(index - 1) % count]
        current = vertices[index]
        following = vertices[(index + 1) % count]
        first = current.vector_to(previous)
        second = current.vector_to(following)
        if first.length == 0.0 or second.length == 0.0:
            continue
        cosine = first.dot(second) / (first.length * second.length)
        angles.append(math.degrees(math.acos(max(-1.0, min(1.0, cosine)))))
    return angles


@given(outline=outlines())
@settings(max_examples=100, deadline=None)
def test_outlines_are_closed_simple_polygons_with_positive_area(outline: Polyline2D) -> None:
    assert outline.closed
    assert len(outline.vertices) >= 3
    assert outline.area() > 0.0
    assert not polyline_self_intersects(outline, DEMO_TOLERANCE)


@given(outline=outlines_with_short_edge())
@settings(max_examples=100, deadline=None)
def test_short_edge_outlines_contain_an_edge_at_or_below_zero_length_tolerance(
    outline: Polyline2D,
) -> None:
    shortest = min(a.distance_to(b) for a, b in outline.segments)
    # The inserted vertex is placed by normalising and scaling, so the measured length
    # lands within a few ULPs of the requested epsilon rather than exactly on it.
    assert shortest <= MAX_SHORT_EDGE_MM * (1.0 + 1.0e-9)
    assert not polyline_self_intersects(outline, DEMO_TOLERANCE)


@given(outline=outlines_with_narrow_angle())
@settings(max_examples=100, deadline=None)
def test_narrow_angle_outlines_contain_an_angle_below_one_degree(outline: Polyline2D) -> None:
    assert min(_interior_angles_deg(outline)) < 1.0
    assert not polyline_self_intersects(outline, DEMO_TOLERANCE)


@given(motion=rigid_motions(), point=st.builds(Point2D, st.just(3.0), st.just(-7.0)))
@settings(max_examples=100, deadline=None)
def test_rigid_motions_preserve_distance_from_the_transformed_origin(motion, point) -> None:
    """A generator that silently scaled would invalidate every invariance property."""
    origin = Point2D(0.0, 0.0)
    assert DEMO_TOLERANCE.length_close(
        motion.apply(origin).distance_to(motion.apply(point)), origin.distance_to(point)
    )


@given(candidate=contained_paths())
@settings(max_examples=100, deadline=None)
def test_contained_paths_stay_inside_the_root_they_are_joined_to(candidate: Path) -> None:
    assert not candidate.is_absolute()
    assert ".." not in candidate.parts
    assert (FAKE_ROOT / candidate).resolve().is_relative_to(FAKE_ROOT.resolve())


@given(candidate=traversal_paths())
@settings(max_examples=100, deadline=None)
def test_traversal_paths_resolve_outside_the_root(candidate: Path) -> None:
    assert ".." in candidate.parts
    assert not (FAKE_ROOT / candidate).resolve().is_relative_to(FAKE_ROOT.resolve())


@given(candidate=st.one_of(unc_paths(), absolute_outside_paths()))
@settings(max_examples=100, deadline=None)
def test_absolute_candidates_ignore_the_root_and_stay_outside_it(candidate: Path) -> None:
    # Deliberately lexical: resolving a UNC name would reach for the network.
    assert candidate.is_absolute()
    assert FAKE_ROOT / candidate == candidate
    assert not candidate.is_relative_to(FAKE_ROOT)


@given(case=curve_params())
@settings(max_examples=100, deadline=None)
def test_curve_cases_are_measurable(case) -> None:
    match case:
        case CircleCase():
            assert case.radius_mm > 0.0
        case ArcCase():
            assert case.radius_mm > 0.0
            assert 0.0 < case.sweep_deg <= 360.0
            assert case.end_angle_deg > case.start_angle_deg
        case EllipseCase():
            assert case.semi_major_mm >= case.semi_minor_mm > 0.0
            assert 0.0 < case.axis_ratio <= 1.0
        case BulgeCase():
            assert case.bulge != 0.0
            assert case.start.distance_to(case.end) > 0.0


def test_generators_reach_the_edge_cases_the_design_names() -> None:
    """``find`` fails loudly if a documented edge case became unreachable."""
    find(escaping_paths(), lambda p: ".." in p.parts)
    find(escaping_paths(), lambda p: str(p).startswith("\\\\") or str(p).startswith("//"))
    find(contained_paths(), lambda p: not p.name.isascii())
    find(curve_params(), lambda c: isinstance(c, BulgeCase) and c.bulge < 0.0)
    find(curve_params(), lambda c: isinstance(c, ArcCase) and c.sweep_deg > 359.0)
    find(curve_params(), lambda c: isinstance(c, EllipseCase) and c.axis_ratio > 0.99)


@given(case=nested_contour_forests())
@settings(max_examples=100, deadline=None)
def test_nested_contour_forests_have_requested_depth_and_known_area(case) -> None:
    assert case.forest.max_depth == case.requested_depth
    assert DEMO_TOLERANCE.area_close(case.forest.net_area_mm2, case.expected_net_area_mm2)


def test_nested_contour_generator_reaches_hole_in_island_and_depth_three() -> None:
    find(nested_contour_forests(), lambda case: case.requested_depth == 2)
    find(nested_contour_forests(), lambda case: case.requested_depth == 3)


def test_takeoff_requests_reach_documented_input_boundaries() -> None:
    find(takeoff_requests(), lambda request: request.parts[0].thickness_mm == 0.5)
    find(takeoff_requests(), lambda request: request.parts[0].thickness_mm == 500.0)
    find(takeoff_requests(), lambda request: request.parts[0].quantity == 1)
    find(takeoff_requests(), lambda request: request.parts[0].quantity == 100_000)
    find(takeoff_requests(), lambda request: request.parts[0].stock_allowance_mm is None)
    find(takeoff_requests(), lambda request: request.parts[0].stock_allowance_mm == 0.0)
    find(takeoff_requests(), lambda request: request.parts[0].stock_allowance_mm == 500.0)
