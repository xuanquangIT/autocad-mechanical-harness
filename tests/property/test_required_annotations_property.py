"""Property 24: required annotations derive from phase-one geometry."""

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.annotation.engine import AnnotationEngine
from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.operation_plan import Operation, OperationType
from cad_harness.geometry.primitives import Point2D


# Feature: cad-ai-production-roadmap, Property 24: Tập annotation bắt buộc được suy ra từ hình học, không từ văn bản trong spec
@given(
    width=st.floats(min_value=50, max_value=500, allow_nan=False),
    height=st.floats(min_value=50, max_value=500, allow_nan=False),
    diameter=st.floats(min_value=1, max_value=20, allow_nan=False),
    count=st.integers(min_value=1, max_value=6),
)
@settings(max_examples=100, deadline=None)
def test_required_annotations_come_only_from_geometry(
    width: float, height: float, diameter: float, count: int
) -> None:
    """**Validates: Requirements 9.1, 9.2, 9.3, 9.7, 9.9**"""
    profile = load_profile("demo-profile")
    centers = [
        [10.0 + (index + 1) * (width - 20.0) / (count + 1), height / 2.0] for index in range(count)
    ]
    geometry = (
        Operation(
            operation_id="op:plate:outline",
            feature_id="plate",
            type=OperationType.CREATE_CLOSED_POLYLINE,
            layer="OBJECT",
            geometry={"vertices_mm": [[0, 0], [width, 0], [width, height], [0, height]]},
        ),
        Operation(
            operation_id="op:holes:holes",
            feature_id="holes",
            type=OperationType.CREATE_CIRCLES,
            layer="OBJECT",
            geometry={"centers_mm": centers, "diameter_mm": diameter},
        ),
    )
    spec = DrawingSpec.model_validate(
        {
            "spec_id": "s",
            "document_id": "d",
            "standard_profile": {"profile_id": "demo-profile", "version": "1.0"},
            "annotations": {
                "dimensions": "auto_required",
                "general_tolerance": "TEXT-MUST-NOT-BECOME-A-DIMENSION",
            },
        }
    )
    result = AnnotationEngine(profile, profile.tolerance()).annotate(
        geometry_operations=geometry, spec=spec, datum=Point2D(0, 0)
    )
    kinds = [op.geometry.get("annotation_kind") for op in result.operations]
    assert {"linear_dimension", "hole_location_x", "hole_location_y"}.issubset(set(kinds))
    assert sum(op.type is OperationType.CREATE_CENTERMARK for op in result.operations) == count
    assert (
        sum(op.type is OperationType.CREATE_CENTERLINE for op in result.operations)
        == count * (count - 1) // 2
    )
    assert kinds.count("hole_table_row") == 1
    if count >= 4:
        assert kinds.count("hole_callout") == 1 and kinds.count("hole_diameter") == 0
    else:
        assert kinds.count("hole_diameter") == 1 and kinds.count("hole_callout") == 0
    measured = [op.geometry.get("measurement_mm") for op in result.operations]
    assert width in measured and height in measured
    assert all("TEXT-MUST" not in str(op.geometry) for op in result.operations)
