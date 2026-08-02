"""Fault injection (architecture section 22.6).

Focus on the failure modes that would otherwise damage a real drawing: stale revisions,
duplicate retries, and committed geometry that does not match the plan.
"""

from __future__ import annotations

from typing import Any

import pytest

from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.domain.errors import (
    IdempotencyKeyReusedError,
    PostCommitValidationFailedError,
    RollbackNotAvailableError,
    StaleDocumentRevisionError,
)
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.validation import ValidationStage
from cad_harness.domain.ports.autocad_adapter import CommitRequest, RollbackRequest


def _prepare(service: HarnessService, spec: dict[str, Any]):
    """Drive a job to the approved state and return (job, plan_hash, token)."""
    job = service.create_job()
    submitted = service.submit_spec(job.job_id, spec)
    service.preview(job.job_id)
    service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    _, token = service.approve(job.job_id, "engineer-1", ("STD-PROFILE-PROVENANCE",))
    return job, str(submitted["plan_hash"]), token


class TestStaleRevision:
    def test_document_changed_after_approval_blocks_commit(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)

        # Someone edits the drawing between approval and commit.
        adapter.document.write_counter += 1

        with pytest.raises(StaleDocumentRevisionError):
            service.commit(
                job.job_id,
                idempotency_key="key-1",
                expected_revision=job.expected_revision,
                plan_hash=plan_hash,
                approval_token=token,
            )

    def test_nothing_is_written_when_the_revision_is_stale(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)
        adapter.document.write_counter += 1
        entity_count_before = len(adapter.document.entities)

        with pytest.raises(StaleDocumentRevisionError):
            service.commit(
                job.job_id,
                idempotency_key="key-1",
                expected_revision=job.expected_revision,
                plan_hash=plan_hash,
                approval_token=token,
            )
        assert len(adapter.document.entities) == entity_count_before

    def test_adapter_rejects_a_stale_revision_directly(self, adapter: FakeAutoCADAdapter) -> None:
        from tests.contract.test_adapter_contract import sample_plan

        plan = sample_plan(document_id=adapter.document.document_id)
        with pytest.raises(StaleDocumentRevisionError) as info:
            adapter.commit(
                CommitRequest(
                    plan=plan,
                    idempotency_key="key-1",
                    expected_revision="sha256:something-else",
                    approval_token="token",
                )
            )
        assert "actual_revision" in info.value.details


class TestIdempotency:
    def test_replaying_the_same_key_does_not_duplicate_entities(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)
        first = service.commit(
            job.job_id,
            idempotency_key="retry-key",
            expected_revision=job.expected_revision,
            plan_hash=plan_hash,
            approval_token=token,
        )
        entity_count = len(adapter.document.entities)

        second = service.commit(
            job.job_id,
            idempotency_key="retry-key",
            expected_revision=job.expected_revision,
            plan_hash=plan_hash,
            approval_token=token,
        )

        assert len(adapter.document.entities) == entity_count
        assert [e.entity_ref for e in second.entity_results] == [
            e.entity_ref for e in first.entity_results
        ]

    def test_reusing_a_key_for_different_content_is_rejected(
        self, service: HarnessService, base_plate_spec: dict[str, Any]
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)
        service.commit(
            job.job_id,
            idempotency_key="shared-key",
            expected_revision=job.expected_revision,
            plan_hash=plan_hash,
            approval_token=token,
        )

        with pytest.raises(IdempotencyKeyReusedError):
            service.commit(
                job.job_id,
                idempotency_key="shared-key",
                expected_revision="sha256:different",
                plan_hash=plan_hash,
                approval_token=token,
            )


class TestPostCommitMismatch:
    def test_measurement_mismatch_fails_the_commit(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulate an adapter that writes geometry differing from the approved plan."""
        job, plan_hash, token = _prepare(service, base_plate_spec)

        original_measure = adapter._measure

        def wrong_measure(operation):
            measurements = original_measure(operation)
            if "area_mm2" in measurements:
                measurements["area_mm2"] = float(measurements["area_mm2"]) + 500.0
            return measurements

        monkeypatch.setattr(adapter, "_measure", wrong_measure)

        with pytest.raises(PostCommitValidationFailedError) as info:
            service.commit(
                job.job_id,
                idempotency_key="key-1",
                expected_revision=job.expected_revision,
                plan_hash=plan_hash,
                approval_token=token,
            )
        assert info.value.details["checkpoint_id"] is not None
        assert service.store.get_job(job.job_id).state is JobState.FAILED  # type: ignore[union-attr]


class TestRollback:
    def test_checkpoint_restore_reverts_entities(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)
        service.commit(
            job.job_id,
            idempotency_key="key-1",
            expected_revision=job.expected_revision,
            plan_hash=plan_hash,
            approval_token=token,
        )
        assert len(adapter.document.entities) == 5

        service.rollback(job.job_id)
        assert len(adapter.document.entities) == 0
        assert service.store.get_job(job.job_id).state is JobState.ROLLED_BACK  # type: ignore[union-attr]

    def test_rollback_without_a_checkpoint_is_refused(self, adapter: FakeAutoCADAdapter) -> None:
        with pytest.raises(RollbackNotAvailableError):
            adapter.rollback(
                RollbackRequest(job_id="job_1", document_id=adapter.document.document_id)
            )
