"""Property 21: infeasible geometry and undeclared contour crossings are rejected."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.errors import ErrorCode, InvalidFeatureParametersError, InvalidGeometryError
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.feature_catalog.base import CompileContext
from cad_harness.feature_catalog.linear_hole_pattern import LinearHolePatternCompiler
from cad_harness.geometry.fillet_chamfer import fillet_vertex
from cad_harness.geometry.primitives import BoundingBox, Point2D, Polyline2D
from cad_harness.validation.engine import RuleContext
from cad_harness.validation.rules.feature_rules import NoUndeclaredContourIntersectionRule


def _operation(
    feature_id: str, vertices: list[list[float]], parent: str | None = None
) -> Operation:
    return Operation(
        operation_id=f"op:{feature_id}:outline",
        feature_id=feature_id,
        type=OperationType.CREATE_CLOSED_POLYLINE,
        layer="OBJECT",
        geometry={"vertices_mm": vertices},
        expected={"parent_feature_id": parent} if parent else {},
    )


# Feature: cad-ai-production-roadmap, Property 21: Tham số hình học không khả thi bị từ chối kèm giá trị tối đa cho phép, và hình học con phải nằm trong cha
@given(
    edge=st.floats(min_value=2.0, max_value=500.0, allow_nan=False),
    infeasible=st.booleans(),
    child_inside=st.booleans(),
    relationship_declared=st.booleans(),
)
@settings(max_examples=100, deadline=None)
def test_complex_geometry_constraints_are_exact_and_actionable(
    edge: float, infeasible: bool, child_inside: bool, relationship_declared: bool
) -> None:
    """**Validates: Requirements 8.7, 8.8, 8.9**"""
    profile = load_profile("demo-profile")
    tolerance = profile.tolerance()
    maximum = edge / 2.0
    radius = maximum + 2.0 * tolerance.absolute_length_mm if infeasible else maximum
    points = (Point2D(0.0, 0.0), Point2D(edge, 0.0), Point2D(edge, edge))
    if infeasible:
        with pytest.raises(InvalidFeatureParametersError) as caught:
            fillet_vertex(*points, radius, tolerance)
        assert caught.value.code is ErrorCode.INVALID_FEATURE_PARAMETERS
        assert tolerance.length_close(float(caught.value.details["maximum_allowed_mm"]), maximum)
    else:
        result = fillet_vertex(*points, radius, tolerance)
        assert tolerance.length_close(result.maximum_radius_mm, maximum)

    parent = Polyline2D(
        (Point2D(0.0, 0.0), Point2D(100.0, 0.0), Point2D(100.0, 100.0), Point2D(0.0, 100.0)),
        closed=True,
    )
    context = CompileContext(
        profile=profile,
        tolerance=tolerance,
        parent_feature_id="parent",
        parent_box=BoundingBox(0.0, 0.0, 100.0, 100.0),
        parent_outline=parent,
    )
    start = [50.0, 50.0] if child_inside else [110.0, 50.0]
    feature = FeatureSpec(
        feature_id="child",
        type="linear_hole_pattern",
        parameters={
            "start_point": start,
            "direction": [1.0, 0.0],
            "pitch_mm": 10.0,
            "count": 1,
            "hole_diameter_mm": 2.0,
        },
    )
    if child_inside:
        assert LinearHolePatternCompiler().compile(feature, context).operations
    else:
        with pytest.raises(InvalidGeometryError) as caught_geometry:
            LinearHolePatternCompiler().compile(feature, context)
        assert caught_geometry.value.code is ErrorCode.INVALID_GEOMETRY
        assert float(caught_geometry.value.details["overflow_mm"]) > 0.0

    first = _operation("first", [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
    parent_id = "first" if relationship_declared else None
    second = _operation("second", [[5.0, -5.0], [15.0, -5.0], [15.0, 5.0], [5.0, 5.0]], parent_id)
    plan = OperationPlan(
        plan_id="plan-property-21",
        job_id="job-property-21",
        document_id="doc",
        expected_revision="rev",
        profile_ref=profile.as_ref(),
        operations=(first, second),
    )
    findings = NoUndeclaredContourIntersectionRule().evaluate(
        RuleContext(profile=profile, tolerance=tolerance, plan=plan)
    )
    assert bool(findings) is not relationship_declared
