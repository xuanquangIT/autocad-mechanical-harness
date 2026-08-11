"""End-to-end workflow against the fake adapter. No AutoCAD required."""

from __future__ import annotations

from typing import Any

import pytest

from cad_harness.application.services.harness_service import HarnessService
from cad_harness.domain.errors import ApprovalRequiredError, PlanHashMismatchError
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.operation_plan import OperationType
from cad_harness.domain.models.validation import Severity, ValidationStage


def _approve_and_commit(
    service: HarnessService, job_id: str, plan_hash: str, revision: str, key: str = "key-1"
):
    _, token = service.approve(job_id, "engineer-1", ("STD-PROFILE-PROVENANCE",))
    return service.commit(
        job_id,
        idempotency_key=key,
        expected_revision=revision,
        plan_hash=plan_hash,
        approval_token=token,
    )


class TestHappyPath:
    def test_full_workflow(self, service: HarnessService, base_plate_spec: dict[str, Any]) -> None:
        job = service.create_job()
        assert job.state is JobState.CREATED

        submitted = service.submit_spec(job.job_id, base_plate_spec)
        assert submitted["status"] == "ok"
        plan = service.store.get_plan(job.job_id)
        assert plan is not None
        assert submitted["operation_count"] == len(plan.operations)
        assert len(plan.operations) > 2  # Geometry plus phase-two annotations.
        plan_hash = str(submitted["plan_hash"])

        preview = service.preview(job.job_id)
        assert {a["kind"] for a in preview["artifacts"]} == {"dxf", "svg"}
        assert preview["semantic_diff"]["summary"]["added"] == len(plan.operations)

        report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)
        assert report.blocking_count == 0
        assert report.error_count == 0
        assert report.gate_allows_commit()

        result = _approve_and_commit(service, job.job_id, plan_hash, job.expected_revision)
        expected_entity_count = sum(
            len(operation.geometry["centers_mm"])
            if operation.type is OperationType.CREATE_CIRCLES
            else 1
            for operation in plan.operations
        )
        assert len(result.entity_results) == expected_entity_count
        assert result.new_revision != result.previous_revision
        assert service.store.get_job(job.job_id).state is JobState.COMMITTED  # type: ignore[union-attr]

    def test_demo_profile_raises_a_provenance_warning(
        self, service: HarnessService, base_plate_spec: dict[str, Any]
    ) -> None:
        """A non-approved profile must be surfaced, not hidden."""
        job = service.create_job()
        service.submit_spec(job.job_id, base_plate_spec)
        report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)

        provenance = [f for f in report.findings if f.rule_id == "STD-PROFILE-PROVENANCE"]
        assert len(provenance) == 1
        assert provenance[0].severity is Severity.WARNING

    def test_precommit_blocks_styles_missing_from_live_drawing(
        self, service: HarnessService, base_plate_spec: dict[str, Any]
    ) -> None:
        job = service.create_job()
        service.submit_spec(job.job_id, base_plate_spec)
        snapshot = service._snapshots[job.document_id]
        service._snapshots[job.document_id] = snapshot.model_copy(
            update={
                "dimension_styles": ("Standard",),
                "text_styles": ("Standard",),
            }
        )

        report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)

        assert report.has_blocking
        assert {
            finding.rule_id for finding in report.findings if finding.rule_id.startswith("LIVE_")
        } == {"LIVE_DIMSTYLE_MISSING", "LIVE_TEXTSTYLE_MISSING"}
        assert not report.gate_allows_commit()

    def test_plan_hash_is_reproducible_across_jobs(
        self, service: HarnessService, base_plate_spec: dict[str, Any]
    ) -> None:
        """Same spec, same profile, same hash - the determinism guarantee."""
        first = service.submit_spec(service.create_job().job_id, base_plate_spec)
        second = service.submit_spec(service.create_job().job_id, base_plate_spec)
        assert first["plan_hash"] == second["plan_hash"]


class TestMissingInputs:
    def test_missing_datum_and_origin_returns_field_paths(self, service: HarnessService) -> None:
        job = service.create_job()
        spec = {
            "units": "mm",
            "features": [
                {
                    "feature_id": "plate-1",
                    "type": "rectangular_plate",
                    "parameters": {"width_mm": 160.0, "height_mm": 100.0, "thickness_mm": 12.0},
                }
            ],
        }
        result = service.submit_spec(job.job_id, spec)
        assert result["status"] == "needs_input"
        paths = [entry["path"] for entry in result["missing_inputs"]]
        assert any("origin_mm" in path for path in paths)

    def test_no_plan_is_stored_when_inputs_are_missing(self, service: HarnessService) -> None:
        job = service.create_job()
        service.submit_spec(
            job.job_id,
            {"features": [{"feature_id": "p", "type": "rectangular_plate", "parameters": {}}]},
        )
        assert service.store.get_plan(job.job_id) is None


