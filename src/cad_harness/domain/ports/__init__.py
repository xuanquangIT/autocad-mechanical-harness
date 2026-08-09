"""Ports (interfaces) the domain depends on. Infrastructure implements them."""

from cad_harness.domain.ports.autocad_adapter import (
    AdapterCapability,
    AdapterStatus,
    AutoCADAdapter,
    CommitRequest,
    ExportRequest,
    InspectRequest,
    RollbackRequest,
    SelectionRequest,
)
from cad_harness.domain.ports.drawing_source import (
    DrawingReadRequest,
    DrawingSourcePort,
    DrawingSourceRef,
    ReadScope,
)
from cad_harness.domain.ports.lease_store import LeaseStore
from cad_harness.domain.ports.material_table import MaterialTablePort
from cad_harness.domain.ports.repositories import (
    ApprovalStore,
    AuditSink,
    DrawingAuditStore,
    JobStore,
    TakeoffPersistencePort,
    TakeoffReportStore,
)

__all__ = [
    "AdapterCapability",
    "AdapterStatus",
    "ApprovalStore",
    "AuditSink",
    "AutoCADAdapter",
    "CommitRequest",
    "DrawingAuditStore",
    "DrawingReadRequest",
    "DrawingSourcePort",
    "DrawingSourceRef",
    "ExportRequest",
    "InspectRequest",
    "JobStore",
    "LeaseStore",
    "MaterialTablePort",
    "ReadScope",
    "RollbackRequest",
    "SelectionRequest",
    "TakeoffPersistencePort",
    "TakeoffReportStore",
]
