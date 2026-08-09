"""Property 16: feature-specific rules agree with analytic geometry."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.validation import ValidationStage
from cad_harness.feature_catalog.base import CompileContext, CompiledFeature
from cad_harness.feature_catalog.bracket import LBracketCompiler
from cad_harness.feature_catalog.flange import FlangeCompiler
from cad_harness.feature_catalog.slot import SlotCompiler
from cad_harness.validation.engine import RuleContext, ValidationEngine, ValidationRule
from cad_harness.validation.rules.feature_rules import (
    FlangeHolesOnPcdRule,
    FlangeOuterDiameterClearanceRule,
    LBracketLegPerpendicularityRule,
    SlotArcTangencyRule,
)

lengths = st.floats(min_value=20.0, max_value=300.0, allow_nan=False, allow_infinity=False)


def _plan(compiled: CompiledFeature) -> OperationPlan:
    return OperationPlan(
        plan_id="plan_property_16",
        job_id="job_property_16",
        document_id="doc_property_16",
        expected_revision="rev_property_16",
        profile_ref="demo-profile@1.0",
        operations=tuple(compiled.operations),
        validation_expectations=tuple(compiled.expectations),
    )


def _findings(rule: ValidationRule, compiled: CompiledFeature):
    profile = load_profile("demo-profile")
    context = RuleContext(
        profile=profile,
        tolerance=profile.tolerance(),
        plan=_plan(compiled),
    )
    return (
        ValidationEngine([rule])
        .run(ValidationStage.PLAN, context, job_id="job_property_16")
        .findings
    )


def _assert_evidence(findings) -> None:
    assert all(
        item.expected is not None and item.actual is not None and item.tolerance is not None
        for item in findings
    )


# Feature: cad-ai-production-roadmap, Property 16: Quy tắc hình học của flange, slot và l_bracket đúng trên toàn không gian tham số
@given(
    pcd=lengths,
    hole=st.floats(min_value=2.0, max_value=20.0, allow_nan=False),
    count=st.integers(min_value=2, max_value=16),
    slot_width=st.floats(min_value=2.0, max_value=50.0, allow_nan=False),
    slot_extra=st.floats(min_value=0.1, max_value=200.0, allow_nan=False),
    leg_a=lengths,
    leg_b=lengths,
    clearance_bad=st.booleans(),
    pcd_bad=st.booleans(),
    tangency_bad=st.booleans(),
    bracket_bad=st.booleans(),
)
@settings(max_examples=100, deadline=None)
def test_feature_rules_hold_across_the_parameter_space(
    pcd: float,
    hole: float,
    count: int,
    slot_width: float,
    slot_extra: float,
    leg_a: float,
    leg_b: float,
    clearance_bad: bool,
    pcd_bad: bool,
    tangency_bad: bool,
    bracket_bad: bool,
) -> None:
    """**Validates: Requirements 7.5, 7.6, 7.7, 7.8**"""
    profile = load_profile("demo-profile")
    context = CompileContext(profile=profile, tolerance=profile.tolerance())
    minimum = pcd + hole + 2.0 * float(profile.minimum_hole_ligament_mm or 0.0)
    outer = minimum if clearance_bad else minimum + 1.0
    flange = FlangeCompiler().compile(
        FeatureSpec(
            feature_id="property-16-flange",
            type="flange",
            parameters={
                "outer_diameter_mm": outer,
                "bore_diameter_mm": 1.0,
                "bolt_hole_count": count,
                "bolt_hole_diameter_mm": hole,
                "pcd_mm": pcd,
                "datum": [0.0, 0.0],
            },
        ),
        context,
    )
    clearance_findings = _findings(FlangeOuterDiameterClearanceRule(), flange)
    assert bool(clearance_findings) is clearance_bad
    _assert_evidence(clearance_findings)

    if pcd_bad:
        holes = flange.operations[2]
        centers = [list(point) for point in holes.geometry["centers_mm"]]
        centers[0][0] += 0.1
        flange.operations[2] = holes.model_copy(
            update={"geometry": {**holes.geometry, "centers_mm": centers}}
        )
    pcd_findings = _findings(FlangeHolesOnPcdRule(), flange)
    assert bool(pcd_findings) is pcd_bad
    _assert_evidence(pcd_findings)

    slot = SlotCompiler().compile(
        FeatureSpec(
            feature_id="property-16-slot",
            type="slot",
            parameters={
                "length_mm": slot_width + slot_extra,
                "width_mm": slot_width,
                "center_mm": [3.0, -2.0],
                "angle_deg": 37.0,
            },
        ),
        context,
    )
    if tangency_bad:
        arc = slot.operations[1]
        slot.operations[1] = arc.model_copy(
            update={
                "geometry": {
                    **arc.geometry,
                    "end_angle_deg": float(arc.geometry["end_angle_deg"]) + 1.0,
                }
            }
        )
    tangency_findings = _findings(SlotArcTangencyRule(), slot)
    assert bool(tangency_findings) is tangency_bad
    _assert_evidence(tangency_findings)

    thickness = min(leg_a, leg_b) / 4.0
    bracket = LBracketCompiler().compile(
        FeatureSpec(
            feature_id="property-16-bracket",
            type="l_bracket",
            parameters={
                "leg_a_mm": leg_a,
                "leg_b_mm": leg_b,
                "thickness_mm": thickness,
                "origin_mm": [0.0, 0.0],
            },
        ),
        context,
    )
    if bracket_bad:
        outline = bracket.operations[0]
        vertices = [list(point) for point in outline.geometry["vertices_mm"]]
        vertices[-1][0] = leg_b / 2.0
        bracket.operations[0] = outline.model_copy(
            update={"geometry": {**outline.geometry, "vertices_mm": vertices}}
        )
    bracket_findings = _findings(LBracketLegPerpendicularityRule(), bracket)
    assert bool(bracket_findings) is bracket_bad
    _assert_evidence(bracket_findings)
