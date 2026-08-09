"""In-memory LeaseStore for local workflows and deterministic tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

from cad_harness.domain.errors import HarnessError, WriterLeaseConflictError
from cad_harness.domain.models.lease import WriterLease


@dataclass(slots=True)
class InMemoryLeaseStore:
    leases: dict[str, WriterLease] = field(default_factory=dict)

    def try_acquire(
        self, *, document_id: str, owner_id: str, ttl: timedelta, now: datetime
    ) -> WriterLease:
        current = self.active_lease(document_id, now=now)
        if current is not None:
            raise WriterLeaseConflictError(
                "Document already has an active writer lease",
                required_action="Wait for the current writer to finish or for its lease to expire",
                details={
                    "owner_id": current.owner_id,
                    "expires_at": current.expires_at.isoformat(),
                },
            )
        lease = WriterLease(
            lease_id=f"lease_{uuid4().hex}",
            document_id=document_id,
            owner_id=owner_id,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + ttl,
        )
        self.leases[document_id] = lease
        return lease

    def renew(self, lease_id: str, *, ttl: timedelta, now: datetime) -> WriterLease:
        current = next((item for item in self.leases.values() if item.lease_id == lease_id), None)
        if current is None or current.expires_at <= now:
            raise HarnessError(
                "Writer lease is no longer active", required_action="Reconcile the commit outcome"
            )
        renewed = current.model_copy(update={"heartbeat_at": now, "expires_at": now + ttl})
        self.leases[current.document_id] = renewed
        return renewed

    def release(self, lease_id: str, *, now: datetime) -> None:
        del now
        for document_id, lease in tuple(self.leases.items()):
            if lease.lease_id == lease_id:
                self.leases.pop(document_id, None)
                return

    def active_lease(self, document_id: str, *, now: datetime) -> WriterLease | None:
        lease = self.leases.get(document_id)
        return lease if lease is not None and lease.expires_at > now else None
