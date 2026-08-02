"""Shared adapter scaffolding.

Adapters are thin. They map operations to CAD calls and read measurements back. Any
business decision found in an adapter is a layering bug.
"""

from __future__ import annotations

from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.errors import AdapterCapabilityMissingError
from cad_harness.domain.models.document import DocumentSnapshot, SelectionSnapshot
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.result import (
    CommitResult,
    ExportResult,
    PreviewResult,
    RollbackResult,
)
from cad_harness.domain.ports.autocad_adapter import (
    AdapterCapability,
    AdapterStatus,
    CommitRequest,
    ExportRequest,
    InspectRequest,
    RollbackRequest,
    SelectionRequest,
)

#: Entity type each operation is expected to produce, for post-commit checks.
ENTITY_TYPE_BY_OPERATION: dict[OperationType, str] = {
    OperationType.CREATE_LINE: "AcDbLine",
    OperationType.CREATE_POLYLINE: "AcDbPolyline",
    OperationType.CREATE_CLOSED_POLYLINE: "AcDbPolyline",
    OperationType.CREATE_CIRCLE: "AcDbCircle",
    OperationType.CREATE_CIRCLES: "AcDbCircle",
    OperationType.CREATE_ARC: "AcDbArc",
    OperationType.CREATE_TEXT: "AcDbText",
    OperationType.CREATE_CENTERLINE: "AcDbLine",
    OperationType.CREATE_CENTERMARK: "AcDbPoint",
    OperationType.CREATE_LINEAR_DIMENSION: "AcDbRotatedDimension",
    OperationType.CREATE_ALIGNED_DIMENSION: "AcDbAlignedDimension",
    OperationType.CREATE_DIAMETER_DIMENSION: "AcDbDiametricDimension",
    OperationType.CREATE_RADIUS_DIMENSION: "AcDbRadialDimension",
}


class BaseAdapter:
    """Common behaviour: capability gating and deterministic revision hashing."""

    adapter_type: str = "base"
    capabilities: frozenset[AdapterCapability] = frozenset()

    def supports(self, capability: AdapterCapability) -> bool:
        return capability in self.capabilities

    def require(self, capability: AdapterCapability) -> None:
        """Fail fast and explicitly rather than half-performing an operation."""
        if not self.supports(capability):
            raise AdapterCapabilityMissingError(
                f"Adapter '{self.adapter_type}' does not support '{capability.value}'",
                required_action="Switch to an adapter that declares this capability",
                details={
                    "adapter_type": self.adapter_type,
                    "missing_capability": capability.value,
                    "declared": sorted(c.value for c in self.capabilities),
                },
            )

    def status(self) -> AdapterStatus:
        return AdapterStatus(
            adapter_type=self.adapter_type,
            available=True,
            capabilities=tuple(sorted(self.capabilities, key=lambda c: c.value)),
        )

    @staticmethod
    def entity_type_for(operation: Operation) -> str:
        return ENTITY_TYPE_BY_OPERATION.get(operation.type, "AcDbEntity")

    @staticmethod
    def revision_from(document_id: str, fingerprint_parts: list[object]) -> str:
        """Deterministic revision fingerprint (architecture section 13.2)."""
        return sha256_of({"document_id": document_id, "parts": fingerprint_parts})

    # ----------------------------------------------------------------- #
    # Port methods. Subclasses override what their capabilities declare.
    # ----------------------------------------------------------------- #

    def inspect_document(self, request: InspectRequest) -> DocumentSnapshot:
        self.require(AdapterCapability.INSPECT_DOCUMENT)
        raise NotImplementedError

    def inspect_selection(self, request: SelectionRequest) -> SelectionSnapshot:
        self.require(AdapterCapability.INSPECT_SELECTION)
        raise NotImplementedError

    def preview(self, plan: OperationPlan) -> PreviewResult:
        self.require(AdapterCapability.PREVIEW)
        raise NotImplementedError

    def validate_revision(self, document_id: str, expected_revision: str) -> bool:
        raise NotImplementedError

    def commit(self, request: CommitRequest) -> CommitResult:
        self.require(AdapterCapability.COMMIT)
        raise NotImplementedError

    def rollback(self, request: RollbackRequest) -> RollbackResult:
        self.require(AdapterCapability.CHECKPOINT_RESTORE)
        raise NotImplementedError

    def export(self, request: ExportRequest) -> ExportResult:
        self.require(AdapterCapability.EXPORT)
        raise NotImplementedError
