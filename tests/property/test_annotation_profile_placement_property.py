"""Property 26: annotation profile mapping, placement and missing-key errors."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.annotation.engine import AnnotationEngine
from cad_harness.annotation.placement import TextBox, overlap_ratio
from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.errors import StandardProfileNotFoundError
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.operation_plan import Operation, OperationType
from cad_harness.geometry.primitives import Point2D


# Feature: cad-ai-production-roadmap, Property 26: Annotation dùng đúng layer, style của profile, không chồng lấn quá mức, và thiếu khai báo thì báo đúng khoá
@given(
    missing=st.sampled_from(("dimension_style", "text_style", "layer_map.dimension")),
    count=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=100, deadline=None)
def test_annotation_profile_and_placement_are_explicit(missing: str, count: int) -> None:
    """**Validates: Requirements 9.5, 9.6, 9.8, 10.5**"""
    profile = load_profile("demo-profile")
    geometry = (
        Operation(
            operation_id="op:o",
            feature_id="p",
            type=OperationType.CREATE_CLOSED_POLYLINE,
            layer="OBJECT",
            geometry={"vertices_mm": [[0, 0], [200, 0], [200, 100], [0, 100]]},
        ),
        Operation(
            operation_id="op:h",
            feature_id="h",
            type=OperationType.CREATE_CIRCLES,
            layer="OBJECT",
            geometry={"centers_mm": [[20 + i * 30, 50] for i in range(count)], "diameter_mm": 8},
        ),
    )
    spec = DrawingSpec.model_validate(
        {
            "spec_id": "s",
            "document_id": "d",
            "standard_profile": {"profile_id": "demo-profile", "version": "1.0"},
            "annotations": {"dimensions": "auto_required"},
        }
    )
    result = AnnotationEngine(profile, profile.tolerance()).annotate(
        geometry_operations=geometry, spec=spec, datum=Point2D(0, 0)
    )
    assert {op.layer for op in result.operations} <= profile.layer_names()
    for operation in result.operations:
        if "dimstyle" in operation.geometry:
            assert operation.geometry["dimstyle"] == profile.dimension_style
        if "textstyle" in operation.geometry:
            assert operation.geometry["textstyle"] == profile.text_style
    boxes = [
        TextBox(*op.geometry["text_bbox_mm"])
        for op in result.operations
        if "text_bbox_mm" in op.geometry
    ]
    has_warning = any(item.rule_id == "ANNOTATION_OVERLAP" for item in result.findings)
    excessive = any(
        overlap_ratio(first, second) > 0.10
        for index, first in enumerate(boxes)
        for second in boxes[index + 1 :]
    )
    assert not excessive or has_warning
    update = (
        {"dimension_style": None}
        if missing == "dimension_style"
        else {"text_style": None}
        if missing == "text_style"
        else {
            "layer_map": {
                key: value for key, value in profile.layer_map.items() if key != "dimension"
            }
        }
    )
    broken = profile.model_copy(update=update)
    with pytest.raises(StandardProfileNotFoundError) as caught:
        AnnotationEngine(broken, broken.tolerance()).annotate(
            geometry_operations=geometry, spec=spec, datum=Point2D(0, 0)
        )
    assert caught.value.details["missing_config_key"] == missing
