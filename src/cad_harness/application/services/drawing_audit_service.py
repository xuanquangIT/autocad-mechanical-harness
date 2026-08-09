"""Application boundary for pure drawing audit, persistence and metadata-only audit events."""

from __future__ import annotations

from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.comprehension.auditor import audit_drawing
from cad_harness.domain.models.drawing_model import DrawingModel
from cad_harness.domain.models.validation import DrawingAuditEvidence, ValidationReport
from cad_harness.domain.ports.repositories import AuditSink, DrawingAuditStore
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.observability.audit import AuditEventType


class DrawingAuditService:
    def __init__(
        self,
        *,
        store: DrawingAuditStore | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self._store = store
        self._audit = audit

    def audit(
        self,
        model: DrawingModel,
        *,
        profile: CompanyProfile,
        tolerance: ToleranceProfile,
        actor_id: str = "local-user",
    ) -> ValidationReport:
        return self.audit_with_evidence(
            model,
            profile=profile,
            tolerance=tolerance,
            actor_id=actor_id,
        ).report

    def audit_with_evidence(
        self,
        model: DrawingModel,
        *,
        profile: CompanyProfile,
        tolerance: ToleranceProfile,
        actor_id: str = "local-user",
    ) -> DrawingAuditEvidence:
        """Persist and return the identity required by remediation compilation."""
        report = audit_drawing(model, profile=profile, tolerance=tolerance)
        audit_id = new_id(IdPrefix.DRAWING_AUDIT)
        if self._store is not None:
            self._store.save_drawing_audit(
                audit_id=audit_id,
                document_id=model.document_id,
                revision=model.revision,
                report=report,
            )
        if self._audit is not None:
            self._audit.append(
                event_type=AuditEventType.DRAWING_AUDITED,
                job_id=None,
                actor_type="human",
                actor_id=actor_id,
                payload={
                    "audit_id": audit_id,
                    "document_id": model.document_id,
                    "revision": model.revision,
                    "blocking_count": report.blocking_count,
                    "error_count": report.error_count,
                    "warning_count": report.warning_count,
                    "info_count": report.info_count,
                    "entities_examined": report.entities_examined,
                },
            )
        return DrawingAuditEvidence(
            audit_id=audit_id,
            document_id=model.document_id,
            revision=model.revision,
            report=report,
        )


__all__ = ["DrawingAuditService"]
