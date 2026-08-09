"""OperationPlan: the deterministic instruction list handed to an adapter.

Adapters translate operations into CAD entities. They never decide geometry, layers
or tolerances - those are already resolved here (architecture section 11.2).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from cad_harness.domain.canonical import compute_plan_hash
from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel
from cad_harness.domain.value_objects.units import Unit


class OperationType(StrEnum):
    """Adapter-agnostic operation vocabulary.

    Adding a member requires a mapping (or an explicit capability gap) in every
    adapter, plus a golden case. See the Definition of Done, section 29.
    """

    CREATE_LINE = "create_line"
    CREATE_POLYLINE = "create_polyline"
    CREATE_CLOSED_POLYLINE = "create_closed_polyline"
    CREATE_CIRCLE = "create_circle"
    CREATE_CIRCLES = "create_circles"
    CREATE_ARC = "create_arc"
    CREATE_TEXT = "create_text"
    CREATE_CENTERLINE = "create_centerline"
    CREATE_CENTERMARK = "create_centermark"
    CREATE_LINEAR_DIMENSION = "create_linear_dimension"
    CREATE_ALIGNED_DIMENSION = "create_aligned_dimension"
    CREATE_DIAMETER_DIMENSION = "create_diameter_dimension"
    CREATE_RADIUS_DIMENSION = "create_radius_dimension"
    CREATE_ANGULAR_DIMENSION = "create_angular_dimension"
    CREATE_HATCH = "create_hatch"
    UPDATE_ENTITY = "update_entity"
    DELETE_ENTITY = "delete_entity"


class Operation(ContractModel):
    """A single deterministic instruction.

    ``expected`` carries the measurements the adapter result is checked against
    after commit. An operation without expectations cannot be post-validated.
    """

    operation_id: str
    feature_id: str
    type: OperationType
    layer: str
    geometry: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    #: Present only for update/delete operations.
    target_entity_ref: str | None = None


class ValidationExpectation(ContractModel):
    """A measurable claim the validation engine must confirm."""

    rule_id: str
    feature_id: str | None = None
    operation_id: str | None = None
    expected: dict[str, Any] = Field(default_factory=dict)


class OperationPlan(ContractModel):
    """Compiled, hashable plan. Preview, approval and commit all key off ``plan_hash``."""

    schema_version: str = SCHEMA_VERSION
    plan_id: str
    job_id: str
    document_id: str
    expected_revision: str
    canonical_units: Unit = Unit.MM
    profile_ref: str
    operations: tuple[Operation, ...] = ()
    validation_expectations: tuple[ValidationExpectation, ...] = ()
    #: Filled by :meth:`with_hash`. Excluded from its own hash input.
    plan_hash: str | None = None

    def compute_hash(self) -> str:
        # Hash the exact JSON-mode wire shape.  In particular, optional fields such as
        # target_entity_ref are emitted as explicit nulls by model_dump at the IPC
        # boundary; omitting them here would make Python approvals unverifiable by the
        # C# bridge even though both peers received the same OperationPlan.
        return compute_plan_hash(self.model_dump(mode="json"))

    def with_hash(self) -> OperationPlan:
        """Return a copy carrying its deterministic hash."""
        return self.model_copy(update={"plan_hash": self.compute_hash()})

    def verify_hash(self, expected: str) -> bool:
        return self.compute_hash() == expected
