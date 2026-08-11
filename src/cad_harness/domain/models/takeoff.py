"""Versioned read-only contracts for material take-off."""

from __future__ import annotations

from pydantic import Field, field_validator

from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel
from cad_harness.domain.models.validation import Finding


class PartInput(ContractModel):
    """User-supplied part data; the engine validates required ranges explicitly."""

    part_code: str
    outline_entity_ref: str
    thickness_mm: float
    material_code: str
    quantity: int
    stock_allowance_mm: float | None = None
    inner_contour_entity_refs: tuple[str, ...] = Field(default=(), max_length=20_000)

    @field_validator("inner_contour_entity_refs")
    @classmethod
    def inner_contour_refs_are_opaque_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not entity_ref
            or len(entity_ref) > 512
            or any(ord(character) < 32 for character in entity_ref)
            for entity_ref in value
        ):
            raise ValueError("Inner contour entity references must be bounded opaque ids")
        if len(set(value)) != len(value):
            raise ValueError("Inner contour entity references must be unique")
        return value


class TakeoffRequest(ContractModel):
    schema_version: str = SCHEMA_VERSION
    document_id: str
    parts: tuple[PartInput, ...]
    weld_edges: tuple[str, ...] = ()
    material_profile_ref: str


class MaterialEntry(ContractModel):
    material_code: str
    description: str
    density_kg_per_m3: float


class MaterialTable(ContractModel):
    profile_id: str
    version: str
    company_approved: bool = False
    entries: tuple[MaterialEntry, ...]


class HoleGroup(ContractModel):
    diameter_mm: float
    count: int
    entity_refs: tuple[str, ...] = Field(min_length=1)


class PartTakeoffLine(ContractModel):
    part_code: str
    material_code: str
    density_kg_per_m3: float
    thickness_mm: float
    quantity: int
    net_area_mm2: float
    gross_area_mm2: float | None
    unit_mass_kg: float
    unit_mass_kg_raw: float
    unit_mass_kg_raw_text: str
    total_mass_kg: float
    total_mass_kg_raw: float
    total_mass_kg_raw_text: str
    cut_length_mm: float
    outer_cut_length_mm: float
    inner_cut_length_mm: float
    pierce_count: int
    hole_groups: tuple[HoleGroup, ...]
    weld_length_mm: float
    evidence: dict[str, tuple[str, ...]] = Field(min_length=1)

    @field_validator("evidence")
    @classmethod
    def evidence_references_entities(
        cls, value: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        if any(not refs for refs in value.values()):
            raise ValueError("Each takeoff evidence value must contain an entity reference")
        return value


class TakeoffReport(ContractModel):
    schema_version: str = SCHEMA_VERSION
    document_id: str
    revision: str
    profile_id: str
    material_profile_id: str
    material_profile_version: str
    company_approved: bool
    parts: tuple[PartTakeoffLine, ...] = ()
    excluded_contours: tuple[Finding, ...] = ()
    units: dict[str, str]


__all__ = [
    "HoleGroup",
    "MaterialEntry",
    "MaterialTable",
    "PartInput",
    "PartTakeoffLine",
    "TakeoffReport",
    "TakeoffRequest",
]
