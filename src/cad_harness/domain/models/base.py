"""Shared base for every wire contract."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

#: Current contract major.minor. Minor bumps add optional fields only.
SCHEMA_VERSION = "1.0"


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
