"""SQLite-backed implementation of the complete :class:`JobStore` port."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.models.approval import ApprovalRecord
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.job import CadJob
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.result import Checkpoint, EntityMappingRecord
from cad_harness.domain.models.validation import ValidationReport
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.persistence.models import (
    Approval,
    CheckpointRow,
    Document,
    EntityMapping,
    Execution,
    Job,
    Plan,
    RemediationSelectionRow,
    SpecVersion,
    Validation,
    WriterLeaseRow,
)
from cad_harness.persistence.retry import DEFAULT_SQLITE_RETRY, RetryPolicy

T = TypeVar("T")


def _utc(value: datetime) -> datetime:
    """Restore UTC lost by SQLite's timezone-naive datetime storage."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class SqlJobStore:
    """Persist every aggregate write in its own committed transaction."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        retry: RetryPolicy = DEFAULT_SQLITE_RETRY,
    ) -> None:
        self._session_factory = session_factory
        self._retry = retry

    def _write(self, action: Callable[[Session], T]) -> T:
        def attempt() -> T:
            with self._session_factory() as session:
                try:
                    result = action(session)
                    session.commit()
                    return result
                except Exception:
                    session.rollback()
                    raise

        return self._retry.run(attempt)

    def save_job(self, job: CadJob) -> None:
        payload = job.model_dump(mode="json")

        def action(session: Session) -> None:
            document = session.get(Document, job.document_id)
            if document is None:
                document = Document(
                    document_id=job.document_id,
                    path_hash=sha256_of({"document_id": job.document_id}),
                    current_revision=job.expected_revision,
                )
                session.add(document)
                session.flush()
            else:
                document.current_revision = job.expected_revision
                document.last_seen_at = datetime.now(UTC)

            row = session.get(Job, job.job_id)
            if row is None:
                session.add(
                    Job(
                        job_id=str(payload["job_id"]),
                        document_id=str(payload["document_id"]),
                        state=str(payload["state"]),
                        expected_revision=str(payload["expected_revision"]),
                        current_spec_version=int(payload["spec_version"]),
                        plan_hash=payload["plan_hash"],
                        created_at=job.created_at,
                        updated_at=job.updated_at,
                    )
                )
                return
            row.document_id = str(payload["document_id"])
            row.state = str(payload["state"])
            row.expected_revision = str(payload["expected_revision"])
            row.current_spec_version = int(payload["spec_version"])
            row.plan_hash = payload["plan_hash"]
            row.updated_at = job.updated_at

        self._write(action)

    def get_job(self, job_id: str) -> CadJob | None:
        with self._session_factory() as session:
            row = session.get(Job, job_id)
            if row is None:
                return None
            spec_row = session.scalar(
                select(SpecVersion)
                .where(SpecVersion.job_id == job_id)
                .order_by(SpecVersion.version.desc())
                .limit(1)
            )
            plan_row = None
            if row.plan_hash is not None:
                plan_row = session.scalar(
                    select(Plan).where(Plan.plan_hash == row.plan_hash).limit(1)
                )
            approval_row = session.scalar(
                select(Approval)
                .where(Approval.job_id == job_id, Approval.revoked.is_(False))
                .order_by(Approval.approved_at.desc(), Approval.approval_id.desc())
                .limit(1)
            )
            checkpoint_row = session.scalar(
                select(CheckpointRow)
                .where(CheckpointRow.job_id == job_id)
                .order_by(CheckpointRow.created_at.desc(), CheckpointRow.checkpoint_id.desc())
                .limit(1)
            )
            payload = {
                "schema_version": SCHEMA_VERSION,
                "job_id": row.job_id,
                "document_id": row.document_id,
                "expected_revision": row.expected_revision,
                "state": row.state,
                "spec_id": spec_row.normalized_json.get("spec_id") if spec_row else None,
                "spec_version": row.current_spec_version,
                "plan_id": plan_row.plan_id if plan_row else None,
                "plan_hash": row.plan_hash,
                "approval_id": approval_row.approval_id if approval_row else None,
                "checkpoint_id": checkpoint_row.checkpoint_id if checkpoint_row else None,
                "created_at": _utc(row.created_at),
                "updated_at": _utc(row.updated_at),
            }
            return CadJob.model_validate(payload)

    def save_spec(self, job_id: str, spec: DrawingSpec) -> int:
        payload = spec.model_dump(mode="json")

        def action(session: Session) -> int:
            latest = session.scalar(
                select(func.max(SpecVersion.version)).where(SpecVersion.job_id == job_id)
            )
            version = int(latest or 0) + 1
            session.add(
                SpecVersion(
                    spec_version_id=f"{spec.spec_id}:{version}",
                    job_id=job_id,
                    version=version,
                    schema_version=spec.schema_version,
                    normalized_json=payload,
                    content_hash=sha256_of(payload),
                    created_at=datetime.now(UTC),
                )
            )
            return version

        return self._write(action)

    def get_spec(self, job_id: str) -> DrawingSpec | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(SpecVersion)
                .where(SpecVersion.job_id == job_id)
                .order_by(SpecVersion.version.desc())
                .limit(1)
            )
            return DrawingSpec.model_validate(row.normalized_json) if row else None

    def save_plan(self, plan: OperationPlan) -> None:
        payload = plan.model_dump(mode="json")
        plan_hash = plan.plan_hash or plan.compute_hash()

        def action(session: Session) -> None:
            row = session.get(Plan, plan.plan_id)
            if row is None:
                session.add(
                    Plan(
                        plan_id=plan.plan_id,
                        job_id=plan.job_id,
                        schema_version=plan.schema_version,
                        plan_json=payload,
                        plan_hash=plan_hash,
                        created_at=datetime.now(UTC),
                    )
                )
                return
            row.plan_json = payload
            row.plan_hash = plan_hash
            row.schema_version = plan.schema_version

        self._write(action)

    def get_plan(self, job_id: str) -> OperationPlan | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(Plan)
                .where(Plan.job_id == job_id)
                .order_by(Plan.created_at.desc(), Plan.plan_id.desc())
                .limit(1)
            )
            return OperationPlan.model_validate(row.plan_json) if row else None

    def save_remediation(self, *, job_id: str, plan_hash: str, payload: dict[str, Any]) -> None:
        def action(session: Session) -> None:
            session.add(
                RemediationSelectionRow(
                    job_id=job_id,
                    plan_hash=plan_hash,
                    selection_json=dict(payload),
                    created_at=datetime.now(UTC),
                )
            )

        self._write(action)

    def get_remediation(self, job_id: str) -> tuple[str, dict[str, Any]] | None:
        with self._session_factory() as session:
            row = session.get(RemediationSelectionRow, job_id)
            if row is None:
                return None
            return row.plan_hash, dict(row.selection_json)

    def save_validation(self, report: ValidationReport) -> None:
        payload = report.model_dump(mode="json")

        def action(session: Session) -> None:
            row = session.get(Validation, report.validation_id)
            if row is None:
                session.add(
                    Validation(
                        validation_id=report.validation_id,
                        job_id=report.job_id,
                        stage=report.stage.value,
                        plan_hash=report.plan_hash,
                        report_json=payload,
                        blocking_count=report.blocking_count,
                        error_count=report.error_count,
                        created_at=datetime.now(UTC),
                    )
                )
                return
            row.report_json = payload
            row.blocking_count = report.blocking_count
            row.error_count = report.error_count

        self._write(action)

    def get_validation(self, job_id: str) -> ValidationReport | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(Validation)
                .where(Validation.job_id == job_id)
                .order_by(Validation.created_at.desc(), Validation.validation_id.desc())
                .limit(1)
            )
            return ValidationReport.model_validate(row.report_json) if row else None

    def save_approval(self, approval: ApprovalRecord) -> None:
        payload = approval.model_dump(mode="json")

        def action(session: Session) -> None:
            row = session.get(Approval, approval.approval_id)
            if row is None:
                session.add(
                    Approval(
                        approval_id=approval.approval_id,
                        job_id=approval.job_id,
                        plan_hash=approval.plan_hash,
                        expected_revision=approval.expected_revision,
                        approved_by=approval.approved_by,
                        approved_at=approval.approved_at,
                        expires_at=approval.expires_at,
                        revoked=False,
                        record_json=payload,
                    )
                )
                return
            row.record_json = payload
            row.revoked = False
            row.expires_at = approval.expires_at

        self._write(action)

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._session_factory() as session:
            row = session.get(Approval, approval_id)
            if row is None or row.revoked:
                return None
            return ApprovalRecord.model_validate(row.record_json)

    def revoke_approvals_for_job(self, job_id: str) -> None:
        def action(session: Session) -> None:
            session.execute(update(Approval).where(Approval.job_id == job_id).values(revoked=True))

        self._write(action)

    def record_execution(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        request_digest: str,
        result: dict[str, Any],
    ) -> None:
        def action(session: Session) -> None:
            row = session.scalar(
                select(Execution).where(
                    Execution.job_id == job_id,
                    Execution.idempotency_key == idempotency_key,
                )
            )
            status = str(result.get("status", "completed"))
            if row is None:
                session.add(
                    Execution(
                        execution_id=new_id(IdPrefix.EXECUTION),
                        job_id=job_id,
                        idempotency_key=idempotency_key,
                        request_digest=request_digest,
                        status=status,
                        result_json=result,
                        started_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    )
                )
                return
            row.request_digest = request_digest
            row.status = status
            row.result_json = result
            row.completed_at = datetime.now(UTC)

        self._write(action)

    def find_execution(
        self, *, job_id: str, idempotency_key: str
    ) -> tuple[str, dict[str, Any]] | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(Execution).where(
                    Execution.job_id == job_id,
                    Execution.idempotency_key == idempotency_key,
                )
            )
            if row is None or row.result_json is None:
                return None
            return row.request_digest, row.result_json

    def map_entity(
        self,
        *,
        document_id: str,
        feature_id: str,
        operation_id: str,
        entity_ref: str,
        revision: str,
    ) -> None:
        def action(session: Session) -> None:
            row = session.scalar(
                select(EntityMapping).where(
                    EntityMapping.document_id == document_id,
                    EntityMapping.entity_ref == entity_ref,
                )
            )
            if row is None:
                session.add(
                    EntityMapping(
                        document_id=document_id,
                        feature_id=feature_id,
                        operation_id=operation_id,
                        entity_ref=entity_ref,
                        last_revision=revision,
                    )
                )
                return
            row.feature_id = feature_id
            row.operation_id = operation_id
            row.last_revision = revision

        self._write(action)

    def entity_mappings_for(self, document_id: str) -> tuple[EntityMappingRecord, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(EntityMapping)
                .where(EntityMapping.document_id == document_id)
                .order_by(EntityMapping.id)
            ).all()
            return tuple(
                EntityMappingRecord(
                    document_id=row.document_id,
                    feature_id=row.feature_id,
                    operation_id=row.operation_id,
                    entity_ref=row.entity_ref,
                    last_revision=row.last_revision,
                )
                for row in rows
            )

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        def action(session: Session) -> None:
            row = session.get(CheckpointRow, checkpoint.checkpoint_id)
            if row is None:
                session.add(
                    CheckpointRow(
                        checkpoint_id=checkpoint.checkpoint_id,
                        job_id=checkpoint.job_id,
                        revision=checkpoint.revision,
                        artifact_ref=checkpoint.artifact_ref,
                        created_at=checkpoint.created_at,
                    )
                )
                return
            row.revision = checkpoint.revision
            row.artifact_ref = checkpoint.artifact_ref

        self._write(action)

    def finalize_job(
        self,
        job: CadJob,
        *,
        lease_id: str,
        now: datetime,
        mappings: tuple[EntityMappingRecord, ...] = (),
    ) -> bool:
        def action(session: Session) -> bool:
            self._save_terminal_job(session, job)
            for mapping in mappings:
                self._save_mapping(session, mapping)
            session.execute(delete(WriterLeaseRow).where(WriterLeaseRow.lease_id == lease_id))
            _ = now
            # True means lease deletion participated in this transaction; deletion is
            # intentionally idempotent when an expired row was already reclaimed.
            return True

        return self._write(action)

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
        def action(session: Session) -> bool:
            self._save_terminal_job(session, job)
            for mapping in mappings:
                self._save_mapping(session, mapping)
            if checkpoint is not None:
                self._save_checkpoint(session, checkpoint)
            session.add(
                Execution(
                    execution_id=new_id(IdPrefix.EXECUTION),
                    job_id=job.job_id,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    status=str(result.get("status", "completed")),
                    result_json=result,
                    started_at=now,
                    completed_at=now,
                )
            )
            session.execute(delete(WriterLeaseRow).where(WriterLeaseRow.lease_id == lease_id))
            return True

        return self._write(action)

    @staticmethod
    def _save_checkpoint(session: Session, checkpoint: Checkpoint) -> None:
        row = session.get(CheckpointRow, checkpoint.checkpoint_id)
        if row is None:
            session.add(
                CheckpointRow(
                    checkpoint_id=checkpoint.checkpoint_id,
                    job_id=checkpoint.job_id,
                    revision=checkpoint.revision,
                    artifact_ref=checkpoint.artifact_ref,
                    created_at=checkpoint.created_at,
                )
            )
            return
        row.revision = checkpoint.revision
        row.artifact_ref = checkpoint.artifact_ref

    @staticmethod
    def _save_terminal_job(session: Session, job: CadJob) -> None:
        row = session.get(Job, job.job_id)
        if row is None:
            raise RuntimeError("Cannot finalize a job that was not persisted")
        row.state = job.state.value
        row.expected_revision = job.expected_revision
        row.current_spec_version = job.spec_version
        row.plan_hash = job.plan_hash
        row.updated_at = job.updated_at
        document = session.get(Document, job.document_id)
        if document is not None:
            document.current_revision = job.expected_revision
            document.last_seen_at = job.updated_at

    @staticmethod
    def _save_mapping(session: Session, mapping: EntityMappingRecord) -> None:
        row = session.scalar(
            select(EntityMapping).where(
                EntityMapping.document_id == mapping.document_id,
                EntityMapping.entity_ref == mapping.entity_ref,
            )
        )
        if row is None:
            session.add(
                EntityMapping(
                    document_id=mapping.document_id,
                    feature_id=mapping.feature_id,
                    operation_id=mapping.operation_id,
                    entity_ref=mapping.entity_ref,
                    last_revision=mapping.last_revision,
                )
            )
            return
        row.feature_id = mapping.feature_id
        row.operation_id = mapping.operation_id
        row.last_revision = mapping.last_revision
