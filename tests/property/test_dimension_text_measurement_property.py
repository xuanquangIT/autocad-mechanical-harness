"""Property 25: dimension text agrees with measured geometry within tolerance."""

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.validation.engine import RuleContext
from cad_harness.validation.rules.annotation_rules import DimensionTextMatchesGeometryRule


# Feature: cad-ai-production-roadmap, Property 25: Chữ dimension khớp số đo hình học
@given(value=st.floats(min_value=0.1, max_value=10000, allow_nan=False), within=st.booleans())
@settings(max_examples=100, deadline=None)
def test_dimension_rule_detects_exactly_out_of_tolerance_text(value: float, within: bool) -> None:
    """**Validates: Requirements 9.4, 21.3**"""
    profile = load_profile("demo-profile")
    tolerance = profile.tolerance()
    delta = tolerance.absolute_length_mm * (0.5 if within else 2.0)
    actual = value + delta
    operation = Operation(
        operation_id="op:annotation:test",
        feature_id="f",
        type=OperationType.CREATE_LINEAR_DIMENSION,
        layer="DIM",
        geometry={"measurement_mm": value},
        expected={"text_value_mm": actual},
    )
    plan = OperationPlan(
        plan_id="p",
        job_id="j",
        document_id="d",
        expected_revision="r",
        profile_ref=profile.as_ref(),
        operations=(operation,),
    )
    rule = DimensionTextMatchesGeometryRule()
    plan_findings = rule.evaluate(RuleContext(profile=profile, tolerance=tolerance, plan=plan))
    audit_findings = rule.evaluate(
        RuleContext(
            profile=profile,
            tolerance=tolerance,
            drawing_model=type("M", (), {"document_id": "d", "revision": "r"})(),
            extras={"dimensions": ({"measurement_mm": value, "text_value_mm": actual},)},
        )
    )
    assert bool(plan_findings) is (not within)
    assert bool(audit_findings) is (not within)
    for item in (*plan_findings, *audit_findings):
        assert item.expected == value
        assert item.actual == actual
        assert item.tolerance == tolerance.absolute_length_mm
        assert item.rule_id == "DIMENSION_TEXT_MATCHES_GEOMETRY"
