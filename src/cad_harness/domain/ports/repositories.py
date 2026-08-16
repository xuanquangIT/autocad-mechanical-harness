"""Persistence and audit ports. SQLite/SQLAlchemy implementations live in persistence/."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from cad_harness.domain.models.approval import ApprovalRecord
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.job import CadJob
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.result import Checkpoint, EntityMappingRecord
from cad_harness.domain.models.takeoff import TakeoffReport
from cad_harness.domain.models.validation import DrawingAuditEvidence, ValidationReport


@runtime_checkable
class ApprovalStore(Protocol):
    def save_approval(self, approval: ApprovalRecord) -> None: ...

    def get_approval(self, approval_id: str) -> ApprovalRecord | None: ...

    def revoke_approvals_for_job(self, job_id: str) -> None: ...


@runtime_checkable
class JobStore(ApprovalStore, Protocol):
    """Job aggregate persistence, including idempotency bookkeeping.

    Approvals, entity mappings and checkpoints belong to the same aggregate: a commit
    writes job state, entity mappings and the execution record as one unit of work, so
    one port covers them rather than forcing callers to hold several stores in sync.
    """

    def save_job(self, job: CadJob) -> None: ...

    def get_job(self, job_id: str) -> CadJob | None: ...

    def save_spec(self, job_id: str, spec: DrawingSpec) -> int:
        """Persist a spec version and return its version number."""
        ...

    def get_spec(self, job_id: str) -> DrawingSpec | None: ...

    def save_plan(self, plan: OperationPlan) -> None: ...

    def get_plan(self, job_id: str) -> OperationPlan | None: ...

    def save_remediation(self, *, job_id: str, plan_hash: str, payload: dict[str, Any]) -> None:
        """Persist the immutable selected-finding evidence for restart-safe re-audit."""
        ...

    def get_remediation(self, job_id: str) -> tuple[str, dict[str, Any]] | None:
        """Return ``(plan_hash, payload)`` for a remediation job, if present."""
        ...

    def save_validation(self, report: ValidationReport) -> None: ...

    def get_validation(self, job_id: str) -> ValidationReport | None: ...

    def record_execution(
        self, *, job_id: str, idempotency_key: str, request_digest: str, result: dict[str, Any]
    ) -> None: ...

    def find_execution(
        self, *, job_id: str, idempotency_key: str
    ) -> tuple[str, dict[str, Any]] | None:
        """Return ``(request_digest, result)`` for a previously seen key."""
        ...

    def map_entity(
        self,
        *,
        document_id: str,
        feature_id: str,
        operation_id: str,
        entity_ref: str,
        revision: str,
        expected_layer: str | None = None,
    ) -> None:
        """Record that ``entity_ref`` in the document came from ``operation_id``."""
        ...

    def entity_mappings_for(self, document_id: str) -> tuple[EntityMappingRecord, ...]:
        """Mappings for one document, in insertion order. Order is semantic."""
        ...

    def save_checkpoint(self, checkpoint: Checkpoint) -> None: ...

    def finalize_job(
        self,
        job: CadJob,
        *,
        lease_id: str,
        now: datetime,
        mappings: tuple[EntityMappingRecord, ...] = (),
    ) -> bool:
        """Persist a terminal/unknown state and mappings; return whether lease joined the UoW."""
        ...

    def finalize_commit(
        self,
        job: CadJob,
        *,
        lease_id: str,
        now: datetime,
        mappings: tuple[EntityMappingRecord, ...],
        idempotency_key: str,
        request_digest: str,
        result: dict[str, Any],
        checkpoint: Checkpoint | None = None,
    ) -> bool:
        """Atomically persist proven write output, checkpoint and writer-lease release."""
        ...


@runtime_checkable
class AuditSink(Protocol):
    """Append-only audit trail. Payloads must already be redacted."""

    def append(
        self,
        *,
        event_type: str,
        job_id: str | None,
        actor_type: str,
        actor_id: str,
        payload: dict[str, Any],
    ) -> str:
        """Return the new ``audit_event_id``."""
        ...


@runtime_checkable
class TakeoffReportStore(Protocol):
    """Append-only persistence for generated take-off reports."""

    def save_takeoff_report(
        self, *, report_id: str, report: TakeoffReport, total_mass_kg: float
    ) -> None: ...


class CancellationTokenPort(Protocol):
    """Minimal cooperative deadline surface accepted by infrastructure ports."""

    def checkpoint(self) -> None: ...

    def add_cancel_callback(self, callback: Callable[[], None]) -> None: ...

    def cancel(self) -> None: ...

    @property
    def cancelled(self) -> bool: ...

    @property
    def elapsed_seconds(self) -> float: ...

    @property
    def remaining_seconds(self) -> float: ...

    @property
    def timeout_seconds(self) -> float: ...

    @property
    def operation(self) -> str: ...


@runtime_checkable
class TakeoffPersistencePort(Protocol):
    """Atomically persist a take-off report and its mandatory audit event."""

    def persist_created(
        self,
        *,
        report_id: str,
        report: TakeoffReport,
        total_mass_kg: float,
        actor_id: str,
        deadline: CancellationTokenPort,
    ) -> str: ...


@runtime_checkable
class DrawingAuditStore(Protocol):
    """Append-only persistence for drawing audit evidence."""

    def save_drawing_audit(
        self,
        *,
        audit_id: str,
        document_id: str,
        revision: str,
        report: ValidationReport,
    ) -> None: ...

    def get_drawing_audit(self, audit_id: str) -> DrawingAuditEvidence | None: ...
