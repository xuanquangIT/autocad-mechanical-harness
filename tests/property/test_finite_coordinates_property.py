"""Property 32: non-finite geometry is blocking and carries complete evidence."""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.validation import Severity, ValidationStage
from cad_harness.validation.engine import RuleContext, ValidationEngine
from cad_harness.validation.rules.geometry_rules import FiniteCoordinatesRule


@dataclass(frozen=True, slots=True)
class DrawingWithBadCoordinate:
    document_id: str
    revision: str
    entities: tuple[dict[str, object], ...]


def _nested_geometry(location: str, value: float) -> dict[str, object]:
    if location == "coordinate":
        return {"vertices_mm": [[0.0, value], [1.0, 2.0]]}
    if location == "dimension":
        return {"arc": {"center_mm": [3.0, 4.0], "radius_mm": value}}
    return {"blocks": [{"children": [{"ellipse": {"semi_major_mm": value}}]}]}


# Feature: cad-ai-production-roadmap, Property 32
@given(
    value=st.sampled_from((float("nan"), float("inf"), float("-inf"))),
    location=st.sampled_from(("coordinate", "dimension", "nested")),
    drawing_stage=st.booleans(),
)
@settings(max_examples=100, deadline=None)
def test_non_finite_coordinates_are_blocked_with_complete_evidence(
    value: float,
    location: str,
    drawing_stage: bool,
) -> None:
    """**Validates: Requirements 12.5, 12.6**"""
    # Built here rather than injected: a function-scoped fixture is created once for the
    # whole @given test, so Hypothesis would reuse one instance across every example.
    profile = load_profile("demo-profile")
    tolerance = profile.tolerance()
    geometry = _nested_geometry(location, value)
    if drawing_stage:
        context = RuleContext(
            profile=profile,
            tolerance=tolerance,
            drawing_model=DrawingWithBadCoordinate("doc_property_32", "rev_1", (geometry,)),
        )
        stage = ValidationStage.DRAWING_AUDIT
    else:
        operation = Operation(
            operation_id="op_property_32",
            feature_id="feature_property_32",
            type=OperationType.CREATE_POLYLINE,
            layer="OBJECT",
            geometry=geometry,
        )
        plan = OperationPlan(
            plan_id="plan_property_32",
            job_id="job_property_32",
            document_id="doc_property_32",
            expected_revision="rev_1",
            profile_ref="demo-profile@1.0",
            operations=(operation,),
        )
        context = RuleContext(profile=profile, tolerance=tolerance, plan=plan)
        stage = ValidationStage.PLAN

    report = ValidationEngine([FiniteCoordinatesRule()]).run(
        stage, context, job_id="job_property_32"
    )
    assert report.has_blocking
    assert report.findings
    assert all(finding.rule_id == "FINITE_COORDINATES" for finding in report.findings)
    assert all(finding.severity is Severity.BLOCKING for finding in report.findings)
    assert all(
        finding.expected is not None
        and finding.actual is not None
        and finding.tolerance is not None
        for finding in report.findings
    )
