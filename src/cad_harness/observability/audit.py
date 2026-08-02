"""Append-only audit trail with hash chaining (architecture section 21.3).

Each event carries the previous event's hash, so removing or editing an event breaks
the chain and is detectable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.security.redaction import redact_payload


class AuditEventType(StrEnum):
    DOCUMENT_INSPECTED = "DOCUMENT_INSPECTED"
    JOB_CREATED = "JOB_CREATED"
    SPEC_SUBMITTED = "SPEC_SUBMITTED"
    SPEC_CHANGED = "SPEC_CHANGED"
    PLAN_COMPILED = "PLAN_COMPILED"
    PREVIEW_GENERATED = "PREVIEW_GENERATED"
    VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REVOKED = "APPROVAL_REVOKED"
    COMMIT_STARTED = "COMMIT_STARTED"
    COMMIT_SUCCEEDED = "COMMIT_SUCCEEDED"
    COMMIT_FAILED = "COMMIT_FAILED"
    ROLLBACK_STARTED = "ROLLBACK_STARTED"
    ROLLBACK_SUCCEEDED = "ROLLBACK_SUCCEEDED"
    EXPORT_CREATED = "EXPORT_CREATED"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    event_type: str
    job_id: str | None
    actor_type: str
    actor_id: str
    payload: dict[str, Any]
    created_at: datetime
    previous_event_hash: str | None
    event_hash: str


@dataclass(slots=True)
class InMemoryAuditSink:
    """Reference implementation. The SQLite sink writes the same chained records."""

    events: list[AuditEvent] = field(default_factory=list)

    def append(
        self,
        *,
        event_type: str,
        job_id: str | None,
        actor_type: str,
        actor_id: str,
        payload: dict[str, Any],
    ) -> str:
        previous_hash = self.events[-1].event_hash if self.events else None
        safe_payload = redact_payload(payload)
        created_at = datetime.now(UTC)
        event_id = new_id(IdPrefix.AUDIT_EVENT)
        event_hash = sha256_of(
            {
                "event_id": event_id,
                "event_type": event_type,
                "job_id": job_id,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "payload": safe_payload,
                "created_at": created_at.isoformat(),
                "previous_event_hash": previous_hash,
            }
        )
        self.events.append(
            AuditEvent(
                event_id=event_id,
                event_type=event_type,
                job_id=job_id,
                actor_type=actor_type,
                actor_id=actor_id,
                payload=safe_payload,
                created_at=created_at,
                previous_event_hash=previous_hash,
                event_hash=event_hash,
            )
        )
        return event_id

    def verify_chain(self) -> bool:
        """True when every event links to its predecessor."""
        expected_previous: str | None = None
        for event in self.events:
            if event.previous_event_hash != expected_previous:
                return False
            expected_previous = event.event_hash
        return True
