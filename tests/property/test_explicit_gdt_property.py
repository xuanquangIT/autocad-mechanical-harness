"""Property 30: GD&T is explicit-only and datum references are validated."""

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.annotation.engine import AnnotationEngine
from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.validation.engine import RuleContext
from cad_harness.validation.rules.annotation_rules import GdtDatumExistsRule


# Feature: cad-ai-production-roadmap, Property 30: GD&T chỉ được sinh khi khai báo tường minh và mọi datum tham chiếu phải tồn tại
@given(missing_datum=st.booleans(), tolerance_text=st.from_regex(r"0\.[0-9]{1,3}", fullmatch=True))
@settings(max_examples=100, deadline=None)
def test_gdt_is_explicit_only_and_requires_defined_datums(
    missing_datum: bool, tolerance_text: str
) -> None:
    """**Validates: Requirements 11.5, 11.6, 11.7**"""
    profile = load_profile("demo-profile")
    geometry = (
        Operation(
            operation_id="op:p:outline",
            feature_id="p",
            type=OperationType.CREATE_CLOSED_POLYLINE,
            layer="OBJECT",
            geometry={"vertices_mm": [[0, 0], [10, 0], [10, 10], [0, 10]]},
        ),
    )
    base = {
        "spec_id": "s",
        "document_id": "d",
        "standard_profile": {"profile_id": "demo-profile", "version": "1.0"},
        "annotations": {"dimensions": "none"},
    }
    none_result = AnnotationEngine(profile, profile.tolerance()).annotate(
        geometry_operations=geometry, spec=DrawingSpec.model_validate(base), datum=None
    )
    assert not [
        op
        for op in none_result.operations
        if str(op.geometry.get("annotation_kind", "")).startswith("gdt_")
    ]
    annotations = {
        "dimensions": "none",
        "datum_symbols": []
        if missing_datum
        else [{"identifier": "A", "feature_id": "p", "position_mm": [1, 1]}],
        "feature_control_frames": [
            {
                "frame_id": "fcf-1",
                "feature_id": "p",
                "characteristic": "position",
                "tolerance_text": tolerance_text,
                "datum_references": ["A"],
                "position_mm": [2, 2],
            }
        ],
    }
    explicit = DrawingSpec.model_validate({**base, "annotations": annotations})
    result = AnnotationEngine(profile, profile.tolerance()).annotate(
        geometry_operations=geometry, spec=explicit, datum=None
    )
    frames = [
        op
        for op in result.operations
        if op.geometry.get("annotation_kind") == "gdt_feature_control_frame"
    ]
    assert len(frames) == 1
    assert frames[0].geometry["certifies_tolerance_chain"] is False
    plan = OperationPlan(
        plan_id="p",
        job_id="j",
        document_id="d",
        expected_revision="r",
        profile_ref=profile.as_ref(),
        operations=tuple(result.operations),
    )
    findings = GdtDatumExistsRule().evaluate(
        RuleContext(profile=profile, tolerance=profile.tolerance(), plan=plan)
    )
    assert bool(findings) is missing_datum
