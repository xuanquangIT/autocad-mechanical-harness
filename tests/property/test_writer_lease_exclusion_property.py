# Feature: cad-ai-production-roadmap, Property 4: Writer lease mutual exclusion and release in the same unit of work
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cad_harness.domain.errors import WriterLeaseConflictError
from cad_harness.domain.models.job import CadJob, JobState
from cad_harness.persistence.engine import build_engine, build_session_factory, create_all
from cad_harness.persistence.sql_job_store import SqlJobStore
from cad_harness.persistence.sql_lease_store import SqlLeaseStore


@given(
    owner_a=st.text(alphabet="abc012", min_size=1, max_size=12),
    owner_b=st.text(alphabet="xyz789", min_size=1, max_size=12),
    ttl_seconds=st.integers(min_value=5, max_value=120),
)
@settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_writer_lease_is_exclusive_and_terminal_uow_releases_it(
    tmp_path, owner_a: str, owner_b: str, ttl_seconds: int
) -> None:
    """**Validates: Requirements 2.1, 2.2, 2.3, 2.7**"""
    suffix = uuid4().hex
    engine = build_engine(tmp_path / f"lease-property-{suffix}.db")
    create_all(engine)
    sessions = build_session_factory(engine)
    leases = SqlLeaseStore(sessions)
    jobs = SqlJobStore(sessions)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    ttl = timedelta(seconds=ttl_seconds)
    document_id = f"doc_{suffix}"

    first = leases.try_acquire(document_id=document_id, owner_id=owner_a, ttl=ttl, now=now)
    with pytest.raises(WriterLeaseConflictError) as conflict:
        leases.try_acquire(document_id=document_id, owner_id=owner_b, ttl=ttl, now=now)
    assert conflict.value.details == {
        "owner_id": owner_a,
        "expires_at": first.expires_at.isoformat(),
    }

    committing = CadJob(
        job_id=f"job_{suffix}",
        document_id=document_id,
        expected_revision="rev_1",
        state=JobState.COMMITTING,
    )
    jobs.save_job(committing)
    failed = committing.transition_to(JobState.FAILED)
    assert jobs.finalize_job(failed, lease_id=first.lease_id, now=now)
    assert leases.active_lease(document_id, now=now) is None

    second = leases.try_acquire(document_id=document_id, owner_id=owner_b, ttl=ttl, now=now)
    takeover_time = second.expires_at
    third = leases.try_acquire(
        document_id=document_id, owner_id=owner_a, ttl=ttl, now=takeover_time
    )
    assert third.owner_id == owner_a
    assert leases.active_lease(document_id, now=takeover_time) == third
    engine.dispose()
