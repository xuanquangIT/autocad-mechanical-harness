"""Property 63: selected audit findings compile to an exact-scope write plan."""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.application.services.remediation_service import RemediationService
from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.errors import (
    InvalidFeatureParametersError,
    MissingRequiredInputsError,
    StaleDocumentRevisionError,
)
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    DimensionGeometry,
    DrawingModel,
    EntityRecord,
    LineGeometry,
    PolylineGeometry,
    PolylineVertex,
    ReadScope,
    TextGeometry,
)
from cad_harness.domain.models.operation_plan import OperationType
from cad_harness.domain.models.validation import (
    Finding,
    Severity,
    ValidationReport,
    ValidationStage,
)
from cad_harness.persistence.memory_drawing_audit_store import InMemoryDrawingAuditStore


def _entity(ref: str, geometry: object, *, layer: str = "OBJECT") -> EntityRecord:
    return EntityRecord.model_validate(
        {
            "entity_ref": ref,
            "entity_type": f"AcDb{type(geometry).__name__.removesuffix('Geometry')}",
            "layer": layer,
            "visible": True,
            "space": "model",
            "geometry": geometry,
            "bounding_box_mm": (0.0, 0.0, 20.0, 20.0),
        }
    )


def _model() -> DrawingModel:
    entities = (
        _entity(
            "open",
            PolylineGeometry(
                vertices=(
                    PolylineVertex(point_mm=(0.0, 0.0)),
                    PolylineVertex(point_mm=(8.0, 0.0)),
                    PolylineVertex(point_mm=(8.0, 6.0)),
                ),
                closed=False,
            ),
        ),
        _entity("duplicate", LineGeometry(start_mm=(0.0, 0.0), end_mm=(5.0, 0.0))),
        _entity("zero", LineGeometry(start_mm=(1.0, 1.0), end_mm=(1.0, 1.0))),
        _entity(
            "wrong-layer",
            LineGeometry(start_mm=(0.0, 2.0), end_mm=(5.0, 2.0)),
            layer="TEXT",
        ),
        _entity(
            "dimension-style",
            DimensionGeometry(
                dimension_type="linear",
                dimension_style="BAD",
                measurement_mm=20.0,
                text_override=None,
            ),
            layer="DIM",
        ),
        _entity(
            "text-style",
            TextGeometry(
                insertion_mm=(0.0, 0.0),
                height_mm=2.5,
                text_style="BAD",
                content="NOTE",
            ),
            layer="TEXT",
        ),
        _entity(
            "dimension-text",
            DimensionGeometry(
                dimension_type="linear",
                dimension_style="ISO-25",
                measurement_mm=20.0,
                text_override="25",
            ),
            layer="DIM",
        ),
        _entity("fillet-first", LineGeometry(start_mm=(0.0, 10.0), end_mm=(9.0, 10.0))),
        _entity(
            "fillet",
            ArcGeometry(
                center_mm=(9.0, 11.0),
                radius_mm=1.0,
                start_angle_deg=270.0,
                end_angle_deg=5.0,
            ),
        ),
        _entity("fillet-last", LineGeometry(start_mm=(10.0, 11.0), end_mm=(10.0, 20.0))),
        _entity("hole", LineGeometry(start_mm=(30.0, 30.0), end_mm=(31.0, 30.0))),
    )
    return DrawingModel(
        document_id="doc-remediation",
        revision="sha256:audit-r1",
        display_name="remediation.dwg",
        source_unit_code="mm",
        to_mm_factor=1.0,
        geometry_normalized=True,
        scope=ReadScope(),
        entities=entities,
        arc_chord_tolerance_mm=0.01,
    )


def _finding(
    rule_id: str,
    entity_ref: str,
    *,
    expected: object = None,
    actual: object = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=Severity.ERROR,
        message=f"Injected {rule_id}",
        entity_ref=entity_ref,
        expected=expected,
        actual=actual,
        suggested_fix="Use remediation",
    )


