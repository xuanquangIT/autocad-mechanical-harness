"""Versioned, immutable semantic model extracted from a drawing."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel
from cad_harness.domain.models.document import LayerInfo

PointTuple = tuple[float, float]
BoundingBoxTuple = tuple[float, float, float, float]


class ReadScope(ContractModel):
    """Explicit bounded region of a drawing; absence means summary-only."""

    kind: Literal["model_space", "selection", "layer", "layout"] = "model_space"
    layer_name: str | None = None
    layout_name: str | None = None
    entity_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _required_selector_is_present(self) -> ReadScope:
        if self.kind == "layer" and not self.layer_name:
            raise ValueError("layer scope requires layer_name")
        if self.kind == "layout" and not self.layout_name:
            raise ValueError("layout scope requires layout_name")
        if self.kind == "selection" and not self.entity_refs:
            raise ValueError("selection scope requires entity_refs")
        return self


class MeasuredValue(ContractModel):
    value: float
    unit: Literal["mm", "mm2", "deg", "count"]
    provenance: Literal["measured", "user_supplied"]
    entity_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _measured_values_have_sources(self) -> MeasuredValue:
        if self.provenance == "measured" and not self.entity_refs:
            raise ValueError("measured values require at least one source entity_ref")
        return self


class LineGeometry(ContractModel):
    kind: Literal["line"] = "line"
    start_mm: PointTuple
    end_mm: PointTuple


class PointGeometry(ContractModel):
    kind: Literal["point"] = "point"
    position_mm: PointTuple


class ArcGeometry(ContractModel):
    kind: Literal["arc"] = "arc"
    center_mm: PointTuple
    radius_mm: float
    start_angle_deg: float
    end_angle_deg: float


class CircleGeometry(ContractModel):
    kind: Literal["circle"] = "circle"
    center_mm: PointTuple
    radius_mm: float


class EllipseGeometry(ContractModel):
    kind: Literal["ellipse"] = "ellipse"
    center_mm: PointTuple
    major_axis_mm: float
    minor_axis_mm: float
    rotation_deg: float


class PolylineVertex(ContractModel):
    point_mm: PointTuple
    bulge: float = 0.0


class PolylineGeometry(ContractModel):
    kind: Literal["polyline"] = "polyline"
    vertices: tuple[PolylineVertex, ...]
    closed: bool


class TextGeometry(ContractModel):
    kind: Literal["text"] = "text"
    insertion_mm: PointTuple
    height_mm: float
    text_style: str
    content: str


class DimensionGeometry(ContractModel):
    kind: Literal["dimension"] = "dimension"
    dimension_type: str
    dimension_style: str
    measurement_mm: float | None
    text_override: str | None
    measured_entity_refs: tuple[str, ...] = ()


class HatchGeometry(ContractModel):
    kind: Literal["hatch"] = "hatch"
    pattern_name: str
    area_mm2: float | None
    boundary_entity_refs: tuple[str, ...] = ()


class BlockReferenceGeometry(ContractModel):
    kind: Literal["block_reference"] = "block_reference"
    block_name: str
    insertion_mm: PointTuple
    scale: tuple[float, float]
    rotation_deg: float
    non_uniform_scale: bool = False
    nested_depth_read: int = 0
    child_entities: tuple[EntityRecord, ...] = ()
    children_beyond_depth: int = 0


EntityGeometry = Annotated[
    PointGeometry
    | LineGeometry
    | ArcGeometry
    | CircleGeometry
    | EllipseGeometry
    | PolylineGeometry
    | TextGeometry
    | DimensionGeometry
    | HatchGeometry
    | BlockReferenceGeometry,
    Field(discriminator="kind"),
]


class EntityRecord(ContractModel):
    entity_ref: str
    entity_type: str
    layer: str
    visible: bool
    space: str
    geometry: EntityGeometry
    bounding_box_mm: BoundingBoxTuple
    non_uniform_scale: bool = False
    feature_id: str | None = None


class UnsupportedEntityCount(ContractModel):
    entity_type: str
    count: int = Field(ge=1)


class DrawingSummary(ContractModel):
    """Counts only; deliberately has no geometry or entity collection."""

    schema_version: str = SCHEMA_VERSION
    document_id: str
    revision: str
    counts_by_entity_type: dict[str, int]
    counts_by_layer: dict[str, int]
    counts_by_space: dict[str, int]
    unsupported: tuple[UnsupportedEntityCount, ...] = ()
    coverage_complete: bool = True

    @model_validator(mode="after")
    def _coverage_matches_unsupported_entities(self) -> DrawingSummary:
        if self.coverage_complete and self.unsupported:
            raise ValueError("coverage_complete cannot be true when unsupported entities exist")
        return self


class DrawingModel(ContractModel):
    schema_version: str = SCHEMA_VERSION
    document_id: str
    revision: str
    display_name: str
    source_unit_code: str
    to_mm_factor: float | None
    geometry_normalized: bool
    scope: ReadScope
    entities: tuple[EntityRecord, ...] = ()
    layers: tuple[LayerInfo, ...] = ()
    dimension_styles: tuple[str, ...] = ()
    text_styles: tuple[str, ...] = ()
    unsupported: tuple[UnsupportedEntityCount, ...] = ()
    coverage_complete: bool = True
    arc_chord_tolerance_mm: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _unit_normalization_is_explicit(self) -> DrawingModel:
        if self.to_mm_factor is None and self.geometry_normalized:
            raise ValueError("geometry_normalized must be false when to_mm_factor is unknown")
        if self.to_mm_factor is not None and self.to_mm_factor <= 0.0:
            raise ValueError("to_mm_factor must be positive")
        if self.coverage_complete and self.unsupported:
            raise ValueError("coverage_complete cannot be true when unsupported entities exist")
        return self
