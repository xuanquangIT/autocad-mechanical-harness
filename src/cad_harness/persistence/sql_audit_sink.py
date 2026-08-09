"""SQLite audit sink with a verifiable per-job hash chain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.errors import HarnessError
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.observability.audit import AuditEvent
from cad_harness.persistence.models import AuditEventRow
from cad_harness.persistence.retry import DEFAULT_SQLITE_RETRY, RetryPolicy
from cad_harness.security.redaction import redact_payload


def append_audit_row(
    session: Session,
    *,
    event_type: str,
    job_id: str | None,
    actor_type: str,
    actor_id: str,
    safe_payload: dict[str, Any],
) -> str:
    """Append one hash-chained row inside a caller-owned transaction."""
    previous = session.scalar(
        select(AuditEventRow)
        .where(SqlAuditSink._job_filter(job_id))
        .order_by(AuditEventRow.created_at.desc(), AuditEventRow.event_id.desc())
        .limit(1)
    )
    created_at = datetime.now(UTC)
    if previous is not None:
        previous_created_at = previous.created_at
        if previous_created_at.tzinfo is None:
            previous_created_at = previous_created_at.replace(tzinfo=UTC)
        if created_at <= previous_created_at:
            created_at = previous_created_at + timedelta(microseconds=1)
    event_id = new_id(IdPrefix.AUDIT_EVENT)
    row = AuditEventRow(
        event_id=event_id,
        event_type=event_type,
        job_id=job_id,
        actor_type=actor_type,
        actor_id=actor_id,
        payload_redacted_json=safe_payload,
        previous_event_hash=previous.event_hash if previous else None,
        event_hash="",
        created_at=created_at,
    )
    row.event_hash = SqlAuditSink._hash_for(row)
    session.add(row)
    return event_id


class SqlAuditSink:
    """Append redacted events using the reference in-memory hash formula."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        retry: RetryPolicy = DEFAULT_SQLITE_RETRY,
    ) -> None:
        self._session_factory = session_factory
        self._retry = retry

    @staticmethod
    def _job_filter(job_id: str | None) -> ColumnElement[bool]:
        if job_id is None:
            return AuditEventRow.job_id.is_(None)
        return AuditEventRow.job_id == job_id

    @staticmethod
    def _hash_for(row: AuditEventRow) -> str:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return sha256_of(
            {
                "event_id": row.event_id,
                "event_type": row.event_type,
                "job_id": row.job_id,
                "actor_type": row.actor_type,
                "actor_id": row.actor_id,
                "payload": row.payload_redacted_json,
                "created_at": created_at.isoformat(),
                "previous_event_hash": row.previous_event_hash,
            }
        )

    def append(
        self,
        *,
        event_type: str,
        job_id: str | None,
        actor_type: str,
        actor_id: str,
        payload: dict[str, Any],
    ) -> str:
        safe_payload = redact_payload(payload)

        def attempt() -> str:
            with self._session_factory() as session:
                try:
                    # A reserved writer lock makes reading the tail and inserting the
                    # successor one atomic operation, preventing concurrent chain forks.
                    session.execute(text("BEGIN IMMEDIATE"))
                    event_id = append_audit_row(
                        session,
                        event_type=event_type,
                        job_id=job_id,
                        actor_type=actor_type,
                        actor_id=actor_id,
                        safe_payload=safe_payload,
                    )
                    session.commit()
                    return event_id
                except Exception:
                    session.rollback()
                    raise

        return self._retry.run(attempt)

    def verify_chain(self, job_id: str | None) -> bool:
        """Verify links and event contents, raising an actionable error on tampering."""
        with self._session_factory() as session:
            rows = session.scalars(
                select(AuditEventRow)
                .where(self._job_filter(job_id))
                .order_by(AuditEventRow.created_at, AuditEventRow.event_id)
            ).all()

            expected_previous: str | None = None
            for row in rows:
                if row.previous_event_hash != expected_previous or row.event_hash != self._hash_for(
                    row
                ):
                    raise HarnessError(
                        "The persisted audit hash chain is broken",
                        required_action=(
                            "Stop write operations, preserve the database, and investigate "
                            "the audit trail"
                        ),
                        details={"broken_at_event_id": row.event_id},
                    )
                expected_previous = row.event_hash
        return True

    def events_for_job(self, job_id: str) -> tuple[AuditEvent, ...]:
        """Read a UTC-normalized timeline for local metric reproduction."""
        with self._session_factory() as session:
            rows = session.scalars(
                select(AuditEventRow)
                .where(AuditEventRow.job_id == job_id)
                .order_by(AuditEventRow.created_at, AuditEventRow.event_id)
            ).all()
            return tuple(
                AuditEvent(
                    event_id=row.event_id,
                    event_type=row.event_type,
                    job_id=row.job_id,
                    actor_type=row.actor_type,
                    actor_id=row.actor_id,
                    payload=row.payload_redacted_json,
                    created_at=(
                        row.created_at
                        if row.created_at.tzinfo is not None
                        else row.created_at.replace(tzinfo=UTC)
                    ),
                    previous_event_hash=row.previous_event_hash,
                    event_hash=row.event_hash,
                )
                for row in rows
            )
