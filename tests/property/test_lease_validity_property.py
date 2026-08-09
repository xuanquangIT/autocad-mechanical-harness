# Feature: cad-ai-production-roadmap, Property 5: Lease remains valid during commit
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.application.services.lease_service import LeaseHandle, LeaseRenewer
from cad_harness.persistence.memory_lease_store import InMemoryLeaseStore


class FailingRenewStore(InMemoryLeaseStore):
    def renew(self, lease_id, *, ttl, now):
        raise RuntimeError("injected renewal failure")


@given(elapsed_steps=st.lists(st.integers(min_value=1, max_value=5), min_size=1, max_size=30))
@settings(max_examples=100, deadline=None)
def test_heartbeat_preserves_minimum_ttl_without_cancelling_commit(
    elapsed_steps: list[int],
) -> None:
    """**Validates: Requirements 2.4**"""
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = InMemoryLeaseStore()
    lease = store.try_acquire(
        document_id="doc_property_5", owner_id="owner", ttl=timedelta(seconds=30), now=now
    )
    handle = LeaseHandle(lease)
    renewer = LeaseRenewer(
        store, handle, ttl=timedelta(seconds=30), interval_seconds=5, clock=lambda: now
    )
    observed = now
    for elapsed in elapsed_steps:
        observed += timedelta(seconds=elapsed)
        assert handle.lease.expires_at - observed >= timedelta(seconds=15)
        renewer.renew_once(now=observed)
        assert handle.lease.expires_at - observed >= timedelta(seconds=15)

    failing_handle = LeaseHandle(handle.lease)
    failing = LeaseRenewer(
        FailingRenewStore(),
        failing_handle,
        ttl=timedelta(seconds=30),
        interval_seconds=5,
        clock=lambda: observed,
    )
    commit_completed = True
    with pytest.raises(RuntimeError, match="injected"):
        failing.renew_once(now=observed)
    assert failing_handle.renewal_failed
    assert commit_completed
