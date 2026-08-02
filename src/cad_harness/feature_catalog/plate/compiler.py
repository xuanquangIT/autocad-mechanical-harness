"""Rectangular plate compiler.

Reference case from architecture section 32: a 160x100x12 plate with four holes.
Thickness is carried for annotation and BOM; it is not drawn in a 2D top view.
"""

from __future__ import annotations

from typing import Any

from cad_harness.domain.errors import InvalidFeatureParametersError
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.domain.models.operation_plan import Operation, OperationType, ValidationExpectation
from cad_harness.feature_catalog.base import (
    CompileContext,
    CompiledFeature,
    InputReport,
    operation_id,
)
from cad_harness.geometry.primitives import BoundingBox, Point2D


def _positive(parameters: dict[str, Any], key: str) -> float | None:
    """Return a positive float, or ``None`` when absent so it reports as missing."""
    value = parameters.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise InvalidFeatureParametersError(
            f"Parameter '{key}' must be numeric", details={key: repr(value)}
        )
    if value <= 0:
        raise InvalidFeatureParametersError(
            f"Parameter '{key}' must be greater than zero", details={key: value}
        )
    return float(value)


class RectangularPlateCompiler:
    """Compiles a plate outline and delegates child features (hole patterns)."""

    feature_type = "rectangular_plate"
    schema_version = "1.0"
    description = (
        "Rectangular plate or base plate drawn as a closed outline in a 2D top view. "
        "Accepts child hole-pattern features."
    )
    required_parameters = ("width_mm", "height_mm", "thickness_mm", "origin_mm")
    optional_parameters = ("material", "corner_radius_mm")

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        parameters = feature.parameters

        for key in ("width_mm", "height_mm", "thickness_mm"):
            report.require(
                _positive(parameters, key) is not None,
                f"features[{feature.feature_id}].parameters.{key}",
                f"{key.removesuffix('_mm').replace('_', ' ').title()} is a required plate size",
                "positive number in millimetres",
            )

        # Origin drives placement, so it can never be defaulted to [0, 0].
        origin_supplied = parameters.get("origin_mm") is not None or context.datum is not None
        report.require(
            origin_supplied,
            f"features[{feature.feature_id}].parameters.origin_mm",
            "Origin or drawing datum is required to place the plate",
            "[x, y]",
            "selected_point",
            "named_datum",
        )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        report = self.validate_inputs(feature, context)
        if not report.is_complete:
            raise InvalidFeatureParametersError(
                "Plate cannot be compiled while required inputs are missing",
                required_action="Supply the missing inputs and resubmit the spec",
                details={"missing": [m.path for m in report.missing]},
            )

        parameters = feature.parameters
        width = float(parameters["width_mm"])
        height = float(parameters["height_mm"])
        origin = self._resolve_origin(parameters, context)

        vertices = (
            origin,
            Point2D(origin.x + width, origin.y),
            Point2D(origin.x + width, origin.y + height),
            Point2D(origin.x, origin.y + height),
        )
        box = BoundingBox.from_points(list(vertices))
        layer = context.layer_for("outline")

        compiled = CompiledFeature(
            feature_id=feature.feature_id,
            operations=[
                Operation(
                    operation_id=operation_id(feature.feature_id, "outline"),
                    feature_id=feature.feature_id,
                    type=OperationType.CREATE_CLOSED_POLYLINE,
                    layer=layer,
                    geometry={"vertices_mm": [list(v.as_tuple()) for v in vertices]},
                    expected={
                        "closed": True,
                        "vertex_count": 4,
                        "width_mm": width,
                        "height_mm": height,
                        "area_mm2": width * height,
                    },
                )
            ],
            expectations=[
                ValidationExpectation(
                    rule_id="GEO-PLATE-OUTLINE",
                    feature_id=feature.feature_id,
                    operation_id=operation_id(feature.feature_id, "outline"),
                    expected={
                        "orthogonal_rectangle": True,
                        "width_mm": width,
                        "height_mm": height,
                        "area_mm2": width * height,
                    },
                )
            ],
            defaults_applied=list(report.defaults_applied),
            assumptions=list(report.assumptions),
        )

        # Children are compiled against the real outline, never an assumed one.
        from cad_harness.feature_catalog.registry import get_compiler

        child_context = context.for_child(feature.feature_id, box)
        for child in feature.children:
            compiled.merge(get_compiler(child.type).compile(child, child_context))

        return compiled

    def _resolve_origin(self, parameters: dict[str, Any], context: CompileContext) -> Point2D:
        origin = parameters.get("origin_mm")
        if origin is not None:
            if not isinstance(origin, list | tuple) or len(origin) != 2:
                raise InvalidFeatureParametersError(
                    "origin_mm must be a two-element [x, y] pair",
                    details={"origin_mm": repr(origin)},
                )
            return Point2D(float(origin[0]), float(origin[1]))
        assert context.datum is not None  # guaranteed by validate_inputs
        return context.datum
