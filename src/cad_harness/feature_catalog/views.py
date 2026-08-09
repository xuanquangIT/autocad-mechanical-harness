"""Deterministic placement of linked orthographic views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.errors import StandardProfileNotFoundError, UnsupportedFeatureError
from cad_harness.domain.models.drawing_spec import ViewSpec
from cad_harness.domain.models.operation_plan import Operation

SUPPORTED_VIEW_TYPES = ("top", "front", "side", "section")


@dataclass(frozen=True, slots=True)
class ViewPlacementResult:
    operations: tuple[Operation, ...]
    origins: dict[str, tuple[float, float]]


def _origin(view_type: str, spacing: float) -> tuple[float, float]:
    return {
        "top": (0.0, 0.0),
        "front": (0.0, -spacing),
        "side": (spacing, 0.0),
        "section": (spacing * 2.0, 0.0),
    }[view_type]


def _translate_geometry(value: dict[str, Any], dx: float, dy: float) -> dict[str, Any]:
    translated = dict(value)
    for key in ("center_mm", "start_mm", "end_mm", "position_mm", "text_position_mm"):
        point = translated.get(key)
        if isinstance(point, list | tuple) and len(point) == 2:
            translated[key] = [float(point[0]) + dx, float(point[1]) + dy]
    for key in ("centers_mm", "vertices_mm"):
        points = translated.get(key)
        if isinstance(points, list | tuple):
            translated[key] = [[float(p[0]) + dx, float(p[1]) + dy] for p in points]
    return translated


def view_feature_id(base_feature_id: str, view_type: str) -> str:
    base = base_feature_id.removeprefix("feature:").split("@", 1)[0]
    return f"feature:{base}@{view_type}"


def place_views(
    operations: tuple[Operation, ...], views: tuple[ViewSpec, ...], profile: CompanyProfile
) -> ViewPlacementResult:
    """Clone phase-one geometry per view while preserving source and view order."""
    unsupported = tuple(view.type for view in views if view.type not in SUPPORTED_VIEW_TYPES)
    if unsupported:
        raise UnsupportedFeatureError(
            f"Unsupported drawing view type '{unsupported[0]}'",
            required_action="Choose a supported orthographic view type",
            details={
                "unsupported_view_types": list(unsupported),
                "supported_view_types": list(SUPPORTED_VIEW_TYPES),
            },
        )
    spacing = profile.layout_rules.view_spacing_mm
    if spacing is None:
        raise StandardProfileNotFoundError(
            "Standard profile does not declare multi-view spacing",
            required_action="Set layout_rules.view_spacing_mm in the company profile",
            details={"missing_config_key": "layout_rules.view_spacing_mm"},
        )
    origins = {view.type: _origin(view.type, spacing) for view in views}
    placed: list[Operation] = []
    for view in views:
        dx, dy = origins[view.type]
        for source in operations:
            feature_id = view_feature_id(source.feature_id, view.type)
            suffix = source.operation_id.rsplit(":", 1)[-1]
            geometry = _translate_geometry(source.geometry, dx, dy)
            geometry.update(
                {
                    "view_type": view.type,
                    "view_origin_mm": [dx, dy],
                    "base_feature_id": source.feature_id,
                }
            )
            placed.append(
                source.model_copy(
                    update={
                        "operation_id": f"op:{feature_id}:{suffix}",
                        "feature_id": feature_id,
                        "geometry": geometry,
                    }
                )
            )
    return ViewPlacementResult(tuple(placed), origins)
