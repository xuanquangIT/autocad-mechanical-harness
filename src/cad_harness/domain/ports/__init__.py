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
from cad_harness.domain.ports.repositories import ApprovalStore, AuditSink, JobStore

__all__ = [
    "AdapterCapability",
    "AdapterStatus",
    "ApprovalStore",
    "AuditSink",
    "AutoCADAdapter",
    "CommitRequest",
    "ExportRequest",
    "InspectRequest",
    "JobStore",
    "RollbackRequest",
    "SelectionRequest",
]
