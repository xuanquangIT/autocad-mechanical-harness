"""Persistence and audit ports. SQLite/SQLAlchemy implementations live in persistence/."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from cad_harness.domain.models.approval import ApprovalRecord
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.job import CadJob
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.validation import ValidationReport


@runtime_checkable
class JobStore(Protocol):
    """Job aggregate persistence, including idempotency bookkeeping."""

    def save_job(self, job: CadJob) -> None: ...

    def get_job(self, job_id: str) -> CadJob | None: ...

    def save_spec(self, job_id: str, spec: DrawingSpec) -> int:
        """Persist a spec version and return its version number."""
        ...

    def get_spec(self, job_id: str) -> DrawingSpec | None: ...

    def save_plan(self, plan: OperationPlan) -> None: ...

    def get_plan(self, job_id: str) -> OperationPlan | None: ...

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


@runtime_checkable
class ApprovalStore(Protocol):
    def save_approval(self, approval: ApprovalRecord) -> None: ...

    def get_approval(self, approval_id: str) -> ApprovalRecord | None: ...

    def revoke_approvals_for_job(self, job_id: str) -> None: ...


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
