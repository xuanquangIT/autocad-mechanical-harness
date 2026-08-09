"""Persistence port for exclusive writer leases."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from cad_harness.domain.models.lease import WriterLease


@runtime_checkable
class LeaseStore(Protocol):
    def try_acquire(
        self, *, document_id: str, owner_id: str, ttl: timedelta, now: datetime
    ) -> WriterLease: ...

    def renew(self, lease_id: str, *, ttl: timedelta, now: datetime) -> WriterLease: ...

    def release(self, lease_id: str, *, now: datetime) -> None: ...

    def active_lease(self, document_id: str, *, now: datetime) -> WriterLease | None: ...
