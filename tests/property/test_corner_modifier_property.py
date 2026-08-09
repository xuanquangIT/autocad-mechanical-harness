"""Property 18: corner modifiers replace only selected vertices."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.drawing_spec import ModifierSpec
from cad_harness.feature_catalog.base import CompileContext
from cad_harness.feature_catalog.modifiers import CornerChamferModifier, CornerFilletModifier
from cad_harness.geometry.primitives import Point2D, Polyline2D


@st.composite
def cases(draw: st.DrawFn) -> tuple[Polyline2D, int, float, float]:
    width = draw(st.floats(min_value=20.0, max_value=300.0, allow_nan=False))
    height = draw(st.floats(min_value=20.0, max_value=300.0, allow_nan=False))
    index = draw(st.integers(min_value=0, max_value=3))
    maximum = min(width, height) / 2.0
    first = draw(st.floats(min_value=0.1, max_value=maximum, allow_nan=False))
    second = draw(st.floats(min_value=0.1, max_value=maximum, allow_nan=False))
    outline = Polyline2D(
        (Point2D(0.0, 0.0), Point2D(width, 0.0), Point2D(width, height), Point2D(0.0, height)),
        closed=True,
    )
    return outline, index, first, second


# Feature: cad-ai-production-roadmap, Property 18: Modifier góc chỉ thay đúng góc được chỉ định
@given(case=cases())
@settings(max_examples=100, deadline=None)
def test_corner_modifiers_change_only_selected_corners(
    case: tuple[Polyline2D, int, float, float],
) -> None:
    """**Validates: Requirements 8.1, 8.2**"""
    outline, index, first, second = case
    profile = load_profile("demo-profile")
    context = CompileContext(
        profile=profile, tolerance=profile.tolerance(), parent_feature_id="plate"
    )
    fillet = CornerFilletModifier().apply(
        ModifierSpec(
            type="corner_fillet", parameters={"radius_mm": first, "vertex_indices": [index]}
        ),
        outline,
        context,
        modifier_index=0,
    )
    replacement = fillet.replacements[0]
    assert replacement.vertex_index == index
    assert profile.tolerance().length_close(replacement.curve.radius_mm or 0.0, first)
    for untouched_index, vertex in enumerate(outline.vertices):
        if untouched_index != index:
            assert vertex in fillet.outline.vertices

    by_distance = CornerChamferModifier().apply(
        ModifierSpec(
            type="corner_chamfer",
            parameters={"distance_1_mm": first, "distance_2_mm": second, "vertex_indices": [index]},
        ),
        outline,
        context,
        modifier_index=1,
    )
    angle = float(by_distance.expectations[0].expected["angle_deg"])
    by_angle = CornerChamferModifier().apply(
        ModifierSpec(
            type="corner_chamfer",
            parameters={"distance_1_mm": first, "angle_deg": angle, "vertex_indices": [index]},
        ),
        outline,
        context,
        modifier_index=1,
    )
    for actual, expected in zip(
        by_angle.replacements[0].replacement_points,
        by_distance.replacements[0].replacement_points,
        strict=True,
    ):
        assert profile.tolerance().is_coincident(actual.distance_to(expected))
