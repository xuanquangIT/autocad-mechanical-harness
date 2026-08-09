"""Compiler for a circular bore with a radial keyway."""

from __future__ import annotations

from cad_harness.domain.errors import InvalidGeometryError
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.domain.models.operation_plan import Operation, OperationType, ValidationExpectation
from cad_harness.feature_catalog.base import (
    CompileContext,
    CompiledFeature,
    InputReport,
    operation_id,
)
from cad_harness.feature_catalog.parameters import missing_error, number, point
from cad_harness.geometry.cutouts import keyway_contour
from cad_harness.geometry.predicates import contour_overflow_mm


class KeywayCompiler:
    feature_type = "keyway"
    schema_version = SCHEMA_VERSION
    description = "Circular bore contour with a radial rectangular keyway at the positive Y side."
    required_parameters = ("bore_diameter_mm", "key_width_mm", "key_depth_mm")
    optional_parameters = ("center_mm",)

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        prefix = f"features[{feature.feature_id}].parameters"
        for key in self.required_parameters:
            report.require(
                number(feature.parameters, key) is not None,
                f"{prefix}.{key}",
                f"{key} is required",
                "positive number in millimetres",
            )
        report.require(
            feature.parameters.get("center_mm") is not None or context.datum is not None,
            f"{prefix}.center_mm",
            "A center or drawing datum is required to place the keyway",
            "[x, y]",
            "named datum",
        )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        report = self.validate_inputs(feature, context)
        if not report.is_complete:
            raise missing_error("Keyway inputs are incomplete", report)
        center_value = feature.parameters.get("center_mm")
        center = point(center_value, "center_mm") if center_value is not None else context.datum
        assert center is not None
        result = keyway_contour(
            center,
            float(feature.parameters["bore_diameter_mm"]),
            float(feature.parameters["key_width_mm"]),
            float(feature.parameters["key_depth_mm"]),
            context.tolerance,
        )
        if context.parent_outline is not None:
            overflow = contour_overflow_mm(
                context.parent_outline, result.preview_outline, context.tolerance
            )
            if not context.tolerance.is_zero_length(overflow):
                raise InvalidGeometryError(
                    "Keyway extends outside its parent outline",
                    required_action="Move or resize the keyway so it remains in the parent",
                    details={
                        "overflow_mm": overflow,
                        "parent_feature_id": context.parent_feature_id,
                    },
                )
        op_id = operation_id(feature.feature_id, "contour")
        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=[
                Operation(
                    operation_id=op_id,
                    feature_id=feature.feature_id,
                    type=OperationType.CREATE_CLOSED_POLYLINE,
                    layer=context.layer_for("hole"),
                    geometry={
                        "vertices_mm": [list(p.as_tuple()) for p in result.preview_outline.vertices]
                    },
                    expected={
                        "closed": True,
                        "area_mm2": result.preview_outline.area(),
                        "parent_feature_id": context.parent_feature_id,
                    },
                )
            ],
            expectations=[
                ValidationExpectation(
                    rule_id="KEYWAY_GEOMETRY",
                    feature_id=feature.feature_id,
                    operation_id=op_id,
                    expected={
                        "bore_diameter_mm": result.bore_radius_mm * 2.0,
                        "key_width_mm": result.key_width_mm,
                        "key_depth_mm": result.key_depth_mm,
                        "removed_area_mm2": result.removed_area_mm2,
                    },
                )
            ],
        )
