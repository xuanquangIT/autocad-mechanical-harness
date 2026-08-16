"""Reference-circle feature contract and deterministic geometry."""

from __future__ import annotations

import math

import pytest

from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.errors import InvalidFeatureParametersError, MissingRequiredInputsError
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.domain.models.operation_plan import OperationPlan, OperationType
from cad_harness.domain.models.validation import Severity, ValidationStage
from cad_harness.feature_catalog.base import CompileContext
from cad_harness.feature_catalog.reference_circle import ReferenceCircleCompiler
from cad_harness.geometry.primitives import Point2D
from cad_harness.validation.engine import RuleContext, default_engine


def _context(datum: Point2D | None = None) -> CompileContext:
    profile = load_profile("demo-profile")
    return CompileContext(profile=profile, tolerance=profile.tolerance(), datum=datum)


def _feature(**parameters: object) -> FeatureSpec:
    return FeatureSpec(
        feature_id="reference-circle-1",
        type="reference_circle",
        parameters={
            "center_mm": [0.0, 0.0],
            "radius_mm": 20.0,
            "layer_name": "0",
            **parameters,
        },
    )


def test_compiles_r20_at_origin_on_declared_layer_zero() -> None:
    compiled = ReferenceCircleCompiler().compile(_feature(), _context())

    assert len(compiled.operations) == 1
    operation = compiled.operations[0]
    assert operation.operation_id == "op:reference-circle-1:circle"
    assert operation.type is OperationType.CREATE_CIRCLE
    assert operation.layer == "0"
    assert operation.geometry == {"center_mm": [0.0, 0.0], "diameter_mm": 40.0}
    assert operation.expected == {
        "layer": "0",
        "center_mm": [0.0, 0.0],
        "radius_mm": 20.0,
        "diameter_mm": 40.0,
        "area_mm2": math.pi * 20.0**2,
    }
    assert compiled.expectations[0].expected == {
        **operation.expected,
        "circumference_mm": math.tau * 20.0,
    }


def test_uses_resolved_drawing_datum_when_center_is_omitted() -> None:
    parameters = _feature().parameters.copy()
    parameters.pop("center_mm")
    feature = FeatureSpec(feature_id="datum-circle", type="reference_circle", parameters=parameters)

    operation = (
        ReferenceCircleCompiler().compile(feature, _context(Point2D(12.0, -3.0))).operations[0]
    )

    assert operation.geometry["center_mm"] == [12.0, -3.0]


def test_reports_all_missing_required_inputs_in_one_round() -> None:
    feature = FeatureSpec(feature_id="missing", type="reference_circle", parameters={})
    compiler = ReferenceCircleCompiler()

    report = compiler.validate_inputs(feature, _context())

    assert {item.path.rsplit(".", 1)[-1] for item in report.missing} == {
        "radius_mm",
        "center_mm",
        "layer_name",
    }
    with pytest.raises(MissingRequiredInputsError):
        compiler.compile(feature, _context())


@pytest.mark.parametrize("radius", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_rejects_non_positive_or_non_finite_radius(radius: float) -> None:
    with pytest.raises(InvalidFeatureParametersError):
        ReferenceCircleCompiler().compile(_feature(radius_mm=radius), _context())


def test_rejects_layer_outside_selected_profile() -> None:
    with pytest.raises(InvalidFeatureParametersError) as caught:
        ReferenceCircleCompiler().compile(_feature(layer_name="UNCONTROLLED"), _context())

    assert caught.value.details["layer_name"] == "UNCONTROLLED"
    assert "0" in caught.value.details["declared_layers"]


@pytest.mark.parametrize("unexpected", ["script", "operation_plan", "command"])
def test_rejects_every_unknown_or_executable_parameter(unexpected: str) -> None:
    with pytest.raises(InvalidFeatureParametersError) as caught:
        ReferenceCircleCompiler().compile(_feature(**{unexpected: "untrusted"}), _context())

    assert caught.value.details["unexpected_parameters"] == [unexpected]


def test_rejects_radius_that_overflows_derived_geometry() -> None:
    with pytest.raises(InvalidFeatureParametersError, match="finite drafting bounds"):
        ReferenceCircleCompiler().compile(_feature(radius_mm=1.0e308), _context())


def test_compilation_and_plan_validation_are_deterministic() -> None:
    compiler = ReferenceCircleCompiler()
    first = compiler.compile(_feature(), _context())
    second = compiler.compile(_feature(), _context())
    assert [item.model_dump(mode="json") for item in first.operations] == [
        item.model_dump(mode="json") for item in second.operations
    ]

    profile = load_profile("demo-profile")
    plan = OperationPlan(
        plan_id="plan-reference-circle",
        job_id="job-reference-circle",
        document_id="doc-reference-circle",
        expected_revision="sha256:reference-circle",
        profile_ref=profile.as_ref(),
        operations=tuple(first.operations),
        validation_expectations=tuple(first.expectations),
    ).with_hash()
    report = default_engine().run(
        ValidationStage.PRE_COMMIT,
        RuleContext(plan=plan, profile=profile, tolerance=profile.tolerance()),
        job_id=plan.job_id,
    )

    assert "REFERENCE_CIRCLE_GEOMETRY" not in {
        finding.rule_id for finding in report.findings if finding.severity is Severity.ERROR
    }
    assert "STD-LAYER-DECLARED" not in {finding.rule_id for finding in report.findings}


def test_circle_integrity_rule_detects_plan_geometry_drift() -> None:
    compiled = ReferenceCircleCompiler().compile(_feature(), _context())
    operation = compiled.operations[0].model_copy(
        update={"geometry": {"center_mm": [0.0, 0.0], "diameter_mm": 41.0}}
    )
    profile = load_profile("demo-profile")
    plan = OperationPlan(
        plan_id="plan-drift",
        job_id="job-drift",
        document_id="doc-drift",
        expected_revision="sha256:drift",
        profile_ref=profile.as_ref(),
        operations=(operation,),
        validation_expectations=tuple(compiled.expectations),
    ).with_hash()

    report = default_engine().run(
        ValidationStage.PRE_COMMIT,
        RuleContext(plan=plan, profile=profile, tolerance=profile.tolerance()),
        job_id=plan.job_id,
    )

    assert any(
        finding.rule_id == "REFERENCE_CIRCLE_GEOMETRY" and finding.severity is Severity.ERROR
        for finding in report.findings
    )


def test_circle_integrity_rule_detects_plan_layer_drift() -> None:
    compiled = ReferenceCircleCompiler().compile(_feature(), _context())
    operation = compiled.operations[0].model_copy(update={"layer": "OBJECT"})
    profile = load_profile("demo-profile")
    plan = OperationPlan(
        plan_id="plan-reference-circle-layer-drift",
        job_id="job-reference-circle-layer-drift",
        document_id="doc-reference-circle",
        expected_revision="sha256:reference-circle",
        profile_ref=profile.as_ref(),
        operations=(operation,),
        validation_expectations=tuple(compiled.expectations),
    ).with_hash()

    report = default_engine().run(
        ValidationStage.PRE_COMMIT,
        RuleContext(plan=plan, profile=profile, tolerance=profile.tolerance()),
        job_id=plan.job_id,
    )

    finding = next(item for item in report.findings if item.rule_id == "REFERENCE_CIRCLE_GEOMETRY")
    assert finding.severity is Severity.ERROR
    assert finding.expected["layer"] == "0"
    assert finding.actual["layer"] == "OBJECT"
