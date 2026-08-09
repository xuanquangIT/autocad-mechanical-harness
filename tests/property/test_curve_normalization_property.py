"""Property 37: canonical curved edges and bounded chord approximation."""

from __future__ import annotations

from hypothesis import given, settings
from tests.property.strategies import ArcCase, BulgeCase, CircleCase, EllipseCase, curve_params

from cad_harness.geometry.curves import (
    CurveKind,
    chord_error_bound,
    chord_segment_count,
    linearize_curve,
    normalize_arc,
    normalize_bulge,
    normalize_circle,
    normalize_ellipse,
)
from cad_harness.geometry.intersections import point_to_segment_distance
from cad_harness.geometry.tolerance import DEMO_TOLERANCE


# Feature: cad-ai-production-roadmap, Property 37
@given(case=curve_params())
@settings(max_examples=150, deadline=None)
def test_curved_edges_normalize_and_respect_chord_error(case) -> None:
    """**Validates: Requirements 13.12**"""
    match case:
        case CircleCase():
            normalized = normalize_circle(case.center, case.radius_mm)
            assert normalized.kind is CurveKind.CIRCLE
            assert normalized.center == case.center
            assert DEMO_TOLERANCE.length_close(normalized.radius_mm or 0.0, case.radius_mm)
        case ArcCase():
            normalized = normalize_arc(
                case.center,
                case.radius_mm,
                case.start_angle_deg,
                sweep_deg=case.sweep_deg,
            )
            assert normalized.kind is CurveKind.ARC
            assert DEMO_TOLERANCE.angle_close_deg(normalized.start_angle_deg, case.start_angle_deg)
            assert DEMO_TOLERANCE.angle_close_deg(normalized.sweep_deg, case.sweep_deg)
        case EllipseCase():
            normalized = normalize_ellipse(
                case.center,
                case.semi_major_mm,
                case.semi_minor_mm,
                case.rotation_deg,
                case.start_angle_deg,
                case.sweep_deg,
            )
            assert normalized.kind is CurveKind.ELLIPSE
            assert DEMO_TOLERANCE.length_close(normalized.semi_major_mm or 0.0, case.semi_major_mm)
            assert DEMO_TOLERANCE.length_close(normalized.semi_minor_mm or 0.0, case.semi_minor_mm)
        case BulgeCase():
            normalized = normalize_bulge(case.start, case.end, case.bulge)
            assert normalized.kind is CurveKind.ARC
            assert DEMO_TOLERANCE.is_coincident(normalized.start_point.distance_to(case.start))
            assert DEMO_TOLERANCE.is_coincident(normalized.end_point.distance_to(case.end))

    count = chord_segment_count(normalized, DEMO_TOLERANCE.arc_chord_tolerance_mm)
    points = linearize_curve(normalized, DEMO_TOLERANCE.arc_chord_tolerance_mm)
    assert len(points) == count + 1
    assert chord_error_bound(normalized, count) <= DEMO_TOLERANCE.arc_chord_tolerance_mm * (
        1.0 + 1.0e-12
    )
    # ``points`` holds count + 1 samples, so there are exactly ``count`` chords; index the
    # pairs directly rather than zipping two sequences of different length.
    for index in range(count):
        start, end = points[index], points[index + 1]
        midpoint = normalized.point_at_fraction((index + 0.5) / count)
        error = point_to_segment_distance(midpoint, start, end)
        assert error <= DEMO_TOLERANCE.arc_chord_tolerance_mm * (1.0 + 1.0e-9)
