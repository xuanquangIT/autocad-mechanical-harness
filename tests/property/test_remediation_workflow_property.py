"""Property 64: remediation preserves stale and post-commit audit gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from cad_harness.adapters.fake import FakeAutoCADAdapter, FakeEntity
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.application.services.remediation_service import RemediationService
from cad_harness.comprehension.auditor import audit_drawing
from cad_harness.config import Settings
from cad_harness.domain.errors import (
    InvalidFeatureParametersError,
    PostCommitValidationFailedError,
    StaleDocumentRevisionError,
)
from cad_harness.domain.models.drawing_model import (
    DrawingModel,
    EntityRecord,
    LineGeometry,
    ReadScope,
)
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.validation import ValidationStage
from cad_harness.observability.audit import InMemoryAuditSink
from cad_harness.persistence.memory_drawing_audit_store import InMemoryDrawingAuditStore


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "remediation-test-secret")
    return Settings.model_validate(
        {
            "storage": {
                "sqlite_path": str(tmp_path / "harness.db"),
                "preview_directory": str(tmp_path / "previews"),
                "checkpoint_directory": str(tmp_path / "checkpoints"),
                "export_directory": str(tmp_path / "exports"),
            },
            "security": {"export_path_allowlist": [str(tmp_path / "exports")]},
            "observability": {"log_level": "WARNING"},
        }
    )


def _model(
    adapter: FakeAutoCADAdapter, *, include_zero: bool, zero_ref: str = "zero"
) -> DrawingModel:
    entities: tuple[EntityRecord, ...] = ()
    if include_zero:
        entities = (
            EntityRecord(
                entity_ref=zero_ref,
                entity_type="AcDbLine",
                layer="OBJECT",
                visible=True,
                space="model",
                geometry=LineGeometry(start_mm=(1.0, 1.0), end_mm=(1.0, 1.0)),
                bounding_box_mm=(1.0, 1.0, 1.0, 1.0),
            ),
        )
    return DrawingModel(
        document_id=adapter.document.document_id,
        revision=adapter.current_revision(),
        display_name="remediation.dwg",
        source_unit_code="mm",
        to_mm_factor=1.0,
        geometry_normalized=True,
        scope=ReadScope(),
        entities=entities,
        arc_chord_tolerance_mm=0.01,
    )


def _registered_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    finding_remains: bool,
    introduce_replacement_defect: bool = False,
) -> tuple[HarnessService, FakeAutoCADAdapter, str, str, str, str]:
    adapter = FakeAutoCADAdapter()
    adapter.document.entities["zero"] = FakeEntity(
        entity_ref="zero",
        entity_type="AcDbLine",
        layer="OBJECT",
        feature_id="source",
        operation_id="source:zero",
        geometry={"start_mm": [1.0, 1.0], "end_mm": [1.0, 1.0]},
        measurements={"length_mm": 0.0},
    )

    def readback(_document_id: str) -> DrawingModel:
        replacement_defect = (
            introduce_replacement_defect and "zero" not in adapter.document.entities
        )
        return _model(
            adapter,
            include_zero=finding_remains
            or replacement_defect
            or "zero" in adapter.document.entities,
            zero_ref="replacement-zero" if replacement_defect else "zero",
        )

    audit_store = InMemoryDrawingAuditStore()
    service = HarnessService(
        _settings(tmp_path, monkeypatch),
        adapter,
        drawing_model_reader=readback,
        drawing_audit_store=audit_store,
    )
    job = service.create_job()
    audited = _model(adapter, include_zero=True)
    audit_store.save_drawing_audit(
        audit_id="audit-zero",
        document_id=audited.document_id,
        revision=audited.revision,
        report=audit_drawing(
            audited,
            profile=service.profile,
            tolerance=service.tolerance,
        ),
    )
    remediation = RemediationService(service.tolerance, audit_store).compile_plan(
        job_id=job.job_id,
        model=audited,
        audit_id="audit-zero",
        selected_rule_findings=(("ZERO_LENGTH_ENTITY", "zero"),),
    )
    registered = service.register_remediation_plan(remediation)
    service.preview(job.job_id)
    service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    _, token = service.approve(job.job_id, "engineer-1", ("STD-PROFILE-PROVENANCE",))
    return (
        service,
        adapter,
        token,
        str(registered["plan_hash"]),
        job.job_id,
        job.expected_revision,
    )


# Feature: cad-ai-production-roadmap, Property 64
def test_remediation_commit_reaudits_and_partitions_selected_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Validates: Requirements 22.3, 22.4, 22.7, 22.10.**"""
    service, adapter, token, plan_hash, job_id, expected_revision = _registered_service(
        tmp_path, monkeypatch, finding_remains=False
    )
    result = service.commit(
        job_id,
        idempotency_key="remediation-success",
        expected_revision=expected_revision,
        plan_hash=plan_hash,
        approval_token=token,
    )
    assert result.checkpoint_id is not None
    committed = service.store.get_job(job_id)
    assert committed is not None and committed.state is JobState.COMMITTED
    assert isinstance(service.audit, InMemoryAuditSink)
    reaudit_event = next(
        event for event in service.audit.events if event.event_type == "DRAWING_AUDITED"
    )
    assert reaudit_event.payload["resolved_count"] == 1
    assert reaudit_event.payload["remaining_count"] == 0
    assert adapter.current_revision() == result.new_revision
    assert service.store.entity_mappings_for(adapter.document.document_id) == ()


