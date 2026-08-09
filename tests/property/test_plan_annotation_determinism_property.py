"""Property 23: two-phase compilation is deterministic."""

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.application.services.plan_compiler import PlanCompilerService
from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.drawing_spec import DrawingSpec


# Feature: cad-ai-production-roadmap, Property 23: `plan_hash`, `feature_id` và thứ tự annotation là xác định
@given(
    width=st.floats(min_value=40, max_value=500, allow_nan=False),
    height=st.floats(min_value=40, max_value=500, allow_nan=False),
)
@settings(max_examples=100, deadline=None)
def test_two_phase_compilation_is_deterministic(width: float, height: float) -> None:
    """**Validates: Requirements 8.10, 9.10, 12.3**"""
    profile = load_profile("demo-profile")
    spec = DrawingSpec.model_validate(
        {
            "spec_id": "spec-1",
            "document_id": "doc-1",
            "standard_profile": {"profile_id": profile.profile_id, "version": profile.version},
            "drawing": {"datum": {"type": "point", "point_mm": [0, 0]}},
            "features": [
                {
                    "feature_id": "plate",
                    "type": "rectangular_plate",
                    "parameters": {
                        "width_mm": width,
                        "height_mm": height,
                        "thickness_mm": 5,
                        "origin_mm": [0, 0],
                    },
                }
            ],
            "annotations": {"dimensions": "auto_required"},
        }
    )
    compiler = PlanCompilerService(profile, profile.tolerance())
    first = compiler.compile(spec, job_id="job-a", expected_revision="rev").plan
    second = compiler.compile(spec, job_id="job-b", expected_revision="rev").plan
    assert first is not None and second is not None
    assert first.plan_hash == second.plan_hash
    assert [op.feature_id for op in first.operations] == [op.feature_id for op in second.operations]
    assert [op.operation_id for op in first.operations] == [
        op.operation_id for op in second.operations
    ]
    assert [op.type for op in first.operations] == [op.type for op in second.operations]
