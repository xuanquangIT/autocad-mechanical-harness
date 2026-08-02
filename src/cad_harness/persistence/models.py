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
