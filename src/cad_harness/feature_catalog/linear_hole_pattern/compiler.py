"""Compiler for a straight-line circular-hole pattern."""

from __future__ import annotations

from cad_harness.domain.errors import InvalidFeatureParametersError, InvalidGeometryError
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.domain.models.operation_plan import Operation, OperationType, ValidationExpectation
from cad_harness.feature_catalog.base import (
    CompileContext,
    CompiledFeature,
    InputReport,
    operation_id,
)
from cad_harness.feature_catalog.parameters import integer, missing_error, number, point
from cad_harness.geometry.patterns import linear_pattern
from cad_harness.geometry.predicates import circle_overflow_mm
from cad_harness.geometry.primitives import Circle2D


class LinearHolePatternCompiler:
    feature_type = "linear_hole_pattern"
    schema_version = SCHEMA_VERSION
    description = (
        "Circular holes on a line from a required start point, direction, pitch and count."
    )
    required_parameters = ("start_point", "direction", "pitch_mm", "count", "hole_diameter_mm")
    optional_parameters: tuple[str, ...] = ()

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        parameters = feature.parameters
        prefix = f"features[{feature.feature_id}].parameters"
        report.require(
            parameters.get("start_point") is not None,
            f"{prefix}.start_point",
            "Pattern start point is required",
            "[x, y]",
        )
        report.require(
            parameters.get("direction") is not None,
            f"{prefix}.direction",
            "Pattern direction is required",
            "[dx, dy]",
        )
        report.require(
            number(parameters, "pitch_mm") is not None,
            f"{prefix}.pitch_mm",
            "Pattern pitch is required",
            "positive number in millimetres",
        )
        report.require(
            integer(parameters, "count") is not None,
            f"{prefix}.count",
            "Hole count is required",
            "positive integer",
        )
        report.require(
            number(parameters, "hole_diameter_mm") is not None,
            f"{prefix}.hole_diameter_mm",
            "Hole diameter is required",
            "positive number in millimetres",
        )
        if parameters.get("start_point") is not None:
            point(parameters["start_point"], "start_point")
        direction = parameters.get("direction")
        if direction is not None:
            if not isinstance(direction, list | tuple) or len(direction) != 2:
                raise InvalidFeatureParametersError("direction must be a two-element [dx, dy] pair")
            try:
                float(direction[0])
                float(direction[1])
            except (TypeError, ValueError) as error:
                raise InvalidFeatureParametersError(
                    "direction must contain numeric values"
                ) from error
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        report = self.validate_inputs(feature, context)
        if not report.is_complete:
            raise missing_error("Linear hole pattern inputs are incomplete", report)
        parameters = feature.parameters
        start = point(parameters["start_point"], "start_point")
        direction_value = parameters["direction"]
        direction = (float(direction_value[0]), float(direction_value[1]))
        pitch = float(parameters["pitch_mm"])
        count = int(parameters["count"])
        diameter = float(parameters["hole_diameter_mm"])
        centers = linear_pattern(start, direction, pitch, count)
        if diameter <= 0.0:
            raise InvalidFeatureParametersError("Hole diameter must be positive")
        if context.parent_outline is not None:
            overflow = max(
                circle_overflow_mm(
                    context.parent_outline, Circle2D(center, diameter), context.tolerance
                )
                for center in centers
            )
            if not context.tolerance.is_zero_length(overflow):
                raise InvalidGeometryError(
                    "Linear hole pattern extends outside its parent outline",
                    required_action=(
                        "Move or resize the pattern so every hole remains in the parent"
                    ),
                    details={
                        "overflow_mm": overflow,
                        "parent_feature_id": context.parent_feature_id,
                    },
                )
        op_id = operation_id(feature.feature_id, "holes")
        center_values = [list(center.as_tuple()) for center in centers]
        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=[
                Operation(
                    operation_id=op_id,
                    feature_id=feature.feature_id,
                    type=OperationType.CREATE_CIRCLES,
                    layer=context.layer_for("hole"),
                    geometry={"centers_mm": center_values, "diameter_mm": diameter},
                    expected={"count": count, "diameter_mm": diameter},
                )
            ],
            expectations=[
                ValidationExpectation(
                    rule_id="LINEAR_HOLE_PATTERN_GEOMETRY",
                    feature_id=feature.feature_id,
                    operation_id=op_id,
                    expected={
                        "count": count,
                        "pitch_mm": pitch,
                        "centers_mm": center_values,
                        "parent_feature_id": context.parent_feature_id,
                    },
                )
            ],
        )
