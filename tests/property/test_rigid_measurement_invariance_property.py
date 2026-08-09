"""Property 31: intrinsic measurements survive every rigid motion."""

from __future__ import annotations

from hypothesis import given, settings
from tests.property.strategies import outlines, rigid_motions

from cad_harness.geometry.curves import CurveParams, normalize_arc
from cad_harness.geometry.measure import arc_length, line_angle_deg
from cad_harness.geometry.primitives import Circle2D, Point2D, Polyline2D
from cad_harness.geometry.tolerance import DEMO_TOLERANCE
from cad_harness.geometry.transforms import rotate_about, translate


# Feature: cad-ai-production-roadmap, Property 31
@given(outline=outlines(include_pathological=False), motion=rigid_motions())
@settings(max_examples=150, deadline=None)
def test_intrinsic_measurements_are_rigid_motion_invariants(outline, motion) -> None:
    """**Validates: Requirements 12.4, 23.9**"""
    pivot = Point2D(0.0, 0.0)
    transformed = translate(
        rotate_about(outline, motion.rotation_deg, pivot), motion.dx_mm, motion.dy_mm
    )
    assert isinstance(transformed, Polyline2D)
    assert DEMO_TOLERANCE.length_close(transformed.perimeter(), outline.perimeter())
    assert DEMO_TOLERANCE.area_close(transformed.area(), outline.area())

    first, second = outline.segments[:2]
    moved_first, moved_second = transformed.segments[:2]
    original_angle = line_angle_deg(*first, *second, DEMO_TOLERANCE)
    moved_angle = line_angle_deg(*moved_first, *moved_second, DEMO_TOLERANCE)
    assert DEMO_TOLERANCE.angle_close_deg(original_angle, moved_angle)

    center = outline.vertices[0]
    circle = Circle2D(center, 14.0)
    moved_circle = translate(
        rotate_about(circle, motion.rotation_deg, pivot), motion.dx_mm, motion.dy_mm
    )
    assert isinstance(moved_circle, Circle2D)
    assert DEMO_TOLERANCE.length_close(moved_circle.diameter_mm, circle.diameter_mm)

    radius = max(1.0, first[0].distance_to(first[1]))
    curve = normalize_arc(center, radius, 17.0, sweep_deg=137.0)
    moved_curve = translate(
        rotate_about(curve, motion.rotation_deg, pivot), motion.dx_mm, motion.dy_mm
    )
    assert isinstance(moved_curve, CurveParams)
    assert DEMO_TOLERANCE.length_close(arc_length(moved_curve), arc_length(curve))
