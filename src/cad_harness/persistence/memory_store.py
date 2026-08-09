"""In-memory JobStore.

Used by tests and by the default development configuration so the whole workflow runs
before the SQLite repository lands. It implements the same port, so swapping it out is
a wiring change only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cad_harness.domain.models.approval import ApprovalRecord
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.job import CadJob
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.result import Checkpoint, EntityMappingRecord
from cad_harness.domain.models.validation import ValidationReport


@dataclass(slots=True)
class InMemoryJobStore:
    jobs: dict[str, CadJob] = field(default_factory=dict)
    specs: dict[str, list[DrawingSpec]] = field(default_factory=dict)
    plans: dict[str, OperationPlan] = field(default_factory=dict)
    validations: dict[str, ValidationReport] = field(default_factory=dict)
    approvals: dict[str, ApprovalRecord] = field(default_factory=dict)
    #: (job_id, idempotency_key) -> (request_digest, result)
    executions: dict[tuple[str, str], tuple[str, dict[str, Any]]] = field(default_factory=dict)
    #: Append-only; insertion order is the commit order and is semantic.
    entity_mappings: list[EntityMappingRecord] = field(default_factory=list)
    checkpoints: dict[str, Checkpoint] = field(default_factory=dict)

    # ----------------------------- jobs ----------------------------- #

    def save_job(self, job: CadJob) -> None:
        self.jobs[job.job_id] = job

    def get_job(self, job_id: str) -> CadJob | None:
        return self.jobs.get(job_id)

    # ----------------------------- specs ---------------------------- #

    def save_spec(self, job_id: str, spec: DrawingSpec) -> int:
        versions = self.specs.setdefault(job_id, [])
        versions.append(spec)
        return len(versions)

    def get_spec(self, job_id: str) -> DrawingSpec | None:
        versions = self.specs.get(job_id)
        return versions[-1] if versions else None

    # ----------------------------- plans ---------------------------- #

    def save_plan(self, plan: OperationPlan) -> None:
        self.plans[plan.job_id] = plan

    def get_plan(self, job_id: str) -> OperationPlan | None:
        return self.plans.get(job_id)

    # -------------------------- validations ------------------------- #

    def save_validation(self, report: ValidationReport) -> None:
        self.validations[report.job_id] = report

    def get_validation(self, job_id: str) -> ValidationReport | None:
        return self.validations.get(job_id)

    # --------------------------- approvals -------------------------- #

    def save_approval(self, approval: ApprovalRecord) -> None:
        self.approvals[approval.approval_id] = approval

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        return self.approvals.get(approval_id)

    def revoke_approvals_for_job(self, job_id: str) -> None:
        for approval_id in [a.approval_id for a in self.approvals.values() if a.job_id == job_id]:
            self.approvals.pop(approval_id, None)

    # -------------------------- executions -------------------------- #

    def record_execution(
        self, *, job_id: str, idempotency_key: str, request_digest: str, result: dict[str, Any]
    ) -> None:
        self.executions[(job_id, idempotency_key)] = (request_digest, result)

    def find_execution(
        self, *, job_id: str, idempotency_key: str
    ) -> tuple[str, dict[str, Any]] | None:
        return self.executions.get((job_id, idempotency_key))

    # ------------------------ entity mappings ----------------------- #

    def map_entity(
        self,
        *,
        document_id: str,
        feature_id: str,
        operation_id: str,
        entity_ref: str,
        revision: str,
    ) -> None:
        self.entity_mappings.append(
            EntityMappingRecord(
                document_id=document_id,
                feature_id=feature_id,
                operation_id=operation_id,
                entity_ref=entity_ref,
                last_revision=revision,
            )
        )

    def entity_mappings_for(self, document_id: str) -> tuple[EntityMappingRecord, ...]:
        return tuple(m for m in self.entity_mappings if m.document_id == document_id)

    # -------------------------- checkpoints ------------------------- #

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self.checkpoints[checkpoint.checkpoint_id] = checkpoint

    # ---------------------- commit finalization --------------------- #

    def finalize_job(
        self,
        job: CadJob,
        *,
        lease_id: str,
        now: datetime,
        mappings: tuple[EntityMappingRecord, ...] = (),
    ) -> bool:
        del lease_id, now
        self.save_job(job)
        for mapping in mappings:
            self.entity_mappings.append(mapping)
        # A separate in-memory lease store has no shared transaction. Returning False
        # lets LeaseService release in its context-manager cleanup.
        return False

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
        self.finalize_job(job, lease_id=lease_id, now=now, mappings=mappings)
        if checkpoint is not None:
            self.save_checkpoint(checkpoint)
        self.record_execution(
            job_id=job.job_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            result=result,
        )
        return False
