"""Compiler for one explicitly dimensioned reference circle.

This is a feature-level drafting intent, not a primitive MCP write operation.  The
caller supplies the engineering radius, placement datum and approved layer; the
compiler derives the adapter-neutral circle operation deterministically.
"""

from __future__ import annotations

import math

from cad_harness.domain.errors import InvalidFeatureParametersError
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
from cad_harness.geometry.curves import normalize_circle
from cad_harness.geometry.primitives import Point2D


class ReferenceCircleCompiler:
    """Compile a radius, centre/datum and declared layer into one circle."""

    feature_type = "reference_circle"
    schema_version = SCHEMA_VERSION
    description = (
        "One explicitly dimensioned reference circle placed at a centre or drawing datum "
        "on a layer declared by the selected company profile."
    )
    required_parameters = ("radius_mm", "layer_name")
    optional_parameters = ("center_mm",)

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        parameters = feature.parameters
        prefix = f"features[{feature.feature_id}].parameters"
        allowed_parameters = set(self.required_parameters) | set(self.optional_parameters)
        unexpected_parameters = sorted(set(parameters) - allowed_parameters)
        if unexpected_parameters:
            raise InvalidFeatureParametersError(
                "reference_circle contains unsupported parameters",
                required_action="Use only center_mm, radius_mm and layer_name",
                details={"unexpected_parameters": unexpected_parameters},
            )
        radius = number(parameters, "radius_mm")
        report.require(
            radius is not None,
            f"{prefix}.radius_mm",
            "Reference-circle radius is required",
            "positive finite number in millimetres",
        )
        report.require(
            parameters.get("center_mm") is not None or context.datum is not None,
            f"{prefix}.center_mm",
            "A centre or resolved drawing datum is required to place the circle",
            "[x, y]",
            "drawing.datum",
        )
        report.require(
            parameters.get("layer_name") is not None,
            f"{prefix}.layer_name",
            "An explicit declared layer is required for reference geometry",
            "layer name from the selected company profile",
        )

        if radius is not None and (not math.isfinite(radius) or radius <= 0.0):
            raise InvalidFeatureParametersError(
                "radius_mm must be a positive finite number",
                details={"radius_mm": repr(parameters.get("radius_mm"))},
            )
        if parameters.get("center_mm") is not None:
            point(parameters["center_mm"], "center_mm")

        layer_name = parameters.get("layer_name")
        if layer_name is not None:
            if not isinstance(layer_name, str) or not layer_name:
                raise InvalidFeatureParametersError(
                    "layer_name must be a non-empty string",
                    details={"layer_name": repr(layer_name)},
                )
            declared_layers = context.profile.layer_names()
            if layer_name not in declared_layers:
                raise InvalidFeatureParametersError(
                    f"Layer '{layer_name}' is not declared by the selected company profile",
                    required_action="Choose one of the profile's declared layers",
                    details={
                        "layer_name": layer_name,
                        "declared_layers": sorted(declared_layers),
                    },
                )

        if feature.modifiers or feature.children:
            raise InvalidFeatureParametersError(
                "reference_circle does not accept modifiers or child features",
                required_action="Submit each reference circle as an independent feature",
                details={
                    "modifier_count": len(feature.modifiers),
                    "child_count": len(feature.children),
                },
            )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        report = self.validate_inputs(feature, context)
        if not report.is_complete:
            raise missing_error("Reference-circle inputs are incomplete", report)

        center = self._resolve_center(feature, context)
        radius = float(feature.parameters["radius_mm"])
        layer_name = str(feature.parameters["layer_name"])
        derived_values = (
            radius * 2.0,
            math.tau * radius,
            math.pi * radius * radius,
            center.x - radius,
            center.x + radius,
            center.y - radius,
            center.y + radius,
        )
        if not all(math.isfinite(value) for value in derived_values):
            raise InvalidFeatureParametersError(
                "reference_circle geometry exceeds finite drafting bounds",
                required_action="Use a smaller radius and finite centre coordinates",
                details={"radius_mm": radius},
            )
        curve = normalize_circle(center, radius)
        assert curve.radius_mm is not None

        diameter = curve.radius_mm * 2.0
        circumference = math.tau * curve.radius_mm
        area = math.pi * curve.radius_mm * curve.radius_mm
        operation_expected = {
            "layer": layer_name,
            "center_mm": list(curve.center.as_tuple()),
            "radius_mm": curve.radius_mm,
            "diameter_mm": diameter,
            "area_mm2": area,
        }
        geometry_expected = {**operation_expected, "circumference_mm": circumference}
        op_id = operation_id(feature.feature_id, "circle")
        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=[
                Operation(
                    operation_id=op_id,
                    feature_id=feature.feature_id,
                    type=OperationType.CREATE_CIRCLE,
                    layer=layer_name,
                    geometry={
                        "center_mm": list(curve.center.as_tuple()),
                        "diameter_mm": diameter,
                    },
                    expected=operation_expected,
                )
            ],
            expectations=[
                ValidationExpectation(
                    rule_id="REFERENCE_CIRCLE_GEOMETRY",
                    feature_id=feature.feature_id,
                    operation_id=op_id,
                    expected=geometry_expected,
                )
            ],
        )

    @staticmethod
    def _resolve_center(feature: FeatureSpec, context: CompileContext) -> Point2D:
        center = feature.parameters.get("center_mm")
        if center is not None:
            return point(center, "center_mm")
        assert context.datum is not None
        return context.datum
