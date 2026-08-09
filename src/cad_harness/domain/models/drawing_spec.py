"""DrawingSpec: normalized engineering intent (architecture section 11.1).

A spec describes *meaning*, never a sequence of AutoCAD commands. The LLM fills
this in; the geometry kernel derives coordinates from it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel
from cad_harness.domain.value_objects.units import Unit


class StandardProfileRef(ContractModel):
    profile_id: str
    version: str

    def as_ref(self) -> str:
        return f"{self.profile_id}@{self.version}"


class Datum(ContractModel):
    """Placement origin. Required whenever placement affects geometry."""

    type: Literal["point", "selected_point", "named_datum"]
    point_mm: tuple[float, float] | None = None
    name: str | None = None


class DefaultRecord(ContractModel):
    """Provenance for an applied default (architecture section 12.2).

    A default without a source and version is a silent default, which the
    invariant principles forbid.
    """

    path: str
    value: Any
    source: str
    source_version: str
    reason: str
    impact: str
    override_allowed: bool = True


class Assumption(ContractModel):
    """An interpretation the system made that a human must confirm."""

    path: str
    statement: str
    affects_geometry: bool
    requires_approval: bool = True


class MissingInput(ContractModel):
    """A required engineering input the caller must supply before compiling."""

    path: str
    reason: str
    accepted_formats: tuple[str, ...] = ()


class ModifierSpec(ContractModel):
    """Ordered outline transformation containing engineering inputs only."""

    type: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_intermediate_coordinates(self) -> ModifierSpec:
        forbidden = {"point", "points", "coordinate", "coordinates", "vertex", "vertices"}
        invalid = sorted(
            key
            for key in self.parameters
            if any(token in forbidden for token in key.lower().replace("-", "_").split("_"))
            and key.lower() != "vertex_indices"
        )
        if invalid:
            raise ValueError(
                "Modifier parameters cannot contain intermediate coordinate keys: "
                + ", ".join(invalid)
            )
        return self


class FeatureSpec(ContractModel):
    """One mechanical feature with typed parameters and optional child features."""

    feature_id: str
    type: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    modifiers: tuple[ModifierSpec, ...] = ()
    children: tuple[FeatureSpec, ...] = ()


class ViewSpec(ContractModel):
    """One requested view; supported values are validated by the view compiler."""

    type: str
    name: str | None = None


class DatumFeatureSymbol(ContractModel):
    """An explicitly declared GD&T datum symbol; never inferred."""

    identifier: str
    feature_id: str
    position_mm: tuple[float, float]


class FeatureControlFrame(ContractModel):
    """An explicitly declared note-level feature control frame."""

    frame_id: str
    feature_id: str
    characteristic: str
    tolerance_text: str
    datum_references: tuple[str, ...] = ()
    position_mm: tuple[float, float]


class DrawingIntent(ContractModel):
    projection: Literal["orthographic", "isometric"] = "orthographic"
    view: Literal["top", "front", "side", "section"] = "top"
    views: tuple[ViewSpec, ...] = ()
    datum: Datum | None = None


class Annotations(ContractModel):
    general_tolerance: str | None = None
    dimensions: Literal["auto_required", "auto_optional", "none"] = "none"
    title_block: str | None = None
    title_block_values: dict[str, str] = Field(default_factory=dict)
    dimension_style: str | None = None
    datum_symbols: tuple[DatumFeatureSymbol, ...] = ()
    feature_control_frames: tuple[FeatureControlFrame, ...] = ()


class DrawingSpec(ContractModel):
    """Root normalized requirement submitted through ``cad_spec_submit``."""

    schema_version: str = SCHEMA_VERSION
    spec_id: str
    document_id: str
    units: Unit = Unit.MM
    standard_profile: StandardProfileRef
    drawing: DrawingIntent = Field(default_factory=DrawingIntent)
    features: tuple[FeatureSpec, ...] = ()
    annotations: Annotations = Field(default_factory=Annotations)
    assumptions: tuple[Assumption, ...] = ()
    explicit_defaults: tuple[DefaultRecord, ...] = ()


FeatureSpec.model_rebuild()
ModifierSpec.model_rebuild()
