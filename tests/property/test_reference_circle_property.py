"""Reference-circle deterministic geometry properties."""

from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.feature_catalog.base import CompileContext
from cad_harness.feature_catalog.reference_circle import ReferenceCircleCompiler

finite_coordinate = st.floats(
    min_value=-1.0e6,
    max_value=1.0e6,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)
positive_radius = st.floats(
    min_value=1.0e-6,
    max_value=1.0e6,
    allow_nan=False,
    allow_infinity=False,
    exclude_min=True,
    width=64,
)


@given(x=finite_coordinate, y=finite_coordinate, radius=positive_radius)
@settings(max_examples=100, deadline=None)
def test_reference_circle_preserves_center_and_analytic_measurements(
    x: float, y: float, radius: float
) -> None:
    profile = load_profile("demo-profile")
    context = CompileContext(profile=profile, tolerance=profile.tolerance())
    feature = FeatureSpec(
        feature_id="property-reference-circle",
        type="reference_circle",
        parameters={"center_mm": [x, y], "radius_mm": radius, "layer_name": "0"},
    )

    first = ReferenceCircleCompiler().compile(feature, context).operations[0]
    second = ReferenceCircleCompiler().compile(feature, context).operations[0]

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.geometry["center_mm"] == [x, y]
    assert first.geometry["diameter_mm"] == radius * 2.0
    assert first.expected["area_mm2"] == math.pi * radius * radius

    expectation = ReferenceCircleCompiler().compile(feature, context).expectations[0]
    assert expectation.expected["circumference_mm"] == math.tau * radius
