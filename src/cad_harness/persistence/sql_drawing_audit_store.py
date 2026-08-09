"""SQLite persistence for drawing audit reports."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from cad_harness.domain.models.validation import DrawingAuditEvidence, ValidationReport
from cad_harness.persistence.models import DrawingAuditRow
from cad_harness.persistence.retry import DEFAULT_SQLITE_RETRY, RetryPolicy


class SqlDrawingAuditStore:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        retry: RetryPolicy = DEFAULT_SQLITE_RETRY,
    ) -> None:
        self._session_factory = session_factory
        self._retry = retry

    def save_drawing_audit(
        self,
        *,
        audit_id: str,
        document_id: str,
        revision: str,
        report: ValidationReport,
    ) -> None:
        def attempt() -> None:
            with self._session_factory() as session:
                session.add(
                    DrawingAuditRow(
                        audit_id=audit_id,
                        document_id=document_id,
                        revision=revision,
                        report_json=report.model_dump(mode="json"),
                        blocking_count=report.blocking_count,
                        error_count=report.error_count,
                        warning_count=report.warning_count,
                        info_count=report.info_count,
                    )
                )
                session.commit()

        self._retry.run(attempt)

    def get_drawing_audit(self, audit_id: str) -> DrawingAuditEvidence | None:
        def attempt() -> DrawingAuditEvidence | None:
            with self._session_factory() as session:
                row = session.get(DrawingAuditRow, audit_id)
                if row is None:
                    return None
                return DrawingAuditEvidence(
                    audit_id=row.audit_id,
                    document_id=row.document_id,
                    revision=row.revision,
                    report=ValidationReport.model_validate(row.report_json),
                )

        return self._retry.run(attempt)


__all__ = ["SqlDrawingAuditStore"]