AUTOMATIC_FINDINGS = (
    _finding("OPEN_CONTOUR", "open", expected={"gap_mm": 0.0}, actual={"gap_mm": 10.0}),
    _finding(
        "DUPLICATE_ENTITY",
        "duplicate",
        expected={"unique_geometry": True},
        actual={"duplicate_of": "original"},
    ),
    _finding("ZERO_LENGTH_ENTITY", "zero"),
    _finding("ENTITY_ON_EXPECTED_LAYER", "wrong-layer", expected="OBJECT", actual="TEXT"),
    _finding("DIMSTYLE_IN_PROFILE", "dimension-style", expected=["ISO-25"]),
    _finding("TEXTSTYLE_IN_PROFILE", "text-style", expected=["STANDARD"]),
    _finding("DIMENSION_TEXT_MATCHES_GEOMETRY", "dimension-text", expected=20.0, actual=25.0),
)


def _report(*findings: Finding) -> ValidationReport:
    return ValidationReport(
        validation_id="audit-remediation",
        job_id="audit-job",
        stage=ValidationStage.DRAWING_AUDIT,
        findings=findings,
        entities_examined=len(_model().entities),
        profile_ref=load_profile("demo-profile").as_ref(),
    )


def _service(model: DrawingModel, report: ValidationReport) -> RemediationService:
    store = InMemoryDrawingAuditStore()
    store.save_drawing_audit(
        audit_id="audit-remediation",
        document_id=model.document_id,
        revision=model.revision,
        report=report,
    )
    return RemediationService(load_profile("demo-profile").tolerance(), store)


# Feature: cad-ai-production-roadmap, Property 63
@given(selected_indices=st.sets(st.integers(min_value=0, max_value=6), min_size=1))
@settings(max_examples=50, deadline=None)
def test_plan_contains_only_operations_traceable_to_selected_findings(
    selected_indices: set[int],
) -> None:
    """**Validates: Requirements 22.1, 22.2, 22.6.**"""
    model = _model()
    findings = AUTOMATIC_FINDINGS
    selected = tuple(
        (finding.rule_id, finding.entity_ref or "")
        for index, finding in enumerate(findings)
        if index in selected_indices
    )
    result = _service(model, _report(*findings)).compile_plan(
        job_id="job-remediation",
        model=model,
        audit_id="audit-remediation",
        selected_rule_findings=selected,
    )

    assert result.selected_findings == selected
    assert result.plan.expected_revision == model.revision
    assert result.plan.plan_hash == result.plan.compute_hash()
    assert result.plan.operations
    assert len(result.operation_sources) == len(result.plan.operations)
    assert {source.operation_id for source in result.operation_sources} == {
        operation.operation_id for operation in result.plan.operations
    }
    assert all(
        (source.rule_id, source.entity_ref) in selected for source in result.operation_sources
    )
    assert all(
        operation.type in {OperationType.UPDATE_ENTITY, OperationType.DELETE_ENTITY}
        or operation.type is OperationType.CREATE_LINE
        for operation in result.plan.operations
    )


@pytest.mark.parametrize(
    ("rule_id", "missing_path"),
    [
        ("HOLE_EDGE_DISTANCE_MIN", "remediation.hole.center_or_diameter"),
        ("HOLE_LIGAMENT_MIN", "remediation.hole.center_or_diameter"),
        ("HOLE_OUTSIDE_PART", "remediation.hole.center"),
    ],
)
def test_nonautomatic_finding_requires_an_explicit_engineering_decision(
    rule_id: str, missing_path: str
) -> None:
    model = _model()
    finding = _finding(rule_id, "hole")
    with pytest.raises(MissingRequiredInputsError) as error:
        _service(model, _report(finding)).compile_plan(
            job_id="job-remediation",
            model=model,
            audit_id="audit-remediation",
            selected_rule_findings=((rule_id, "hole"),),
        )
    assert error.value.details == {
        "rule_id": rule_id,
        "entity_ref": "hole",
        "missing_paths": [missing_path],
    }


