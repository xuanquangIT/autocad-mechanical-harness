"""Read-only measurement contracts and supported source geometry kinds."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel


class MeasurementKind(StrEnum):
    """The twelve analytic measurements supported by the read path."""

    POINT_TO_POINT = "point_to_point"
    POINT_TO_ENTITY = "point_to_entity"
    ENTITY_TO_ENTITY = "entity_to_entity"
    ANGLE_BETWEEN_LINES = "angle_between_lines"
    ARC_LENGTH = "arc_length"
    CONTOUR_PERIMETER = "contour_perimeter"
    CONTOUR_AREA = "contour_area"
    DIAMETER = "diameter"
    RADIUS = "radius"
    BOUNDING_BOX = "bounding_box"
    HOLE_TO_EDGE = "hole_to_edge"
    HOLE_CENTER_TO_CENTER = "hole_center_to_center"


#: Single source of truth for request validation and unsupported-kind error payloads.
SUPPORTED_ENTITY_TYPES: dict[MeasurementKind, frozenset[str]] = {
    MeasurementKind.POINT_TO_POINT: frozenset(),
    MeasurementKind.POINT_TO_ENTITY: frozenset({"line", "arc", "circle", "polyline"}),
    MeasurementKind.ENTITY_TO_ENTITY: frozenset({"line", "arc", "circle", "polyline"}),
    MeasurementKind.ANGLE_BETWEEN_LINES: frozenset({"line"}),
    MeasurementKind.ARC_LENGTH: frozenset({"arc"}),
    MeasurementKind.CONTOUR_PERIMETER: frozenset({"circle", "polyline"}),
    MeasurementKind.CONTOUR_AREA: frozenset({"circle", "polyline"}),
    MeasurementKind.DIAMETER: frozenset({"arc", "circle"}),
    MeasurementKind.RADIUS: frozenset({"arc", "circle"}),
    MeasurementKind.BOUNDING_BOX: frozenset({"line", "arc", "circle", "polyline"}),
    MeasurementKind.HOLE_TO_EDGE: frozenset({"circle", "polyline"}),
    MeasurementKind.HOLE_CENTER_TO_CENTER: frozenset({"circle"}),
}


class MeasurementRequest(ContractModel):
    """A read-only measurement request resolved against one DrawingModel."""

    kind: MeasurementKind
    entity_refs: tuple[str, ...] = ()
    first_point_mm: tuple[float, float] | None = None
    second_point_mm: tuple[float, float] | None = None


class MeasurementResult(ContractModel):
    """One measured quantity together with its source revision and basis."""

    schema_version: str = SCHEMA_VERSION
    kind: MeasurementKind
    value: float | tuple[float, float, float, float]
    unit: Literal["mm", "mm2", "deg"]
    tolerance_used: float
    document_id: str
    revision: str
    measurement_basis: tuple[str, ...] = Field(min_length=1)
    entity_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _entity_measurements_keep_sources(self) -> MeasurementResult:
        if self.kind is not MeasurementKind.POINT_TO_POINT and not self.entity_refs:
            raise ValueError("entity-based measurements require at least one entity_ref")
        if self.kind is MeasurementKind.BOUNDING_BOX:
            if not isinstance(self.value, tuple) or len(self.value) != 4:
                raise ValueError("bounding_box value must contain min_x, min_y, max_x, max_y")
        elif isinstance(self.value, tuple):
            raise ValueError("only bounding_box measurements may return a tuple value")
        return self


__all__ = ["SUPPORTED_ENTITY_TYPES", "MeasurementKind", "MeasurementRequest", "MeasurementResult"]
