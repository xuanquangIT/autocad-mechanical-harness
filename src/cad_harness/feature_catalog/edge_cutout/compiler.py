"""Compiler for a rectangular cutout removed from a parent edge."""

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
from cad_harness.feature_catalog.parameters import integer, missing_error, number
from cad_harness.geometry.cutouts import edge_cutout
from cad_harness.geometry.predicates import contour_overflow_mm


class EdgeCutoutCompiler:
    feature_type = "edge_cutout"
    schema_version = SCHEMA_VERSION
    description = "Rectangular pocket removed inward from a selected parent-outline edge."
    required_parameters = ("edge_index", "offset_mm", "width_mm", "depth_mm")
    optional_parameters: tuple[str, ...] = ()

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        prefix = f"features[{feature.feature_id}].parameters"
        report.require(
            integer(feature.parameters, "edge_index") is not None,
            f"{prefix}.edge_index",
            "Target edge index is required",
            "zero-based integer",
        )
        for key in ("offset_mm", "width_mm", "depth_mm"):
            report.require(
                number(feature.parameters, key) is not None,
                f"{prefix}.{key}",
                f"{key} is required",
                "number in millimetres",
            )
        report.require(
            context.parent_outline is not None,
            f"features[{feature.feature_id}].parent_outline",
            "Edge cutout must be declared as a child of an outline feature",
            "child feature",
        )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        report = self.validate_inputs(feature, context)
        if not report.is_complete:
            raise missing_error("Edge cutout inputs are incomplete", report)
        assert context.parent_outline is not None
        result = edge_cutout(
            context.parent_outline,
            int(feature.parameters["edge_index"]),
            float(feature.parameters["offset_mm"]),
            float(feature.parameters["width_mm"]),
            float(feature.parameters["depth_mm"]),
            context.tolerance,
        )
        overflow = contour_overflow_mm(context.parent_outline, result.outline, context.tolerance)
        if not context.tolerance.is_zero_length(overflow):
            raise InvalidGeometryError(
                "Edge cutout extends outside its parent outline",
                details={"overflow_mm": overflow, "parent_feature_id": context.parent_feature_id},
            )
        op_id = operation_id(feature.feature_id, "modified-outline")
        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=[
                Operation(
                    operation_id=op_id,
                    feature_id=feature.feature_id,
                    type=OperationType.CREATE_CLOSED_POLYLINE,
                    layer=context.layer_for("outline"),
                    geometry={"vertices_mm": [list(p.as_tuple()) for p in result.outline.vertices]},
                    expected={
                        "closed": True,
                        "area_mm2": result.outline.area(),
                        "parent_feature_id": context.parent_feature_id,
                    },
                )
            ],
            expectations=[
                ValidationExpectation(
                    rule_id="BOUNDARY_CUTOUT_AREA",
                    feature_id=feature.feature_id,
                    operation_id=op_id,
                    expected={
                        "removed_area_mm2": result.removed_area_mm2,
                        "parent_area_mm2": context.parent_outline.area(),
                        "result_area_mm2": result.outline.area(),
                    },
                )
            ],
        )
