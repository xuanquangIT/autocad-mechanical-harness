"""Compiler for a circular flange with bore and equally spaced bolt holes."""

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
from cad_harness.geometry.curves import CurveParams, normalize_circle
from cad_harness.geometry.patterns import bolt_circle
from cad_harness.geometry.primitives import Point2D


def _number(parameters: dict[str, Any], key: str) -> float | None:
    value = parameters.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise InvalidFeatureParametersError(
            f"Parameter '{key}' must be numeric", details={key: repr(value)}
        )
    if float(value) <= 0.0:
        raise InvalidFeatureParametersError(
            f"Parameter '{key}' must be greater than zero", details={key: value}
        )
    return float(value)


def _point(value: object, key: str) -> Point2D:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise InvalidFeatureParametersError(
            f"{key} must be a two-element [x, y] pair", details={key: repr(value)}
        )
    return Point2D(float(value[0]), float(value[1]))


def _circle_geometry(curve: CurveParams) -> dict[str, object]:
    assert curve.radius_mm is not None
    return {
        "center_mm": list(curve.center.as_tuple()),
        "diameter_mm": curve.radius_mm * 2.0,
    }


class FlangeCompiler:
    feature_type = "flange"
    schema_version = SCHEMA_VERSION
    description = "Circular flange with a bore and equally spaced bolt holes on a PCD."
    required_parameters = (
        "outer_diameter_mm",
        "bore_diameter_mm",
        "bolt_hole_count",
        "bolt_hole_diameter_mm",
        "pcd_mm",
        "datum",
    )
    optional_parameters = ("thickness_mm", "start_angle_deg", "material")

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        parameters = feature.parameters
        prefix = f"features[{feature.feature_id}].parameters"
        labels = {
            "outer_diameter_mm": "Outer diameter",
            "bore_diameter_mm": "Bore diameter",
            "bolt_hole_diameter_mm": "Bolt-hole diameter",
            "pcd_mm": "Pitch circle diameter",
        }
        for key, label in labels.items():
            report.require(
                _number(parameters, key) is not None,
                f"{prefix}.{key}",
                f"{label} is a required flange input",
                "positive number in millimetres",
            )
        count = parameters.get("bolt_hole_count")
        report.require(
            count is not None,
            f"{prefix}.bolt_hole_count",
            "Bolt-hole count is required and must be positive",
            "positive integer",
        )
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or count <= 0
        ):
            raise InvalidFeatureParametersError(
                "bolt_hole_count must be a positive integer",
                details={"bolt_hole_count": repr(count)},
            )
        report.require(
            parameters.get("datum") is not None or context.datum is not None,
            f"{prefix}.datum",
            "Center/datum is required to place the flange",
            "[x, y]",
            "selected_point",
            "named_datum",
        )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        report = self.validate_inputs(feature, context)
        if not report.is_complete:
            raise MissingRequiredInputsError(
                "Flange cannot be compiled while required inputs are missing",
                required_action="Supply every missing flange input and resubmit the spec",
                details={
                    "missing_inputs": [item.model_dump(mode="json") for item in report.missing]
                },
            )

        parameters = feature.parameters
        outer_diameter = float(parameters["outer_diameter_mm"])
        bore_diameter = float(parameters["bore_diameter_mm"])
        hole_diameter = float(parameters["bolt_hole_diameter_mm"])
        pcd = float(parameters["pcd_mm"])
        count = int(parameters["bolt_hole_count"])
        center = self._resolve_datum(parameters, context)
        start_angle = float(parameters.get("start_angle_deg", 0.0))

        if bore_diameter >= outer_diameter:
            raise InvalidFeatureParametersError(
                "Flange bore must be smaller than its outer diameter",
                details={
                    "bore_diameter_mm": bore_diameter,
                    "outer_diameter_mm": outer_diameter,
                },
            )

        outer_curve = normalize_circle(center, outer_diameter / 2.0)
        bore_curve = normalize_circle(center, bore_diameter / 2.0)
        hole_centers = bolt_circle(center, pcd, count, start_angle)
        outer_id = operation_id(feature.feature_id, "outer")
        holes_id = operation_id(feature.feature_id, "bolt-holes")
        operations = [
            Operation(
                operation_id=outer_id,
                feature_id=feature.feature_id,
                type=OperationType.CREATE_CIRCLE,
                layer=context.layer_for("outline"),
                geometry=_circle_geometry(outer_curve),
                expected={"diameter_mm": outer_diameter, "center_mm": list(center.as_tuple())},
            ),
            Operation(
                operation_id=operation_id(feature.feature_id, "bore"),
                feature_id=feature.feature_id,
                type=OperationType.CREATE_CIRCLE,
                layer=context.layer_for("hole"),
                geometry=_circle_geometry(bore_curve),
                expected={"diameter_mm": bore_diameter, "center_mm": list(center.as_tuple())},
            ),
            Operation(
                operation_id=holes_id,
                feature_id=feature.feature_id,
                type=OperationType.CREATE_CIRCLES,
                layer=context.layer_for("hole"),
                geometry={
                    "centers_mm": [list(point.as_tuple()) for point in hole_centers],
                    "diameter_mm": hole_diameter,
                },
                expected={"count": count, "diameter_mm": hole_diameter},
            ),
        ]

        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=operations,
            expectations=[
                ValidationExpectation(
                    rule_id="FLANGE_OUTER_DIAMETER_CLEARANCE",
                    feature_id=feature.feature_id,
                    operation_id=outer_id,
                    expected={
                        "outer_diameter_mm": outer_diameter,
                        "pcd_mm": pcd,
                        "bolt_hole_diameter_mm": hole_diameter,
                    },
                ),
                ValidationExpectation(
                    rule_id="FLANGE_HOLES_ON_PCD",
                    feature_id=feature.feature_id,
                    operation_id=holes_id,
                    expected={
                        "center_mm": list(center.as_tuple()),
                        "pcd_mm": pcd,
                        "bolt_hole_count": count,
                        "angular_spacing_deg": 360.0 / count,
                    },
                ),
            ],
            defaults_applied=list(report.defaults_applied),
            assumptions=list(report.assumptions),
        )

    @staticmethod
    def _resolve_datum(parameters: dict[str, Any], context: CompileContext) -> Point2D:
        datum = parameters.get("datum")
        if datum is not None:
            return _point(datum, "datum")
        assert context.datum is not None
        return context.datum
