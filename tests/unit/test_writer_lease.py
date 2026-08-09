"""Focused examples for writer lease lifecycle and unknown-commit safety."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.application.services.lease_service import LeaseService
from cad_harness.application.services.reconciliation_service import ReconciliationService
from cad_harness.config import Settings
from cad_harness.domain.errors import UnknownCommitStateError, WriterLeaseConflictError
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.lease import WriterLease
from cad_harness.domain.models.result import CommitResult
from cad_harness.domain.models.validation import ValidationStage
from cad_harness.domain.ports.autocad_adapter import CommitRequest
from cad_harness.persistence.engine import build_engine, build_session_factory, create_all
from cad_harness.persistence.memory_lease_store import InMemoryLeaseStore
from cad_harness.persistence.memory_store import InMemoryJobStore
from cad_harness.persistence.sql_lease_store import SqlLeaseStore


def test_writer_lease_validates_time_order_and_sql_conflict_payload(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="expires_at"):
        WriterLease(
            lease_id="lease_bad",
            document_id="doc",
            owner_id="owner",
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now,
        )

    engine = build_engine(tmp_path / "lease.db")
    create_all(engine)
    store = SqlLeaseStore(build_session_factory(engine))
    first = store.try_acquire(
        document_id="doc", owner_id="owner-a", ttl=timedelta(seconds=30), now=now
    )
    with pytest.raises(WriterLeaseConflictError) as caught:
        store.try_acquire(document_id="doc", owner_id="owner-b", ttl=timedelta(seconds=30), now=now)
    assert caught.value.details["owner_id"] == "owner-a"
    assert caught.value.details["expires_at"] == first.expires_at.isoformat()


class AdvancingAdapter(FakeAutoCADAdapter):
    def __init__(self, clock: list[datetime]) -> None:
        super().__init__()
        self.clock = clock
        self.commit_calls = 0

    def commit(self, request: CommitRequest) -> CommitResult:
        self.commit_calls += 1
        result = super().commit(request)
        self.clock[0] += timedelta(seconds=31)
        return result


class UnconfirmedCommitAdapter(FakeAutoCADAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.commit_calls = 0

    def commit(self, request: CommitRequest) -> CommitResult:
        self.commit_calls += 1
        raise UnknownCommitStateError("Bridge did not confirm the commit outcome")


def test_unconfirmed_bridge_commit_marks_unknown_while_lease_is_valid(
    settings: Settings, base_plate_spec: dict[str, Any]
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    adapter = UnconfirmedCommitAdapter()
    store = InMemoryJobStore()
    leases = InMemoryLeaseStore()
    service = HarnessService(
        settings,
        adapter,
        store=store,
        lease_service=LeaseService(
            leases,
            ttl_seconds=30,
            heartbeat_interval_seconds=5,
            minimum_remaining_seconds=15,
            clock=lambda: now,
        ),
    )
    job = service.create_job()
    submitted = service.submit_spec(job.job_id, base_plate_spec)
    service.preview(job.job_id)
    service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    _, token = service.approve(job.job_id, "engineer", ("STD-PROFILE-PROVENANCE",))

    with pytest.raises(UnknownCommitStateError, match="did not confirm"):
        service.commit(
            job.job_id,
            idempotency_key="unconfirmed-key",
            expected_revision=job.expected_revision,
            plan_hash=str(submitted["plan_hash"]),
            approval_token=token,
        )

    assert store.get_job(job.job_id).state is JobState.UNKNOWN_COMMIT_STATE  # type: ignore[union-attr]
    assert leases.active_lease(job.document_id, now=now) is None
    assert adapter.commit_calls == 1

    with pytest.raises(UnknownCommitStateError, match="cannot be committed automatically"):
        service.commit(
            job.job_id,
            idempotency_key="unconfirmed-key",
            expected_revision=job.expected_revision,
            plan_hash=str(submitted["plan_hash"]),
            approval_token=token,
        )
    assert adapter.commit_calls == 1


def test_expired_lease_marks_unknown_and_never_recommits(
    settings: Settings, base_plate_spec: dict[str, Any]
) -> None:
    clock = [datetime(2030, 1, 1, tzinfo=UTC)]
    adapter = AdvancingAdapter(clock)
    store = InMemoryJobStore()
    leases = InMemoryLeaseStore()
    service = HarnessService(
        settings,
        adapter,
        store=store,
        lease_service=LeaseService(
            leases,
            ttl_seconds=30,
            heartbeat_interval_seconds=5,
            minimum_remaining_seconds=15,
            clock=lambda: clock[0],
        ),
    )
    job = service.create_job()
    submitted = service.submit_spec(job.job_id, base_plate_spec)
    service.preview(job.job_id)
    service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    _, token = service.approve(job.job_id, "engineer", ("STD-PROFILE-PROVENANCE",))

    with pytest.raises(UnknownCommitStateError):
        service.commit(
            job.job_id,
            idempotency_key="unknown-key",
            expected_revision=job.expected_revision,
            plan_hash=str(submitted["plan_hash"]),
            approval_token=token,
        )
    assert store.get_job(job.job_id).state is JobState.UNKNOWN_COMMIT_STATE  # type: ignore[union-attr]
    assert adapter.commit_calls == 1
    assert leases.active_lease(job.document_id, now=clock[0]) is None

    with pytest.raises(UnknownCommitStateError):
        service.commit(
            job.job_id,
            idempotency_key="unknown-key",
            expected_revision=job.expected_revision,
            plan_hash=str(submitted["plan_hash"]),
            approval_token=token,
        )
    assert adapter.commit_calls == 1
    report = ReconciliationService(store, adapter).reconcile(job.job_id)
    assert report.differences == {}
    assert adapter.commit_calls == 1
