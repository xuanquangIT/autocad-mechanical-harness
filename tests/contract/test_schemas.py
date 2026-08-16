"""Contract tests: schema strictness and envelope shape."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError
from scripts.generate_schemas import render
from scripts.ipc_envelope_schema import IPC_ENVELOPE_SCHEMA

from cad_harness.domain.errors import ErrorCode, StaleDocumentRevisionError
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.drawing_spec import DrawingSpec, MissingInput
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus
from cad_harness.domain.models.metrics import PilotReport
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.validation import (
    Finding,
    Severity,
    ValidationReport,
    ValidationStage,
)


def _minimal_spec_payload() -> dict[str, Any]:
    return {
        "spec_id": "spec_1",
        "document_id": "doc_1",
        "standard_profile": {"profile_id": "demo-profile", "version": "1.0"},
    }


class TestStrictness:
    def test_unknown_field_is_rejected(self) -> None:
        """An unknown field means a version mismatch, not something to ignore."""
        payload = {**_minimal_spec_payload(), "unexpected_field": 1}
        with pytest.raises(ValidationError):
            DrawingSpec.model_validate(payload)

    def test_models_are_frozen(self) -> None:
        spec = DrawingSpec.model_validate(_minimal_spec_payload())
        with pytest.raises(ValidationError):
            spec.spec_id = "spec_2"

    def test_schema_version_defaults_to_current(self) -> None:
        assert DrawingSpec.model_validate(_minimal_spec_payload()).schema_version == SCHEMA_VERSION

    def test_legacy_spec_can_be_read_for_history_but_not_assumed_current(self) -> None:
        legacy = DrawingSpec.model_validate({**_minimal_spec_payload(), "schema_version": "1.12"})
        assert legacy.schema_version == "1.12"

    @pytest.mark.parametrize(
        "model,filename",
        [(DrawingSpec, "drawing-spec.schema.json"), (OperationPlan, "operation-plan.schema.json")],
    )
    def test_published_input_schema_pins_current_version(self, model: type, filename: str) -> None:
        rendered = json.loads(render(model, filename))
        assert rendered["properties"]["schema_version"]["const"] == SCHEMA_VERSION

    def test_published_ipc_envelope_pins_current_version(self) -> None:
        definitions = IPC_ENVELOPE_SCHEMA["$defs"]
        assert definitions["request"]["properties"]["schema_version"]["const"] == SCHEMA_VERSION
        assert definitions["response"]["properties"]["schema_version"]["const"] == SCHEMA_VERSION

    def test_invalid_unit_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DrawingSpec.model_validate({**_minimal_spec_payload(), "units": "furlong"})


class TestJsonSchemaGeneration:
    @pytest.mark.parametrize(
        "model", [DrawingSpec, OperationPlan, PilotReport, ValidationReport, ToolResponse]
    )
    def test_schema_can_be_generated(self, model: type) -> None:
        schema = model.model_json_schema()  # type: ignore[attr-defined]
        assert schema["type"] == "object"
        assert "properties" in schema

    def test_plan_schema_enumerates_operation_types(self) -> None:
        schema = OperationPlan.model_json_schema()
        definitions = schema.get("$defs", {})
        assert "OperationType" in definitions
        assert "create_closed_polyline" in definitions["OperationType"]["enum"]


class TestPlanHashing:
    def _plan(self, diameter: float = 14.0) -> OperationPlan:
        return OperationPlan(
            plan_id="plan_1",
            job_id="job_1",
            document_id="doc_1",
            expected_revision="sha256:rev",
            profile_ref="demo-profile@1.0",
            operations=(
                Operation(
                    operation_id="op-1",
                    feature_id="f-1",
                    type=OperationType.CREATE_CIRCLES,
                    layer="OBJECT",
                    geometry={"centers_mm": [[0.0, 0.0]], "diameter_mm": diameter},
                    expected={"count": 1},
                ),
            ),
        )

    def test_hash_is_stable_and_excludes_itself(self) -> None:
        plan = self._plan()
        hashed = plan.with_hash()
        assert hashed.plan_hash == plan.compute_hash()
        assert hashed.compute_hash() == plan.compute_hash()

    def test_hash_detects_geometry_change(self) -> None:
        assert self._plan(14.0).compute_hash() != self._plan(14.5).compute_hash()

    def test_wire_nulls_match_the_csharp_plan_hash_contract(self) -> None:
        plan = OperationPlan(
            plan_id="plan_1",
            job_id="job_1",
            document_id="doc_1",
            expected_revision="sha256:revision",
            profile_ref="demo@1.0",
            operations=(
                Operation(
                    operation_id="op_1",
                    feature_id="feat_" + chr(0x03B1),
                    type=OperationType.CREATE_LINE,
                    layer="CUT",
                    geometry={"end_mm": [1.2345678916, 2.0], "start_mm": [0.0, -0.0]},
                    expected={"length_mm": 2.352},
                ),
            ),
        )

        assert plan.compute_hash() == (
            "sha256:40289068e3b4f14a42f0e03392c39e4b807e4b6d2850a7ca1012087f01a42275"
        )

    def test_verify_hash(self) -> None:
        plan = self._plan().with_hash()
        assert plan.verify_hash(str(plan.plan_hash))
        assert not plan.verify_hash("sha256:wrong")


class TestEnvelope:
    def test_ok_envelope(self) -> None:
        response = ToolResponse.ok({"value": 1}, job_id="job_1")
        assert response.status is ToolStatus.OK
        assert response.error is None

    def test_needs_input_envelope_lists_field_paths(self) -> None:
        response = ToolResponse.needs_input(
            (MissingInput(path="features[0].parameters.width_mm", reason="required"),),
            job_id="job_1",
        )
        assert response.status is ToolStatus.NEEDS_INPUT
        assert response.missing_inputs[0].path.endswith("width_mm")
        assert response.error is not None

    def test_error_envelope_carries_actionable_fields(self) -> None:
        error = StaleDocumentRevisionError(
            "Document changed",
            required_action="Re-inspect and re-approve",
            details={"expected_revision": "sha256:a"},
        )
        response = ToolResponse.from_error(error, status=ToolStatus.CONFLICT)
        assert response.error is not None
        assert response.error.code == ErrorCode.STALE_DOCUMENT_REVISION.value
        assert response.error.required_action == "Re-inspect and re-approve"
        assert response.error.retryable is False


class TestValidationReportGate:
    def _report(self, *severities: Severity) -> ValidationReport:
        return ValidationReport(
            validation_id="validation_1",
            job_id="job_1",
            stage=ValidationStage.PRE_COMMIT,
            findings=tuple(
                Finding(rule_id=f"R-{i}", severity=s, message="m") for i, s in enumerate(severities)
            ),
        )

    def test_blocking_always_stops_commit(self) -> None:
        assert not self._report(Severity.BLOCKING).gate_allows_commit()
        assert not self._report(Severity.BLOCKING).gate_allows_commit(block_on_error=False)

    def test_error_stops_commit_under_default_policy(self) -> None:
        report = self._report(Severity.ERROR)
        assert not report.gate_allows_commit()
        assert report.gate_allows_commit(block_on_error=False)

    def test_warnings_and_info_pass(self) -> None:
        assert self._report(Severity.WARNING, Severity.INFO).gate_allows_commit()
