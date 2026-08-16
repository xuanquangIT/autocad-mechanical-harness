"""Shared base for every wire contract."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

#: Current contract major.minor. Minor bumps add contracts or tighten pre-production
#: security surfaces; established model fields otherwise remain backward compatible.
#: 1.7 - measured pilot report contract (see ADR-014).
#: 1.8 - explicit terminal cancellation for bridge IPC (see ADR-015).
#: 1.9 - calibrated raster intake, observed unknown units and point geometry (ADR-016).
#: 1.10 - separate rollback approval and exact destructive restore request (ADR-017).
#: 1.11 - explicit cross-layer inner contours for safe take-off (ADR-019).
#: 1.12 - selected-finding remediation submission and restart evidence (ADR-020).
#: 1.13 - bounded reference geometry and planning-only MCP sessions (ADR-022).
SCHEMA_VERSION = "1.13"


class ContractModel(BaseModel):
    """Strict base model for contracts crossing a process boundary.

    ``extra="forbid"`` is deliberate: an unknown field means the peer speaks a
    different contract version and must be rejected rather than silently ignored.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        populate_by_name=True,
        ser_json_inf_nan="strings",
    )

    def to_canonical_dict(self) -> dict[str, Any]:
        """Dump for hashing: JSON-mode, no ``None`` noise, enums as values."""
        return self.model_dump(mode="json", exclude_none=True)
