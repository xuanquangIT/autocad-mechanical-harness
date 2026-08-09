"""Compiler for a closed 2D L-bracket profile."""

from __future__ import annotations

from typing import Any

from cad_harness.domain.errors import InvalidFeatureParametersError, MissingRequiredInputsError
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.domain.models.operation_plan import Operation, OperationType, ValidationExpectation
from cad_harness.feature_catalog.base import (
    CompileContext,
    CompiledFeature,
    InputReport,
    operation_id,
)
from cad_harness.geometry.fillet_chamfer import l_bracket_outline
from cad_harness.geometry.primitives import Point2D


def _number(parameters: dict[str, Any], key: str) -> float | None:
    value = parameters.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise InvalidFeatureParametersError(
            f"Parameter '{key}' must be numeric", details={key: repr(value)}
        )
    return float(value)


def _point(value: object, key: str) -> Point2D:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise InvalidFeatureParametersError(
            f"{key} must be a two-element [x, y] pair", details={key: repr(value)}
        )
    return Point2D(float(value[0]), float(value[1]))


class LBracketCompiler:
    feature_type = "l_bracket"
    schema_version = SCHEMA_VERSION
    description = "Closed L-shaped bracket profile with optional inner fillet."
    required_parameters = ("leg_a_mm", "leg_b_mm", "thickness_mm", "origin_mm")
    optional_parameters = ("inner_fillet_radius_mm", "material")

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        parameters = feature.parameters
        prefix = f"features[{feature.feature_id}].parameters"
        for key, label in (
            ("leg_a_mm", "Horizontal leg"),
            ("leg_b_mm", "Vertical leg"),
            ("thickness_mm", "Bracket thickness"),
        ):
            report.require(
                _number(parameters, key) is not None,
                f"{prefix}.{key}",
                f"{label} is a required bracket size",
                "positive number in millimetres",
            )
        report.require(
            parameters.get("origin_mm") is not None or context.datum is not None,
            f"{prefix}.origin_mm",
            "Origin or drawing datum is required to place the bracket",
            "[x, y]",
            "selected_point",
            "named_datum",
        )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        report = self.validate_inputs(feature, context)
        if not report.is_complete:
            raise MissingRequiredInputsError(
                "L-bracket cannot be compiled while required inputs are missing",
                required_action="Supply every missing bracket input and resubmit the spec",
                details={
                    "missing_inputs": [item.model_dump(mode="json") for item in report.missing]
                },
            )

        parameters = feature.parameters
        origin_value = parameters.get("origin_mm")
        origin = _point(origin_value, "origin_mm") if origin_value is not None else context.datum
        assert origin is not None
        leg_a = float(parameters["leg_a_mm"])
        leg_b = float(parameters["leg_b_mm"])
        thickness = float(parameters["thickness_mm"])
        fillet_value = _number(parameters, "inner_fillet_radius_mm")
        outline = l_bracket_outline(
            origin,
            leg_a,
            leg_b,
            thickness,
            context.tolerance,
            inner_fillet_radius_mm=fillet_value,
        )
        outline_id = operation_id(feature.feature_id, "outline")
        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=[
                Operation(
                    operation_id=outline_id,
                    feature_id=feature.feature_id,
                    type=OperationType.CREATE_CLOSED_POLYLINE,
                    layer=context.layer_for("outline"),
                    geometry={
                        "vertices_mm": [list(vertex.as_tuple()) for vertex in outline.vertices]
                    },
                    expected={
                        "closed": True,
                        "leg_a_mm": leg_a,
                        "leg_b_mm": leg_b,
                        "thickness_mm": thickness,
                        "area_mm2": outline.area(),
                    },
                )
            ],
            expectations=[
                ValidationExpectation(
                    rule_id="LBRACKET_LEG_PERPENDICULARITY",
                    feature_id=feature.feature_id,
                    operation_id=outline_id,
                    expected={"leg_angle_deg": 90.0, "closed": True},
                )
            ],
            defaults_applied=list(report.defaults_applied),
            assumptions=list(report.assumptions),
        )
