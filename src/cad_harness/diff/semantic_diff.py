"""Semantic diff generation (architecture section 7.6).

The diff describes *what will change in engineering terms*, not pixels. An engineer
approves this, and the same plan hash it carries is what commit verifies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cad_harness.domain.models.document import DocumentSnapshot
from cad_harness.domain.models.operation_plan import OperationPlan, OperationType

#: Operation types that add geometry rather than modify or remove it.
_CREATE_TYPES = frozenset(
    {
        OperationType.CREATE_LINE,
        OperationType.CREATE_POLYLINE,
        OperationType.CREATE_CLOSED_POLYLINE,
        OperationType.CREATE_CIRCLE,
        OperationType.CREATE_CIRCLES,
        OperationType.CREATE_ARC,
        OperationType.CREATE_TEXT,
        OperationType.CREATE_CENTERLINE,
        OperationType.CREATE_CENTERMARK,
        OperationType.CREATE_LINEAR_DIMENSION,
        OperationType.CREATE_ALIGNED_DIMENSION,
        OperationType.CREATE_DIAMETER_DIMENSION,
        OperationType.CREATE_RADIUS_DIMENSION,
    }
)


@dataclass(slots=True)
class DiffEntry:
    change: str  # added | modified | deleted
    feature_id: str
    operation_id: str
    entity_type: str
    layer: str
    summary: str
    measurements: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SemanticDiff:
    plan_hash: str
    document_id: str
    from_revision: str
    entries: list[DiffEntry] = field(default_factory=list)

    @property
    def added_count(self) -> int:
        return sum(1 for e in self.entries if e.change == "added")

    @property
    def modified_count(self) -> int:
        return sum(1 for e in self.entries if e.change == "modified")

    @property
    def deleted_count(self) -> int:
        return sum(1 for e in self.entries if e.change == "deleted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_hash": self.plan_hash,
            "document_id": self.document_id,
            "from_revision": self.from_revision,
            "summary": {
                "added": self.added_count,
                "modified": self.modified_count,
                "deleted": self.deleted_count,
            },
            "entries": [
                {
                    "change": e.change,
                    "feature_id": e.feature_id,
                    "operation_id": e.operation_id,
                    "entity_type": e.entity_type,
                    "layer": e.layer,
                    "summary": e.summary,
                    "measurements": e.measurements,
                }
                for e in self.entries
            ],
        }


def _summarize(operation_type: OperationType, geometry: dict[str, Any]) -> str:
    """One-line, human-readable description of what an operation does."""
    if operation_type is OperationType.CREATE_CIRCLES:
        count = len(geometry.get("centers_mm", []))
        diameter = geometry.get("diameter_mm")
        return f"{count} hole(s) of diameter {diameter} mm"
    if operation_type in {OperationType.CREATE_CLOSED_POLYLINE, OperationType.CREATE_POLYLINE}:
        vertices = geometry.get("vertices_mm", [])
        closed = operation_type is OperationType.CREATE_CLOSED_POLYLINE
        return f"{'Closed' if closed else 'Open'} polyline with {len(vertices)} vertices"
    if operation_type is OperationType.CREATE_CENTERMARK:
        return "Centermark"
    return operation_type.value.replace("_", " ").capitalize()


def build_semantic_diff(plan: OperationPlan, snapshot: DocumentSnapshot) -> SemanticDiff:
    """Build the diff for ``plan`` against ``snapshot``.

    Colour convention for the rendered preview: green = added, yellow = modified,
    red = deleted, purple = standard violation.
    """
    from cad_harness.adapters.base import ENTITY_TYPE_BY_OPERATION

    diff = SemanticDiff(
        plan_hash=plan.plan_hash or plan.compute_hash(),
        document_id=snapshot.document_id,
        from_revision=snapshot.revision,
    )
    for operation in plan.operations:
        if operation.type in _CREATE_TYPES:
            change = "added"
        elif operation.type is OperationType.DELETE_ENTITY:
            change = "deleted"
        else:
            change = "modified"
        diff.entries.append(
            DiffEntry(
                change=change,
                feature_id=operation.feature_id,
                operation_id=operation.operation_id,
                entity_type=ENTITY_TYPE_BY_OPERATION.get(operation.type, "AcDbEntity"),
                layer=operation.layer,
                summary=_summarize(operation.type, operation.geometry),
                measurements=dict(operation.expected),
            )
        )
    return diff
