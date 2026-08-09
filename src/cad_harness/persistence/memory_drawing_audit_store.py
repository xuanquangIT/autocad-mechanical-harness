"""In-memory persisted-audit identity store for tests and local development."""

from __future__ import annotations

from dataclasses import dataclass, field

from cad_harness.domain.models.validation import DrawingAuditEvidence, ValidationReport


@dataclass(slots=True)
class InMemoryDrawingAuditStore:
    records: dict[str, DrawingAuditEvidence] = field(default_factory=dict)

    def save_drawing_audit(
        self,
        *,
        audit_id: str,
        document_id: str,
        revision: str,
        report: ValidationReport,
    ) -> None:
        self.records[audit_id] = DrawingAuditEvidence(
            audit_id=audit_id,
            document_id=document_id,
            revision=revision,
            report=report,
        )

    def get_drawing_audit(self, audit_id: str) -> DrawingAuditEvidence | None:
        return self.records.get(audit_id)


__all__ = ["InMemoryDrawingAuditStore"]
