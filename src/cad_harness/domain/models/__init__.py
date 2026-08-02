"""Pydantic models forming the versioned contracts of the harness."""

from cad_harness.domain.models.approval import ApprovalRecord
from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel
from cad_harness.domain.models.document import DocumentSnapshot, SelectionSnapshot
from cad_harness.domain.models.drawing_spec import (
    Datum,
    DefaultRecord,
    DrawingSpec,
    FeatureSpec,
    MissingInput,
    StandardProfileRef,
)
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus
from cad_harness.domain.models.job import CadJob, JobState, assert_transition
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.result import Checkpoint, CommitResult, EntityResult
from cad_harness.domain.models.validation import (
    Finding,
    Severity,
    ValidationReport,
    ValidationStage,
)

__all__ = [
    "SCHEMA_VERSION",
    "ApprovalRecord",
    "CadJob",
    "Checkpoint",
    "CommitResult",
    "ContractModel",
    "Datum",
    "DefaultRecord",
    "DocumentSnapshot",
    "DrawingSpec",
    "EntityResult",
    "FeatureSpec",
    "Finding",
    "JobState",
    "MissingInput",
    "Operation",
    "OperationPlan",
    "OperationType",
    "SelectionSnapshot",
    "Severity",
    "StandardProfileRef",
    "ToolResponse",
    "ToolStatus",
    "ValidationReport",
    "ValidationStage",
    "assert_transition",
]
