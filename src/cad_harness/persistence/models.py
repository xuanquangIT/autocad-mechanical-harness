"""SQLAlchemy tables mirroring the logical schema in architecture section 18.

JSON columns hold already-validated contract documents. The relational columns exist
for the queries the workflow actually needs: find a job, find a plan by hash, detect a
reused idempotency key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    path_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    fingerprint: Mapped[str | None] = mapped_column(String(80))
    current_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Job(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("documents.document_id"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    current_spec_version: Mapped[int] = mapped_column(Integer, default=0)
    plan_hash: Mapped[str | None] = mapped_column(String(80), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SpecVersion(Base):
    __tablename__ = "spec_versions"
    __table_args__ = (UniqueConstraint("job_id", "version", name="uq_spec_job_version"),)

    spec_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.job_id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    normalized_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Plan(Base):
    __tablename__ = "plans"

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.job_id"), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # Unique: the same plan is never stored twice, which is what makes preview and
    # commit able to key off the hash alone.
    plan_hash: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Validation(Base):
    __tablename__ = "validations"

    validation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.job_id"), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_hash: Mapped[str | None] = mapped_column(String(80))
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    blocking_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Approval(Base):
    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.job_id"), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    expected_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (
        UniqueConstraint("job_id", "idempotency_key", name="uq_execution_idempotency"),
    )

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.job_id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Same key + different digest means the client reused a key: reject, do not replay.
    request_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EntityMapping(Base):
    __tablename__ = "entity_mappings"
    __table_args__ = (UniqueConstraint("document_id", "entity_ref", name="uq_entity_mapping_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    feature_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operation_id: Mapped[str] = mapped_column(String(160), nullable=False)
    entity_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    last_revision: Mapped[str] = mapped_column(String(80), nullable=False)


class CheckpointRow(Base):
    __tablename__ = "checkpoints"

    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.job_id"), nullable=False)
    revision: Mapped[str] = mapped_column(String(80), nullable=False)
    artifact_ref: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_redacted_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    previous_event_hash: Mapped[str | None] = mapped_column(String(80))
    event_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class WriterLeaseRow(Base):
    __tablename__ = "writer_leases"
    __table_args__ = (UniqueConstraint("document_id", name="uq_writer_lease_document"),)

    lease_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TakeoffReportRow(Base):
    __tablename__ = "takeoff_reports"

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision: Mapped[str] = mapped_column(String(80), nullable=False)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    total_mass_kg: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DrawingAuditRow(Base):
    __tablename__ = "drawing_audits"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision: Mapped[str] = mapped_column(String(80), nullable=False)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    blocking_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    info_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EffortRecordRow(Base):
    __tablename__ = "effort_records"
    __table_args__ = (UniqueConstraint("pilot_run_id", "case_id", name="uq_effort_run_case"),)

    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pilot_run_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("jobs.job_id"), nullable=False, index=True
    )
    harness_minutes: Mapped[float] = mapped_column(Float, nullable=False)
    idle_minutes_excluded: Mapped[float] = mapped_column(Float, nullable=False)
    manual_fixup_minutes: Mapped[float] = mapped_column(Float, nullable=False)
    spec_change_count: Mapped[int] = mapped_column(Integer, nullable=False)
    entities_created: Mapped[int] = mapped_column(Integer, nullable=False)
    entities_manually_edited: Mapped[int] = mapped_column(Integer, nullable=False)
    first_preview_clean: Mapped[bool] = mapped_column(Boolean, nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BaselineCaseRow(Base):
    __tablename__ = "baseline_cases"
    __table_args__ = (UniqueConstraint("pilot_run_id", "case_id", name="uq_baseline_run_case"),)

    baseline_record_id: Mapped[str] = mapped_column(String(260), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    pilot_run_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    capability_group: Mapped[str] = mapped_column(String(1), nullable=False)
    work_label: Mapped[str] = mapped_column(String(32), nullable=False)
    manual_minutes: Mapped[float] = mapped_column(Float, nullable=False)
    manual_measured_by: Mapped[str] = mapped_column(String(128), nullable=False)
    manual_measurement_biased: Mapped[bool] = mapped_column(Boolean, nullable=False)
    manual_measured_in_single_session: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OperationMetricRow(Base):
    __tablename__ = "operation_metrics"

    metric_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pilot_run_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operation_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
