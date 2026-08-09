"""Commit results, entity mappings and checkpoints (architecture section 11.3)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel


class CommitStatus(StrEnum):
    COMMITTED = "committed"
    ABORTED = "aborted"
    #: Outcome could not be determined. Requires reconciliation, never a blind retry.
    UNKNOWN = "unknown"


class EntityResult(ContractModel):
    """What the adapter actually produced, plus the measurements read back."""

    operation_id: str
    feature_id: str
    entity_ref: str
    entity_type: str
    measurements: dict[str, Any] = Field(default_factory=dict)


class EntityMappingRecord(ContractModel):
    """Traceability from a committed entity back to the operation that created it.

    ``last_revision`` is the document revision the mapping was observed at, so a stale
    mapping can be detected instead of silently trusted.
    """

    document_id: str
    feature_id: str
    operation_id: str
    entity_ref: str
    last_revision: str


class Checkpoint(ContractModel):
    checkpoint_id: str
    job_id: str
    revision: str
    #: Reference into an allowlisted checkpoint directory, never a raw absolute path.
    artifact_ref: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CommitResult(ContractModel):
    schema_version: str = SCHEMA_VERSION
    job_id: str
    plan_hash: str
    status: CommitStatus
    entity_results: tuple[EntityResult, ...] = ()
    previous_revision: str
    new_revision: str
    checkpoint_id: str | None = None
    undo_group: str | None = None


class PreviewArtifact(ContractModel):
    kind: str  # dxf | svg | png | semantic_diff
    artifact_ref: str
    byte_size: int | None = None


class PreviewResult(ContractModel):
    schema_version: str = SCHEMA_VERSION
    preview_id: str
    job_id: str
    plan_hash: str
    artifacts: tuple[PreviewArtifact, ...] = ()
    #: Provenance label shown on every preview. False is the fail-safe default.
    company_approved: bool = False


class ExportResult(ContractModel):
    schema_version: str = SCHEMA_VERSION
    job_id: str | None = None
    document_id: str
    format: str
    artifact_ref: str
    byte_size: int | None = None


class RollbackResult(ContractModel):
    schema_version: str = SCHEMA_VERSION
    job_id: str
    restored_revision: str
    checkpoint_id: str | None = None
    #: ``undo_group`` is session-bound; ``checkpoint_restore`` requires a durable
    #: document-replacement backend and must never be inferred from a checkpoint id.
    method: Literal["undo_group", "checkpoint_restore"]
