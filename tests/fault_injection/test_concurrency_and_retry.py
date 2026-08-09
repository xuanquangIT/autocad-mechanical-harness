"""Deterministic fault matrix for Requirements 25.8/25.9 (Task 31.4).

Pre-commit failures must leave no live entities.  Once the atomic commit boundary may
have been crossed, an unconfirmed transport outcome must be ``UNKNOWN_COMMIT_STATE``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

import cad_harness.adapters.fake as fake_adapter_module
from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter
from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.domain.errors import (
    ApprovalExpiredError,
    AutoCADBusyError,
    AutoCADNotRunningError,
    ComCallFailedError,
    HarnessError,
    IdempotencyKeyReusedError,
    IpcTimeoutError,
    PostCommitValidationFailedError,
    RollbackNotAvailableError,
    StaleDocumentRevisionError,
    UnknownCommitStateError,
)
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.result import RollbackResult
from cad_harness.domain.models.validation import ValidationStage
from cad_harness.domain.ports.autocad_adapter import CommitRequest, RollbackRequest
from cad_harness.persistence.retry import RetryPolicy
from cad_harness.security.approval import make_approval_token


def _prepare(service: HarnessService, spec: dict[str, Any]):
    """Drive a job to the approved state and return (job, plan_hash, token)."""
    job = service.create_job()
    submitted = service.submit_spec(job.job_id, spec)
    service.preview(job.job_id)
    service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    _, token = service.approve(job.job_id, "engineer-1", ("STD-PROFILE-PROVENANCE",))
    return job, str(submitted["plan_hash"]), token


def _commit(
    service: HarnessService,
    job_id: str,
    expected_revision: str,
    plan_hash: str,
    token: str,
    *,
    key: str = "fault-key",
):
    return service.commit(
        job_id,
        idempotency_key=key,
        expected_revision=expected_revision,
        plan_hash=plan_hash,
        approval_token=token,
    )


def _bridge_commit_request() -> CommitRequest:
    plan = OperationPlan(
        plan_id="plan-fault-ipc",
        job_id="job-fault-ipc",
        document_id="doc-fault-ipc",
        expected_revision="sha256:old",
        profile_ref="fault@1",
        operations=tuple(
            Operation(
                operation_id=f"op-{index}",
                feature_id=f"feature-{index}",
                type=OperationType.CREATE_LINE,
                layer="OBJECT",
                geometry={
                    "start_mm": [float(index), 0.0],
                    "end_mm": [float(index + 1), 0.0],
                },
            )
            for index in range(2)
        ),
    ).with_hash()
    return CommitRequest(
        plan=plan,
        idempotency_key="ipc-fault-key",
        expected_revision="sha256:old",
        approval_token="opaque-test-token",
    )


class _DisconnectingTransport:
    """Atomic IPC oracle with observable staging and commit boundaries."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.staged_entities = 0
        self.committed_entities = 0

    def request(self, envelope: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        del timeout_seconds
        if envelope["method"] == "handshake":
            return {
                "schema_version": SCHEMA_VERSION,
                "request_id": envelope["request_id"],
                "status": "ok",
                "data": {
                    "schema_version": SCHEMA_VERSION,
                    "capabilities": [
                        capability.value
                        for capability in DotNetBridgeAdapter.PRODUCTION_CAPABILITIES
                    ],
                    "supported_operations": [operation.value for operation in OperationType],
                    "cad_application": "AutoCAD",
                    "cad_version": "25.1s (LMS Tech)",
                },
            }

        assert envelope["method"] == "commit"
        operation_count = len(envelope["params"]["plan"]["operations"])
        if self.stage == "before":
            raise self._confirmed_abort("precommit")

        self.staged_entities = operation_count
        if self.stage == "during":
            self.staged_entities = 0
            raise self._confirmed_abort("precommit")

        assert self.stage == "after"
        self.committed_entities = self.staged_entities
        self.staged_entities = 0
        raise IpcTimeoutError(
            "pipe disconnected after commit started",
            details={"cancellation_stage": "commit_started"},
        )

    @staticmethod
    def _confirmed_abort(stage: str) -> IpcTimeoutError:
        return IpcTimeoutError(
            "pipe disconnected before the commit boundary",
            details={
                "terminal_cancel_confirmed": True,
                "cancellation_stage": stage,
                "transaction_aborted": True,
            },
        )


class _VirtualClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.seconds += seconds


class TestAtomicFailureBoundaries:
    def test_autocad_closes_mid_commit_without_partial_entities(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)
        original_execute = adapter._execute_operation
        operation_calls = 0

        def close_during_second_operation(operation, entities):
            nonlocal operation_calls
            operation_calls += 1
            if operation_calls == 2:
                raise AutoCADNotRunningError("AutoCAD closed during the transaction")
            return original_execute(operation, entities)

        monkeypatch.setattr(adapter, "_execute_operation", close_during_second_operation)

        with pytest.raises(AutoCADNotRunningError):
            _commit(service, job.job_id, job.expected_revision, plan_hash, token)

        assert operation_calls == 2
        assert adapter.document.entities == {}
        assert service.store.get_job(job.job_id).state is JobState.FAILED  # type: ignore[union-attr]

    def test_nth_operation_failure_aborts_all_staged_entities(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)
        original_execute = adapter._execute_operation
        operation_calls = 0

        def fail_seventh_operation(operation, entities):
            nonlocal operation_calls
            operation_calls += 1
            if operation_calls == 7:
                raise ComCallFailedError("injected operation 7 failure")
            return original_execute(operation, entities)

        monkeypatch.setattr(adapter, "_execute_operation", fail_seventh_operation)

        with pytest.raises(ComCallFailedError):
            _commit(service, job.job_id, job.expected_revision, plan_hash, token)

        assert operation_calls == 7
        assert adapter.document.entities == {}
        assert service.store.get_job(job.job_id).state is JobState.FAILED  # type: ignore[union-attr]

    def test_autocad_busy_fails_before_any_entity_is_written(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)

        def report_busy(_request: CommitRequest):
            raise AutoCADBusyError("AutoCAD command context is busy")

        monkeypatch.setattr(adapter, "commit", report_busy)

        with pytest.raises(AutoCADBusyError) as caught:
            _commit(service, job.job_id, job.expected_revision, plan_hash, token)

        assert caught.value.retryable is True
        assert adapter.document.entities == {}
        assert service.store.get_job(job.job_id).state is JobState.FAILED  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("stage", "expected_error", "expected_committed"),
    [
        pytest.param("before", IpcTimeoutError, 0, id="before-commit"),
        pytest.param("during", IpcTimeoutError, 0, id="during-transaction"),
        pytest.param("after", UnknownCommitStateError, 2, id="after-commit-boundary"),
    ],
)
def test_ipc_disconnect_respects_atomic_commit_boundary(
    stage: str,
    expected_error: type[HarnessError],
    expected_committed: int,
) -> None:
    transport = _DisconnectingTransport(stage)
    adapter = DotNetBridgeAdapter(transport=transport)

    with pytest.raises(expected_error) as caught:
        adapter.commit(_bridge_commit_request())

    assert transport.staged_entities == 0
    assert transport.committed_entities == expected_committed
    if stage == "after":
        assert isinstance(caught.value, UnknownCommitStateError)
        assert caught.value.retryable is False
    else:
        assert type(caught.value) is IpcTimeoutError
        assert caught.value.details["transaction_aborted"] is True


