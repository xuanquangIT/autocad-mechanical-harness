"""SQLite persistence for immutable take-off reports."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from cad_harness.domain.models.takeoff import TakeoffReport
from cad_harness.domain.ports.repositories import CancellationTokenPort
from cad_harness.observability.audit import AuditEventType
from cad_harness.persistence.models import TakeoffReportRow
from cad_harness.persistence.retry import DEFAULT_SQLITE_RETRY, RetryPolicy
from cad_harness.persistence.sql_audit_sink import append_audit_row
from cad_harness.security.redaction import redact_payload


class SqlTakeoffReportStore:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        retry: RetryPolicy = DEFAULT_SQLITE_RETRY,
    ) -> None:
        self._session_factory = session_factory
        self._retry = retry

    def save_takeoff_report(
        self, *, report_id: str, report: TakeoffReport, total_mass_kg: float
    ) -> None:
        def attempt() -> None:
            with self._session_factory() as session:
                session.add(
                    TakeoffReportRow(
                        report_id=report_id,
                        document_id=report.document_id,
                        revision=report.revision,
                        report_json=report.model_dump(mode="json"),
                        total_mass_kg=total_mass_kg,
                    )
                )
                session.commit()

        self._retry.run(attempt)

    def persist_created(
        self,
        *,
        report_id: str,
        report: TakeoffReport,
        total_mass_kg: float,
        actor_id: str,
        deadline: CancellationTokenPort,
    ) -> str:
        """Commit report and audit evidence at one SQLite linearization point."""

        def attempt() -> str:
            deadline.checkpoint()
            with self._session_factory() as session:
                try:
                    driver: Any = session.connection().connection.driver_connection
                    interrupt = getattr(driver, "interrupt", None)
                    if callable(interrupt):
                        deadline.add_cancel_callback(interrupt)
                    remaining_ms = max(1, int(deadline.remaining_seconds * 1_000.0))
                    session.execute(text(f"PRAGMA busy_timeout = {remaining_ms}"))
                    session.execute(text("BEGIN IMMEDIATE"))
                    session.add(
                        TakeoffReportRow(
                            report_id=report_id,
                            document_id=report.document_id,
                            revision=report.revision,
                            report_json=report.model_dump(mode="json"),
                            total_mass_kg=total_mass_kg,
                        )
                    )
                    event_id = append_audit_row(
                        session,
                        event_type=AuditEventType.TAKEOFF_REPORT_CREATED,
                        job_id=None,
                        actor_type="human",
                        actor_id=actor_id,
                        safe_payload=redact_payload(
                            {
                                "document_id": report.document_id,
                                "revision": report.revision,
                                "total_mass_kg": total_mass_kg,
                            }
                        ),
                    )
                    session.flush()
                    deadline.checkpoint()
                    session.commit()
                    return event_id
                except Exception:
                    session.rollback()
                    raise

        return self._retry.run(attempt)


__all__ = ["SqlTakeoffReportStore"]
