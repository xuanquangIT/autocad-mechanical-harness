"""Compiler for a straight-flank obround slot."""

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
from cad_harness.geometry.curves import CurveParams
from cad_harness.geometry.patterns import slot_end_arcs, slot_outline
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


def _arc_geometry(curve: CurveParams) -> dict[str, object]:
    assert curve.radius_mm is not None
    return {
        "center_mm": list(curve.center.as_tuple()),
        "radius_mm": curve.radius_mm,
        "start_angle_deg": curve.start_angle_deg,
        "end_angle_deg": curve.end_angle_deg,
    }


class SlotCompiler:
    feature_type = "slot"
    schema_version = SCHEMA_VERSION
    description = "Straight-flank obround slot bounded by two lines and tangent semicircles."
    required_parameters = ("length_mm", "width_mm", "center_mm")
    optional_parameters = ("angle_deg", "through", "add_centerlines")

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        parameters = feature.parameters
        prefix = f"features[{feature.feature_id}].parameters"
        for key, label in (("length_mm", "Length"), ("width_mm", "Width")):
            report.require(
                _number(parameters, key) is not None,
                f"{prefix}.{key}",
                f"{label} is a required slot size",
                "positive number in millimetres",
            )
        report.require(
            parameters.get("center_mm") is not None or context.datum is not None,
            f"{prefix}.center_mm",
            "Center or drawing datum is required to place the slot",
            "[x, y]",
            "selected_point",
            "named_datum",
        )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        report = self.validate_inputs(feature, context)
        if not report.is_complete:
            raise MissingRequiredInputsError(
                "Slot cannot be compiled while required inputs are missing",
                required_action="Supply every missing slot input and resubmit the spec",
                details={
                    "missing_inputs": [item.model_dump(mode="json") for item in report.missing]
                },
            )

        parameters = feature.parameters
        length = float(parameters["length_mm"])
        width = float(parameters["width_mm"])
        angle = float(parameters.get("angle_deg", 0.0))
        center_value = parameters.get("center_mm")
        center = _point(center_value, "center_mm") if center_value is not None else context.datum
        assert center is not None

        tangent_points = slot_outline(center, length, width, angle)
        right_arc, left_arc = slot_end_arcs(tangent_points, width)
        top_left, top_right, bottom_right, bottom_left = tangent_points
        ids = {
            "top": operation_id(feature.feature_id, "top-flank"),
            "right": operation_id(feature.feature_id, "right-arc"),
            "bottom": operation_id(feature.feature_id, "bottom-flank"),
            "left": operation_id(feature.feature_id, "left-arc"),
        }
        layer = context.layer_for("outline")
        operations = [
            Operation(
                operation_id=ids["top"],
                feature_id=feature.feature_id,
                type=OperationType.CREATE_LINE,
                layer=layer,
                geometry={
                    "start_mm": list(top_left.as_tuple()),
                    "end_mm": list(top_right.as_tuple()),
                },
                expected={"length_mm": top_left.distance_to(top_right)},
            ),
            Operation(
                operation_id=ids["right"],
                feature_id=feature.feature_id,
                type=OperationType.CREATE_ARC,
                layer=layer,
                geometry=_arc_geometry(right_arc),
                expected={"radius_mm": width / 2.0, "sweep_deg": 180.0},
            ),
            Operation(
                operation_id=ids["bottom"],
                feature_id=feature.feature_id,
                type=OperationType.CREATE_LINE,
                layer=layer,
                geometry={
                    "start_mm": list(bottom_right.as_tuple()),
                    "end_mm": list(bottom_left.as_tuple()),
                },
                expected={"length_mm": bottom_right.distance_to(bottom_left)},
            ),
            Operation(
                operation_id=ids["left"],
                feature_id=feature.feature_id,
                type=OperationType.CREATE_ARC,
                layer=layer,
                geometry=_arc_geometry(left_arc),
                expected={"radius_mm": width / 2.0, "sweep_deg": 180.0},
            ),
        ]

        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=operations,
            expectations=[
                ValidationExpectation(
                    rule_id="SLOT_ARC_TANGENCY",
                    feature_id=feature.feature_id,
                    expected={
                        "line_operation_ids": [ids["top"], ids["bottom"]],
                        "arc_operation_ids": [ids["right"], ids["left"]],
                        "tangent_angle_deg": 90.0,
                    },
                )
            ],
            defaults_applied=list(report.defaults_applied),
            assumptions=list(report.assumptions),
        )