class TestStorageFaults:
    def test_sqlite_lock_retries_on_virtual_clock_without_sleeping(
        self, adapter: FakeAutoCADAdapter
    ) -> None:
        clock = _VirtualClock()
        attempts = 0
        policy = RetryPolicy(
            max_attempts=3,
            budget_seconds=1.0,
            initial_delay_seconds=0.1,
            backoff_multiplier=2.0,
            clock=clock.monotonic,
            sleep=clock.advance,
        )

        def locked_write() -> None:
            nonlocal attempts
            attempts += 1
            raise OperationalError("INSERT", {}, Exception("database is locked"))

        with pytest.raises(HarnessError, match="SQLite remained locked") as caught:
            policy.run(locked_write)

        assert attempts == 3
        assert clock.sleeps == [0.1, 0.2]
        assert caught.value.details["attempts"] == 3
        assert adapter.document.entities == {}

    def test_disk_full_while_creating_preview_never_touches_live_document(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cad_harness.adapters.dxf_preview import DxfPreviewAdapter

        job = service.create_job()
        service.submit_spec(job.job_id, base_plate_spec)

        def disk_full(_adapter: DxfPreviewAdapter, _plan: OperationPlan):
            raise OSError("disk full")

        monkeypatch.setattr(DxfPreviewAdapter, "preview", disk_full)

        with pytest.raises(OSError, match="disk full"):
            service.preview(job.job_id)

        assert adapter.document.entities == {}
        assert service.store.get_job(job.job_id).state is JobState.PLANNED  # type: ignore[union-attr]

    def test_disk_full_while_creating_checkpoint_aborts_before_write(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)

        def disk_full(_value: object):
            raise OSError("disk full while creating checkpoint")

        monkeypatch.setattr(fake_adapter_module, "deepcopy", disk_full)

        with pytest.raises(OSError, match="checkpoint"):
            _commit(service, job.job_id, job.expected_revision, plan_hash, token)

        assert adapter.document.entities == {}
        assert adapter.document.snapshots == {}
        assert service.store.get_job(job.job_id).state is JobState.FAILED  # type: ignore[union-attr]


def test_expired_approval_is_rejected_before_commit(
    service: HarnessService,
    adapter: FakeAutoCADAdapter,
    base_plate_spec: dict[str, Any],
) -> None:
    job, plan_hash, _token = _prepare(service, base_plate_spec)
    approved_job = service.store.get_job(job.job_id)
    assert approved_job is not None
    assert approved_job.approval_id is not None
    approval = service.store.get_approval(approved_job.approval_id)
    assert approval is not None
    expired = approval.model_copy(
        update={
            "approved_at": datetime(1999, 1, 1, tzinfo=UTC),
            "expires_at": datetime(2000, 1, 1, tzinfo=UTC),
        }
    )
    service.store.save_approval(expired)
    expired_token = make_approval_token(expired, service.settings.approval_secret())

    with pytest.raises(ApprovalExpiredError):
        _commit(service, job.job_id, job.expected_revision, plan_hash, expired_token)

    assert adapter.document.entities == {}
    assert service.store.get_job(job.job_id).state is JobState.APPROVED  # type: ignore[union-attr]


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
        failed = service.store.get_job(job.job_id)
        assert failed is not None and failed.state is JobState.FAILED
        assert failed.checkpoint_id == info.value.details["checkpoint_id"]
        assert failed.expected_revision == adapter.current_revision()
        assert service.store.find_execution(job_id=job.job_id, idempotency_key="key-1")


class TestRollback:
    def test_checkpoint_restore_reverts_entities(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)
        plan = service.store.get_plan(job.job_id)
        assert plan is not None
        expected_entity_count = sum(
            len(operation.geometry["centers_mm"])
            if operation.type is OperationType.CREATE_CIRCLES
            else 1
            for operation in plan.operations
        )
        service.commit(
            job.job_id,
            idempotency_key="key-1",
            expected_revision=job.expected_revision,
            plan_hash=plan_hash,
            approval_token=token,
        )
        assert len(adapter.document.entities) == expected_entity_count

        scope = service.rollback_scope(job.job_id)
        _, rollback_token = service.approve_rollback(
            job.job_id,
            "engineer-rollback",
            displayed_checkpoint_id=scope["checkpoint_id"],
            displayed_current_revision=scope["current_revision"],
        )
        service.rollback(
            job.job_id,
            checkpoint_id=scope["checkpoint_id"],
            current_revision=scope["current_revision"],
            rollback_approval_token=rollback_token,
        )
        assert len(adapter.document.entities) == 0
        assert service.store.get_job(job.job_id).state is JobState.ROLLED_BACK  # type: ignore[union-attr]

    def test_session_undo_receipt_avoids_preflight_commands(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)
        result = service.commit(
            job.job_id,
            idempotency_key="session-undo-commit",
            expected_revision=job.expected_revision,
            plan_hash=plan_hash,
            approval_token=token,
        )
        assert result.undo_group is not None

        def unexpected_preflight(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("session rollback must not issue status or revision commands")

        captured: list[RollbackRequest] = []

        def rollback_once(request: RollbackRequest) -> RollbackResult:
            captured.append(request)
            return RollbackResult(
                job_id=request.job_id,
                restored_revision=result.previous_revision,
                checkpoint_id=request.checkpoint_id,
                method="undo_group",
            )

        monkeypatch.setattr(adapter, "status", unexpected_preflight)
        monkeypatch.setattr(adapter, "validate_revision", unexpected_preflight)
        monkeypatch.setattr(adapter, "rollback", rollback_once)

        scope = service.rollback_scope(job.job_id)
        _, rollback_token = service.approve_rollback(
            job.job_id,
            "engineer-session-undo",
            displayed_checkpoint_id=scope["checkpoint_id"],
            displayed_current_revision=scope["current_revision"],
        )
        service.rollback(
            job.job_id,
            checkpoint_id=scope["checkpoint_id"],
            current_revision=scope["current_revision"],
            rollback_approval_token=rollback_token,
        )

        assert len(captured) == 1
        assert captured[0].undo_group == result.undo_group
        assert captured[0].current_revision == result.new_revision

    def test_changed_revision_after_approval_is_refused_without_mutation(
        self,
        service: HarnessService,
        adapter: FakeAutoCADAdapter,
        base_plate_spec: dict[str, Any],
    ) -> None:
        job, plan_hash, token = _prepare(service, base_plate_spec)
        service.commit(
            job.job_id,
            idempotency_key="rollback-stale-commit",
            expected_revision=job.expected_revision,
            plan_hash=plan_hash,
            approval_token=token,
        )
        scope = service.rollback_scope(job.job_id)
        _, rollback_token = service.approve_rollback(
            job.job_id,
            "engineer-rollback",
            displayed_checkpoint_id=scope["checkpoint_id"],
            displayed_current_revision=scope["current_revision"],
        )
        entities = dict(adapter.document.entities)
        adapter.document.write_counter += 1

        with pytest.raises(StaleDocumentRevisionError):
            service.rollback(
                job.job_id,
                checkpoint_id=scope["checkpoint_id"],
                current_revision=scope["current_revision"],
                rollback_approval_token=rollback_token,
            )
        assert adapter.document.entities == entities

    def test_unknown_or_prewrite_failed_jobs_cannot_request_rollback_scope(
        self,
        service: HarnessService,
    ) -> None:
        job = service.create_job()
        for state in (JobState.FAILED, JobState.UNKNOWN_COMMIT_STATE):
            service.store.save_job(job.model_copy(update={"state": state}))
            with pytest.raises(RollbackNotAvailableError):
                service.rollback_scope(job.job_id)

    def test_rollback_without_a_checkpoint_is_refused(self, adapter: FakeAutoCADAdapter) -> None:
        with pytest.raises(RollbackNotAvailableError):
            adapter.rollback(
                RollbackRequest(
                    job_id="job_1",
                    document_id=adapter.document.document_id,
                    checkpoint_id="missing",
                    current_revision=adapter.current_revision(),
                    rollback_approval_token="adapter-contract-only",
                )
            )
