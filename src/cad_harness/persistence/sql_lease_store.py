"""SQLite LeaseStore whose document exclusivity is enforced by the database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cad_harness.domain.errors import HarnessError, WriterLeaseConflictError
from cad_harness.domain.models.lease import WriterLease
from cad_harness.persistence.models import WriterLeaseRow
from cad_harness.persistence.retry import DEFAULT_SQLITE_RETRY, RetryPolicy


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _model(row: WriterLeaseRow) -> WriterLease:
    return WriterLease(
        lease_id=row.lease_id,
        document_id=row.document_id,
        owner_id=row.owner_id,
        acquired_at=_utc(row.acquired_at),
        expires_at=_utc(row.expires_at),
        heartbeat_at=_utc(row.heartbeat_at),
    )


class SqlLeaseStore:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        retry: RetryPolicy = DEFAULT_SQLITE_RETRY,
    ) -> None:
        self._session_factory = session_factory
        self._retry = retry

    def try_acquire(
        self, *, document_id: str, owner_id: str, ttl: timedelta, now: datetime
    ) -> WriterLease:
        lease = WriterLease(
            lease_id=f"lease_{uuid4().hex}",
            document_id=document_id,
            owner_id=owner_id,
            acquired_at=now,
            expires_at=now + ttl,
            heartbeat_at=now,
        )

        def attempt() -> WriterLease:
            with self._session_factory() as session:
                try:
                    # Deletion and insertion are one write transaction. The UNIQUE
                    # constraint, not a preceding read, decides concurrent ownership.
                    session.execute(
                        delete(WriterLeaseRow).where(
                            WriterLeaseRow.document_id == document_id,
                            WriterLeaseRow.expires_at <= now,
                        )
                    )
                    session.add(
                        WriterLeaseRow(
                            lease_id=lease.lease_id,
                            document_id=lease.document_id,
                            owner_id=lease.owner_id,
                            acquired_at=lease.acquired_at,
                            expires_at=lease.expires_at,
                            heartbeat_at=lease.heartbeat_at,
                        )
                    )
                    session.commit()
                    return lease
                except IntegrityError:
                    session.rollback()
                    current = session.scalar(
                        select(WriterLeaseRow).where(WriterLeaseRow.document_id == document_id)
                    )
                    if current is None:
                        raise
                    conflict = _model(current)
                    raise WriterLeaseConflictError(
                        "Document already has an active writer lease",
                        required_action=(
                            "Wait for the current writer to finish or for its lease to expire"
                        ),
                        details={
                            "owner_id": conflict.owner_id,
                            "expires_at": conflict.expires_at.isoformat(),
                        },
                    ) from None
                except Exception:
                    session.rollback()
                    raise

        return self._retry.run(attempt)

    def renew(self, lease_id: str, *, ttl: timedelta, now: datetime) -> WriterLease:
        def attempt() -> WriterLease:
            with self._session_factory() as session:
                try:
                    row = session.get(WriterLeaseRow, lease_id)
                    if row is None or _utc(row.expires_at) <= now:
                        raise HarnessError(
                            "Writer lease is no longer active",
                            required_action="Reconcile the commit outcome before any further write",
                            details={"lease_id": lease_id},
                        )
                    row.heartbeat_at = now
                    row.expires_at = now + ttl
                    session.commit()
                    return _model(row)
                except Exception:
                    session.rollback()
                    raise

        return self._retry.run(attempt)

    def release(self, lease_id: str, *, now: datetime) -> None:
        del now

        def attempt() -> None:
            with self._session_factory() as session:
                try:
                    session.execute(
                        delete(WriterLeaseRow).where(WriterLeaseRow.lease_id == lease_id)
                    )
                    session.commit()
                except Exception:
                    session.rollback()
                    raise

        self._retry.run(attempt)

    def active_lease(self, document_id: str, *, now: datetime) -> WriterLease | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(WriterLeaseRow).where(
                    WriterLeaseRow.document_id == document_id,
                    WriterLeaseRow.expires_at > now,
                )
            )
            return _model(row) if row is not None else None
