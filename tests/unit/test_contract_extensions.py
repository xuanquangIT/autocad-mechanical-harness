"""The enum and contract surface opened up for the roadmap (Requirements 1.8, 13.6, 13.13).

Two things are worth pinning here. First, a new enum member is only useful once every
consumer knows it: an `OperationType` without an entity-type mapping would silently
post-validate against `AcDbEntity`. Second, the drawing-facing stages must run with
`plan=None`, so a plan-facing rule leaking into those stages has to be a test failure
rather than a runtime crash on the first audit.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

import pytest

from cad_harness.adapters.base import ENTITY_TYPE_BY_OPERATION
from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.errors import (
    ErrorCode,
    HarnessError,
    ReadScopeTooLargeError,
    ToolNotAllowedError,
    UnsupportedInputFormatError,
)
from cad_harness.domain.models.drawing_model import DrawingModel, ReadScope
from cad_harness.domain.models.operation_plan import OperationType
from cad_harness.domain.models.validation import (
    Finding,
    Severity,
    ValidationReport,
    ValidationStage,
)
from cad_harness.domain.ports.autocad_adapter import AdapterStatus
from cad_harness.feature_catalog.base import CompileContext
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.observability.audit import AuditEventType
from cad_harness.validation.engine import (
    DrawingModelLike,
    RuleContext,
    ValidationEngine,
    default_engine,
)

#: Operations that act on an entity that already exists, so they create no new type.
_NON_CREATING = {OperationType.UPDATE_ENTITY, OperationType.DELETE_ENTITY}

#: Stages that read a drawing rather than a plan.
DRAWING_STAGES = (ValidationStage.DRAWING_AUDIT, ValidationStage.DRAWING_STANDARD)


@dataclass(frozen=True, slots=True)
class StubDrawingModel:
    """Minimal read model. Stands in until `DrawingModel` lands with the reader."""

    document_id: str
    revision: str


@dataclass(frozen=True, slots=True)
class DrawingRevisionRule:
    """A drawing-stage rule. Reads the model only, never the plan."""

    rule_id: str = "AUDIT-REVISION-PRESENT"
    stages: tuple[ValidationStage, ...] = DRAWING_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        model = context.require_drawing_model()
        return [
            Finding(
                rule_id=self.rule_id,
                severity=Severity.INFO,
                message="Audited an existing drawing",
                expected="revision present",
                actual=model.revision,
            )
        ]


class TestOperationTypeSurface:
    def test_the_new_members_are_declared(self) -> None:
        assert OperationType.CREATE_ANGULAR_DIMENSION.value == "create_angular_dimension"
        assert OperationType.CREATE_HATCH.value == "create_hatch"

    def test_every_creating_operation_has_an_entity_type(self) -> None:
        # Without a mapping, post-commit validation compares against AcDbEntity and
        # silently passes anything the adapter produced.
        unmapped = {
            member
            for member in OperationType
            if member not in _NON_CREATING and member not in ENTITY_TYPE_BY_OPERATION
        }
        assert unmapped == set()

    def test_update_and_delete_are_deliberately_unmapped(self) -> None:
        assert not _NON_CREATING & set(ENTITY_TYPE_BY_OPERATION)

    def test_the_new_members_map_to_the_expected_autocad_types(self) -> None:
        assert ENTITY_TYPE_BY_OPERATION[OperationType.CREATE_HATCH] == "AcDbHatch"
        assert (
            ENTITY_TYPE_BY_OPERATION[OperationType.CREATE_ANGULAR_DIMENSION]
            == "AcDb2LineAngularDimension"
        )


class TestErrorCodeSurface:
    @pytest.mark.parametrize(
        ("exception_type", "code"),
        [
            (ToolNotAllowedError, ErrorCode.TOOL_NOT_ALLOWED),
            (UnsupportedInputFormatError, ErrorCode.UNSUPPORTED_INPUT_FORMAT),
            (ReadScopeTooLargeError, ErrorCode.READ_SCOPE_TOO_LARGE),
        ],
    )
    def test_each_new_code_has_an_exception_with_a_default_action(
        self, exception_type: type[HarnessError], code: ErrorCode
    ) -> None:
        payload = exception_type("boom").to_payload()
        assert payload["code"] == code.value
        assert payload["required_action"]

    def test_a_too_large_scope_is_not_retryable(self) -> None:
        # Retrying the same scope hits the same limit; the caller must narrow it.
        assert ReadScopeTooLargeError("too wide").to_payload()["retryable"] is False

    def test_a_call_site_action_wins_over_the_default(self) -> None:
        error = ToolNotAllowedError("nope", required_action="Ask the owner for write access")
        assert error.required_action == "Ask the owner for write access"


class TestAuditAndAdapterSurface:
    @pytest.mark.parametrize(
        "name",
        [
            "TOOL_CALL_REJECTED",
            "DRAWING_READ",
            "TAKEOFF_REPORT_CREATED",
            "DRAWING_AUDITED",
            "SPEC_CHANGED",
        ],
    )
    def test_the_new_audit_events_are_declared(self, name: str) -> None:
        assert AuditEventType[name].value == name

    def test_an_adapter_that_never_checked_makes_no_version_claim(self) -> None:
        # None means "unknown", which is not the same as "supported".
        assert AdapterStatus(adapter_type="fake", available=True).version_supported is None


class TestContractDefaults:
    def test_chord_tolerance_defaults_to_the_documented_value(self) -> None:
        assert ToleranceProfile(id="t", version="1.0").arc_chord_tolerance_mm == 0.01

    def test_a_report_never_silently_claims_approval_or_scope(self) -> None:
        report = ValidationReport(
            validation_id="val_1", job_id="job_1", stage=ValidationStage.DRAWING_AUDIT
        )
        assert report.company_approved is False
        assert report.entities_examined == 0

    def test_compile_context_carries_an_optional_parent_outline(
        self, profile: CompanyProfile, tolerance: ToleranceProfile
    ) -> None:
        # A bounding box cannot decide containment for a non-rectangular parent, so
        # the real contour has to be reachable from a child compile.
        context = CompileContext(profile=profile, tolerance=tolerance)
        assert context.parent_outline is None
        assert "parent_outline" in {f.name for f in fields(CompileContext)}

    @pytest.mark.parametrize(
        "name",
        [
            "annotation_rules",
            "layout_rules",
            "title_block_fields",
            "dwt_ref",
            "dws_ref",
            "material_profile_ref",
        ],
    )
    def test_profile_declares_the_new_standards_blocks(self, name: str) -> None:
        assert name in CompanyProfile.model_fields


class TestDrawingStageContext:
    """A drawing audit runs with `plan=None` and a read model instead."""

    def test_a_drawing_rule_runs_through_the_engine_without_a_plan(
        self, profile: CompanyProfile, tolerance: ToleranceProfile
    ) -> None:
        context = RuleContext(
            profile=profile,
            tolerance=tolerance,
            plan=None,
            drawing_model=StubDrawingModel(document_id="doc_1", revision="sha256:r1"),
        )
        engine = ValidationEngine([DrawingRevisionRule()])

        report = engine.run(
            ValidationStage.DRAWING_AUDIT, context, job_id="job_1", entities_examined=42
        )

        assert report.plan_hash is None
        assert report.entities_examined == 42
        assert report.company_approved is profile.company_approved
        assert [f.actual for f in report.findings] == ["sha256:r1"]

    def test_the_stub_model_satisfies_the_read_protocol(self) -> None:
        assert isinstance(
            StubDrawingModel(document_id="doc_1", revision="sha256:r1"), DrawingModelLike
        )

    def test_a_missing_plan_fails_loudly_rather_than_reporting_a_clean_drawing(
        self, profile: CompanyProfile, tolerance: ToleranceProfile
    ) -> None:
        context = RuleContext(profile=profile, tolerance=tolerance)
        with pytest.raises(HarnessError) as excinfo:
            context.require_plan()
        assert excinfo.value.required_action

    def test_a_missing_drawing_model_fails_loudly(
        self, profile: CompanyProfile, tolerance: ToleranceProfile
    ) -> None:
        context = RuleContext(profile=profile, tolerance=tolerance)
        with pytest.raises(HarnessError):
            context.require_drawing_model()

    @pytest.mark.parametrize("stage", DRAWING_STAGES)
    def test_no_shipped_rule_demands_a_plan_at_a_drawing_stage(
        self, profile: CompanyProfile, tolerance: ToleranceProfile, stage: ValidationStage
    ) -> None:
        # Every shipped drawing-stage rule consumes only the complete reader contract
        # and never reaches for an OperationPlan.
        drawing_model: DrawingModelLike = DrawingModel(
            document_id="doc_1",
            revision="sha256:r1",
            display_name="drawing.dxf",
            source_unit_code="mm",
            to_mm_factor=1.0,
            geometry_normalized=True,
            scope=ReadScope(),
            arc_chord_tolerance_mm=0.01,
        )
        context = RuleContext(
            profile=profile,
            tolerance=tolerance,
            drawing_model=drawing_model,
        )
        report = default_engine().run(stage, context, job_id="job_1", entities_examined=7)
        assert report.stage is stage
        assert report.entities_examined == 7