def test_remediation_restart_reloads_selection_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process restart cannot downgrade remediation into an ordinary commit."""
    service, adapter, token, plan_hash, job_id, expected_revision = _registered_service(
        tmp_path, monkeypatch, finding_remains=False
    )
    restarted = HarnessService(
        service.settings,
        adapter,
        store=service.store,
        drawing_model_reader=service._drawing_model_reader,
        drawing_audit_store=service._drawing_audit_store,
    )
    assert restarted._remediation_jobs == {}

    result = restarted.commit(
        job_id,
        idempotency_key="remediation-after-restart",
        expected_revision=expected_revision,
        plan_hash=plan_hash,
        approval_token=token,
    )

    assert result.new_revision == adapter.current_revision()
    assert isinstance(restarted.audit, InMemoryAuditSink)
    reaudit = next(
        event for event in restarted.audit.events if event.event_type == "DRAWING_AUDITED"
    )
    assert reaudit.payload["resolved_count"] == 1
    assert reaudit.payload["remaining_count"] == 0


def test_remediation_job_cannot_be_repurposed_as_a_spec_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _, job_id, _ = _registered_service(tmp_path, monkeypatch, finding_remains=False)
    with pytest.raises(InvalidFeatureParametersError, match="cannot be repurposed"):
        service.submit_spec(job_id, {})


def test_remaining_finding_fails_with_rule_and_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, token, plan_hash, job_id, expected_revision = _registered_service(
        tmp_path, monkeypatch, finding_remains=True
    )
    with pytest.raises(PostCommitValidationFailedError) as error:
        service.commit(
            job_id,
            idempotency_key="remediation-remains",
            expected_revision=expected_revision,
            plan_hash=plan_hash,
            approval_token=token,
        )
    assert error.value.details["remaining_rule_ids"] == ["ZERO_LENGTH_ENTITY"]
    assert error.value.details["remaining_findings"] == [["ZERO_LENGTH_ENTITY", "zero"]]
    assert error.value.details["checkpoint_id"] is not None
    failed = service.store.get_job(job_id)
    assert failed is not None and failed.state is JobState.FAILED


def test_revision_drift_blocks_remediation_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, adapter, token, plan_hash, job_id, expected_revision = _registered_service(
        tmp_path, monkeypatch, finding_remains=False
    )
    before_entities = dict(adapter.document.entities)
    adapter.document.write_counter += 1
    with pytest.raises(StaleDocumentRevisionError):
        service.commit(
            job_id,
            idempotency_key="remediation-stale",
            expected_revision=expected_revision,
            plan_hash=plan_hash,
            approval_token=token,
        )
    assert adapter.document.entities == before_entities
    approved = service.store.get_job(job_id)
    assert approved is not None and approved.state is JobState.APPROVED


def test_replacement_defect_cannot_hide_behind_a_deleted_selected_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, token, plan_hash, job_id, expected_revision = _registered_service(
        tmp_path,
        monkeypatch,
        finding_remains=False,
        introduce_replacement_defect=True,
    )
    with pytest.raises(PostCommitValidationFailedError) as error:
        service.commit(
            job_id,
            idempotency_key="remediation-new-defect",
            expected_revision=expected_revision,
            plan_hash=plan_hash,
            approval_token=token,
        )
    assert error.value.details["remaining_findings"] == []
    assert error.value.details["introduced_findings"] == [
        ["ZERO_LENGTH_ENTITY", "replacement-zero"]
    ]
