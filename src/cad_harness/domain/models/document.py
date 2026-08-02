"""Document and selection snapshots returned by inspection (architecture section 8.1)."""

from __future__ import annotations

from pydantic import Field

from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel
from cad_harness.domain.value_objects.units import Unit


class LayerInfo(ContractModel):
    name: str
    color_index: int | None = None
    linetype: str | None = None
    lineweight: int | None = None
    frozen: bool = False
    locked: bool = False


class DocumentSnapshot(ContractModel):
    """State of the active document at inspection time.

    ``revision`` is a fingerprint, not a timestamp. Any commit carries the revision
    it expects, and a mismatch rejects the commit (architecture section 13.2).
    """

    schema_version: str = SCHEMA_VERSION
    document_id: str
    revision: str
    #: Hash of the normalized path. The raw path is redacted by default.
    path_hash: str
    display_name: str
    units: Unit
    active_space: str = "model"
    active_layout: str | None = None
    layers: tuple[LayerInfo, ...] = ()
    dimension_styles: tuple[str, ...] = ()
    text_styles: tuple[str, ...] = ()
    entity_count: int = 0
    #: Capability-dependent. ``None`` means the adapter could not determine it.
    template_name: str | None = None
    read_only: bool = False


class EntitySummary(ContractModel):
    entity_ref: str
    entity_type: str
    layer: str
    feature_id: str | None = None
    measurements: dict[str, float | bool | str] = Field(default_factory=dict)


class SelectionSnapshot(ContractModel):
    """Scoped read of what the engineer selected. Never the whole database."""

    schema_version: str = SCHEMA_VERSION
    document_id: str
    revision: str
    entities: tuple[EntitySummary, ...] = ()
    truncated: bool = False