class TestApprovalGate:
    def test_commit_without_approval_is_refused(
        self, service: HarnessService, base_plate_spec: dict[str, Any]
    ) -> None:
        job = service.create_job()
        submitted = service.submit_spec(job.job_id, base_plate_spec)
        service.preview(job.job_id)
        service.validate(job.job_id, ValidationStage.PRE_COMMIT)

        with pytest.raises(ApprovalRequiredError):
            service.commit(
                job.job_id,
                idempotency_key="key-1",
                expected_revision=job.expected_revision,
                plan_hash=str(submitted["plan_hash"]),
                approval_token="not-a-real-token",
            )

    def test_approval_before_validation_is_refused(
        self, service: HarnessService, base_plate_spec: dict[str, Any]
    ) -> None:
        job = service.create_job()
        service.submit_spec(job.job_id, base_plate_spec)
        with pytest.raises(ApprovalRequiredError):
            service.approve(job.job_id, "engineer-1")

    def test_wrong_plan_hash_is_refused(
        self, service: HarnessService, base_plate_spec: dict[str, Any]
    ) -> None:
        job = service.create_job()
        service.submit_spec(job.job_id, base_plate_spec)
        service.preview(job.job_id)
        service.validate(job.job_id, ValidationStage.PRE_COMMIT)
        _, token = service.approve(job.job_id, "engineer-1", ("STD-PROFILE-PROVENANCE",))

        with pytest.raises(PlanHashMismatchError):
            service.commit(
                job.job_id,
                idempotency_key="key-1",
                expected_revision=job.expected_revision,
                plan_hash="sha256:not-the-approved-plan",
                approval_token=token,
            )


class TestSpecChangeInvalidatesApproval:
    def test_resubmitting_a_spec_revokes_the_approval(
        self, service: HarnessService, base_plate_spec: dict[str, Any]
    ) -> None:
        job = service.create_job()
        service.submit_spec(job.job_id, base_plate_spec)
        service.preview(job.job_id)
        service.validate(job.job_id, ValidationStage.PRE_COMMIT)
        approval_id, _ = service.approve(job.job_id, "engineer-1", ("STD-PROFILE-PROVENANCE",))
        assert service.store.get_approval(approval_id) is not None

        changed = dict(base_plate_spec)
        changed["features"] = [
            {
                **base_plate_spec["features"][0],
                "parameters": {**base_plate_spec["features"][0]["parameters"], "width_mm": 200.0},
            }
        ]
        service.submit_spec(job.job_id, changed)

        assert service.store.get_approval(approval_id) is None
        assert service.store.get_job(job.job_id).approval_id is None  # type: ignore[union-attr]


class TestWarningAcknowledgement:
    def test_service_refuses_unacknowledged_warning_rules(
        self, service: HarnessService, base_plate_spec: dict[str, Any]
    ) -> None:
        job = service.create_job()
        service.submit_spec(job.job_id, base_plate_spec)
        service.preview(job.job_id)
        report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)
        warning_rule_ids = {
            finding.rule_id for finding in report.findings if finding.severity is Severity.WARNING
        }
        assert warning_rule_ids == {"STD-PROFILE-PROVENANCE"}

        with pytest.raises(ApprovalRequiredError) as caught:
            service.approve(job.job_id, "engineer-1")
        assert caught.value.details == {"missing_warning_rule_ids": ["STD-PROFILE-PROVENANCE"]}

        approval_id, token = service.approve(
            job.job_id,
            "engineer-1",
            tuple(warning_rule_ids),
        )
        assert approval_id.startswith("approval_")
        assert token.startswith("v2.")
        assert len(token.split(".")) == 3


class TestAudit:
    def test_lifecycle_events_are_recorded_and_chained(
        self, service: HarnessService, base_plate_spec: dict[str, Any]
    ) -> None:
        job = service.create_job()
        submitted = service.submit_spec(job.job_id, base_plate_spec)
        service.preview(job.job_id)
        service.validate(job.job_id, ValidationStage.PRE_COMMIT)
        _approve_and_commit(service, job.job_id, str(submitted["plan_hash"]), job.expected_revision)

        recorded = [event.event_type for event in service.audit.events]
        for expected in (
            "DOCUMENT_INSPECTED",
            "JOB_CREATED",
            "SPEC_SUBMITTED",
            "PLAN_COMPILED",
            "PREVIEW_GENERATED",
            "VALIDATION_COMPLETED",
            "APPROVAL_GRANTED",
            "COMMIT_STARTED",
            "COMMIT_SUCCEEDED",
        ):
            assert expected in recorded
        assert service.audit.verify_chain()

    def test_audit_payloads_do_not_carry_tokens(
        self, service: HarnessService, base_plate_spec: dict[str, Any]
    ) -> None:
        job = service.create_job()
        submitted = service.submit_spec(job.job_id, base_plate_spec)
        service.preview(job.job_id)
        service.validate(job.job_id, ValidationStage.PRE_COMMIT)
        _approve_and_commit(service, job.job_id, str(submitted["plan_hash"]), job.expected_revision)

        for event in service.audit.events:
            assert "approval_token" not in event.payload or event.payload["approval_token"] == (
                "[redacted]"
            )
