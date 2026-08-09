"""Versioned writer-lease contract used to serialize document ownership."""

from __future__ import annotations

from datetime import datetime

from pydantic import model_validator

from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel


class WriterLease(ContractModel):
    """Exclusive, expiring ownership of one document's write path."""

    schema_version: str = SCHEMA_VERSION
    lease_id: str
    document_id: str
    owner_id: str
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime

    @model_validator(mode="after")
    def _timestamps_are_ordered(self) -> WriterLease:
        if self.expires_at <= self.acquired_at:
            raise ValueError("expires_at must be later than acquired_at")
        if not self.acquired_at <= self.heartbeat_at < self.expires_at:
            raise ValueError("heartbeat_at must fall within the lease interval")
        return self
