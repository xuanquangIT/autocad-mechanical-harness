"""Implemented outline modifiers and deterministic lookup."""

from cad_harness.domain.errors import UnsupportedFeatureError
from cad_harness.feature_catalog.modifiers.base import (
    ModifiedOutline,
    OutlineModifier,
    ReplacedCorner,
    modifier_feature_id,
)
from cad_harness.feature_catalog.modifiers.corner import (
    CornerChamferModifier,
    CornerFilletModifier,
)

_MODIFIERS: dict[str, OutlineModifier] = {
    "corner_fillet": CornerFilletModifier(),
    "corner_chamfer": CornerChamferModifier(),
}


def get_modifier(modifier_type: str) -> OutlineModifier:
    try:
        return _MODIFIERS[modifier_type]
    except KeyError:
        raise UnsupportedFeatureError(
            f"Modifier type '{modifier_type}' is not in the catalog",
            required_action="Choose a supported modifier type",
            details={"supported": sorted(_MODIFIERS)},
        ) from None


__all__ = [
    "CornerChamferModifier",
    "CornerFilletModifier",
    "ModifiedOutline",
    "OutlineModifier",
    "ReplacedCorner",
    "get_modifier",
    "modifier_feature_id",
]
