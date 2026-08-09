"""Validation findings and reports (architecture sections 7.5 and 15)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKING = "blocking"


class ValidationStage(StrEnum):
    """Pipeline stages from architecture section 7.5."""

    SCHEMA = "schema"
    SEMANTIC_INPUT = "semantic_input"
    PLAN = "plan"
    PREVIEW_GEOMETRY = "preview_geometry"
    COMPANY_STANDARD = "company_standard"
    PRE_COMMIT = "pre_commit"
    POST_COMMIT = "post_commit"
    #: Rules run against an existing drawing read back from CAD rather than a plan.
    #: The same rule objects serve both directions; only the input differs.
    DRAWING_AUDIT = "drawing_audit"
    DRAWING_STANDARD = "drawing_standard"


class Finding(ContractModel):
    """One rule outcome, always carrying evidence a human can check."""

    rule_id: str
    severity: Severity
    message: str
    feature_id: str | None = None
    entity_ref: str | None = None
    operation_id: str | None = None
    expected: Any = None
    actual: Any = None
    tolerance: float | None = None
    suggested_fix: str | None = None
    measurement: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(ContractModel):
    schema_version: str = SCHEMA_VERSION
    validation_id: str
    job_id: str
    stage: ValidationStage
    plan_hash: str | None = None
    findings: tuple[Finding, ...] = ()
    #: How many entities (or plan operations) the rules actually looked at. A report
    #: with zero findings means nothing unless the reader knows the scope examined.
    entities_examined: int = 0
    #: False unless the profile behind this report is engineer-signed. Defaults to
    #: False so a report never silently claims company approval.
    company_approved: bool = False
    #: Exact standards profile used for this decision, including demo profiles.
    profile_ref: str | None = None
    blocking_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0

    @model_validator(mode="after")
    def _derive_severity_counts(self) -> ValidationReport:
        """Serialize authoritative counts so API clients need not recalculate them."""
        for field_name, severity in (
            ("blocking_count", Severity.BLOCKING),
            ("error_count", Severity.ERROR),
            ("warning_count", Severity.WARNING),
            ("info_count", Severity.INFO),
        ):
            object.__setattr__(self, field_name, self.count(severity))
        return self

    def count(self, severity: Severity) -> int:
        return sum(1 for f in self.findings if f.severity is severity)

    @property
    def has_blocking(self) -> bool:
        return self.blocking_count > 0

    def gate_allows_commit(self, *, block_on_error: bool = True) -> bool:
        """Commit gate from architecture section 15.5.

        Blocking always stops a commit. Errors stop it under the default policy;
        warnings are allowed through but must be shown in the approval surface.
        """
        if self.has_blocking:
            return False
        return not (block_on_error and self.error_count > 0)


class DrawingAuditEvidence(ContractModel):
    """Persisted identity binding an audit report to one document revision."""

    audit_id: str
    document_id: str
    revision: str
    report: ValidationReport