def test_fillet_requires_only_radius_then_derives_all_coordinates_in_geometry() -> None:
    model = _model()
    finding = _finding("FILLET_NOT_TANGENT", "fillet")
    service = _service(model, _report(finding))
    with pytest.raises(MissingRequiredInputsError) as missing:
        service.compile_plan(
            job_id="job-remediation",
            model=model,
            audit_id="audit-remediation",
            selected_rule_findings=((finding.rule_id, "fillet"),),
        )
    assert missing.value.details["missing_paths"] == ["remediation.fillet.radius_mm"]

    with pytest.raises(InvalidFeatureParametersError) as raw_coordinate:
        service.compile_plan(
            job_id="job-remediation",
            model=model,
            audit_id="audit-remediation",
            selected_rule_findings=((finding.rule_id, "fillet"),),
            technical_inputs={
                "FILLET_NOT_TANGENT:fillet": {
                    "radius_mm": 1.0,
                    "center_mm": [9.0, 11.0],
                }
            },
        )
    assert "center_mm" in str(raw_coordinate.value.details["unexpected_technical_inputs"])

    result = service.compile_plan(
        job_id="job-remediation",
        model=model,
        audit_id="audit-remediation",
        selected_rule_findings=((finding.rule_id, "fillet"),),
        technical_inputs={"FILLET_NOT_TANGENT:fillet": {"radius_mm": 1.0}},
    )
    assert [operation.type for operation in result.plan.operations] == [
        OperationType.UPDATE_ENTITY,
        OperationType.DELETE_ENTITY,
        OperationType.UPDATE_ENTITY,
        OperationType.CREATE_ARC,
    ]
    replacement = result.plan.operations[-1]
    assert replacement.geometry["radius_mm"] == 1.0
    assert all(
        math.isfinite(float(value))
        for key, value in replacement.geometry.items()
        if key != "center_mm"
    )


def test_overlap_requires_explicit_delete_selected_strategy() -> None:
    model = _model()
    finding = _finding(
        "OVERLAPPING_ENTITY",
        "duplicate",
        expected="non-overlapping geometry",
        actual={"other_entity_ref": "open"},
    )
    service = _service(model, _report(finding))

    with pytest.raises(MissingRequiredInputsError) as missing:
        service.compile_plan(
            job_id="job-remediation",
            model=model,
            audit_id="audit-remediation",
            selected_rule_findings=((finding.rule_id, "duplicate"),),
        )
    assert missing.value.details["missing_paths"] == ["remediation.overlap.strategy"]

    with pytest.raises(MissingRequiredInputsError):
        service.compile_plan(
            job_id="job-remediation",
            model=model,
            audit_id="audit-remediation",
            selected_rule_findings=((finding.rule_id, "duplicate"),),
            technical_inputs={"OVERLAPPING_ENTITY:duplicate": {"strategy": "delete_other"}},
        )

    result = service.compile_plan(
        job_id="job-remediation",
        model=model,
        audit_id="audit-remediation",
        selected_rule_findings=((finding.rule_id, "duplicate"),),
        technical_inputs={"OVERLAPPING_ENTITY:duplicate": {"strategy": "delete_selected"}},
    )

    assert len(result.plan.operations) == 1
    operation = result.plan.operations[0]
    assert operation.type is OperationType.DELETE_ENTITY
    assert operation.target_entity_ref == "duplicate"
    assert operation.expected == {
        "remediates_rule_id": "OVERLAPPING_ENTITY",
        "strategy": "delete_selected",
    }


def test_stale_or_unknown_findings_are_rejected_before_a_plan_exists() -> None:
    model = _model()
    finding = AUTOMATIC_FINDINGS[1]
    service = _service(model, _report(finding))
    with pytest.raises(StaleDocumentRevisionError):
        service.compile_plan(
            job_id="job-remediation",
            model=model.model_copy(update={"revision": "sha256:current-r2"}),
            audit_id="audit-remediation",
            selected_rule_findings=((finding.rule_id, "duplicate"),),
        )
    with pytest.raises(InvalidFeatureParametersError):
        service.compile_plan(
            job_id="job-remediation",
            model=model,
            audit_id="audit-remediation",
            selected_rule_findings=((finding.rule_id, "not-in-report"),),
        )
