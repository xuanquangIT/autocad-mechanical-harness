"""Contract shared by deterministic outline modifiers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from cad_harness.domain.models.drawing_spec import ModifierSpec
from cad_harness.domain.models.operation_plan import ValidationExpectation
from cad_harness.feature_catalog.base import CompileContext, InputReport
from cad_harness.geometry.curves import CurveParams
from cad_harness.geometry.primitives import Point2D, Polyline2D


@dataclass(frozen=True, slots=True)
class ReplacedCorner:
    vertex_index: int
    original_vertex: Point2D
    replacement_points: tuple[Point2D, ...]
    curve: CurveParams | None = None


@dataclass(frozen=True, slots=True)
class ModifiedOutline:
    feature_id: str
    outline: Polyline2D
    replacements: tuple[ReplacedCorner, ...]
    expectations: tuple[ValidationExpectation, ...]


def modifier_feature_id(parent_feature_id: str, modifier_type: str, index: int) -> str:
    return f"feature:{parent_feature_id}:mod:{modifier_type}:{index}"


@runtime_checkable
class OutlineModifier(Protocol):
    modifier_type: str
    schema_version: str

    def validate_inputs(
        self, spec: ModifierSpec, outline: Polyline2D, context: CompileContext
    ) -> InputReport: ...

    def apply(
        self,
        spec: ModifierSpec,
        outline: Polyline2D,
        context: CompileContext,
        *,
        modifier_index: int,
    ) -> ModifiedOutline: ...
