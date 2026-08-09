"""The single port through which the harness touches a CAD backend.

Every adapter (fake, DXF preview, COM, .NET bridge) implements this protocol. The
application layer knows nothing else about AutoCAD (architecture section 7.8).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from cad_harness.domain.models.base import ContractModel
from cad_harness.domain.models.document import DocumentSnapshot, SelectionSnapshot
from cad_harness.domain.models.operation_plan import OperationPlan, OperationType
from cad_harness.domain.models.result import (
    CommitResult,
    ExportResult,
    PreviewResult,
    RollbackResult,
)


class AdapterCapability(StrEnum):
    """Declared, queryable capabilities.

    Contract tests assert behaviour *per capability* rather than forcing every
    adapter to support everything (architecture section 22.5).
    """

    INSPECT_DOCUMENT = "inspect_document"
    INSPECT_SELECTION = "inspect_selection"
    PREVIEW = "preview"
    COMMIT = "commit"
    EXPORT = "export"
    #: True transactional atomicity. COM cannot honour this; the C# bridge can.
    ATOMIC_TRANSACTION = "atomic_transaction"
    DOCUMENT_LOCK = "document_lock"
    UNDO_GROUP = "undo_group"
    #: Feature ids persisted in drawing metadata (XData / extension dictionary).
    STABLE_METADATA = "stable_metadata"
    #: Reverts the immediately preceding harness write through a verified,
    #: session-bound AutoCAD undo group.  This is deliberately distinct from
    #: restoring a persisted DWG checkpoint.
    ROLLBACK_UNDO_GROUP = "rollback_undo_group"
    CHECKPOINT_RESTORE = "checkpoint_restore"
    IN_VIEWPORT_PREVIEW = "in_viewport_preview"


class AdapterStatus(ContractModel):
    adapter_type: str
    available: bool
    capabilities: tuple[AdapterCapability, ...] = ()
    cad_application: str | None = None
    cad_version: str | None = None
    #: Whether ``cad_version`` sits inside the published compatibility matrix.
    #: ``None`` means the check has not been made - an adapter that never talked to
    #: CAD must not claim a version is supported (Requirement 28.2).
    version_supported: bool | None = None
    active_document_id: str | None = None
    message: str | None = None


class InspectRequest(ContractModel):
    document_id: str | None = None
    include_layers: bool = True
    include_styles: bool = True


class SelectionRequest(ContractModel):
    document_id: str
    #: Hard cap so a stray selection cannot dump the whole database into context.
    max_entities: int = 200


class CommitRequest(ContractModel):
    plan: OperationPlan
    idempotency_key: str
    expected_revision: str
    approval_token: str
    #: Adapters that support checkpoints snapshot the document before writing.
    create_checkpoint: bool = True


class RollbackRequest(ContractModel):
    job_id: str
    document_id: str
    checkpoint_id: str
    current_revision: str
    rollback_approval_token: str
    undo_group: str | None = None


class ExportRequest(ContractModel):
    document_id: str
    format: str  # dwg | dxf | pdf
    #: Resolved against the configured allowlist before the adapter is called.
    target_path: str
    overwrite: bool = False


@runtime_checkable
class AutoCADAdapter(Protocol):
    """Execution port. Implementations contain no business rules."""

    supported_operations: frozenset[OperationType]

    def unsupported_operations(self, plan: OperationPlan) -> tuple[OperationType, ...]: ...

    def status(self) -> AdapterStatus: ...

    def inspect_document(self, request: InspectRequest) -> DocumentSnapshot: ...

    def inspect_selection(self, request: SelectionRequest) -> SelectionSnapshot: ...

    def preview(self, plan: OperationPlan) -> PreviewResult: ...

    def validate_revision(self, document_id: str, expected_revision: str) -> bool: ...

    def commit(self, request: CommitRequest) -> CommitResult: ...

    def rollback(self, request: RollbackRequest) -> RollbackResult: ...

    def export(self, request: ExportRequest) -> ExportResult: ...
