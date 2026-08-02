"""Hole pattern compilers.

Hole centres are always derived here from the pattern definition. A caller supplying
explicit centre coordinates for a pattern is a smell: it bypasses the formula that
the validation rules check against.
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
from cad_harness.geometry.patterns import bolt_circle, rectangular_grid
from cad_harness.geometry.primitives import Point2D


def _require_number(parameters: dict[str, Any], key: str) -> float | None:
    value = parameters.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise InvalidFeatureParametersError(
            f"Parameter '{key}' must be numeric", details={key: repr(value)}
        )
    return float(value)


def _centermark_operations(
    feature_id: str, centers: tuple[Point2D, ...], layer: str
) -> list[Operation]:
    return [
        Operation(
            operation_id=operation_id(feature_id, f"centermark-{index}"),
            feature_id=feature_id,
            type=OperationType.CREATE_CENTERMARK,
            layer=layer,
            geometry={"center_mm": list(center.as_tuple())},
            expected={"center_mm": list(center.as_tuple())},
        )
        for index, center in enumerate(centers)
    ]


class RectangularHolePatternCompiler:
    """Grid of holes positioned by edge offsets from the parent outline."""

    feature_type = "rectangular_hole_pattern"
    schema_version = "1.0"
    description = (
        "Rectangular grid of circular holes placed by edge offsets from the parent "
        "outline, or by explicit pitch. Requires a parent feature."
    )
    required_parameters = (
        "hole_diameter_mm",
        "count_x",
        "count_y",
        "edge_offset_x_mm",
        "edge_offset_y_mm",
    )
    optional_parameters = ("pitch_x_mm", "pitch_y_mm", "add_centermarks")

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        parameters = feature.parameters
        prefix = f"features[{feature.feature_id}].parameters"

        report.require(
            _require_number(parameters, "hole_diameter_mm") is not None,
            f"{prefix}.hole_diameter_mm",
            "Hole diameter is a required engineering input",
            "positive number in millimetres",
        )
        for key in ("count_x", "count_y"):
            report.require(
                isinstance(parameters.get(key), int) and not isinstance(parameters.get(key), bool),
                f"{prefix}.{key}",
                f"{key} must be given as a positive integer hole count",
                "positive integer",
            )

        # Placement needs either edge offsets against a known outline, or explicit pitch.
        has_offsets = (
            _require_number(parameters, "edge_offset_x_mm") is not None
            and _require_number(parameters, "edge_offset_y_mm") is not None
        )
        has_pitch = (
            _require_number(parameters, "pitch_x_mm") is not None
            and _require_number(parameters, "pitch_y_mm") is not None
        )
        report.require(
            has_offsets or has_pitch,
            f"{prefix}.edge_offset_x_mm",
            "Hole placement needs edge offsets (with a parent outline) or explicit pitch",
            "edge_offset_x_mm + edge_offset_y_mm",
            "pitch_x_mm + pitch_y_mm",
        )
        if has_offsets and not has_pitch:
            report.require(
                context.parent_box is not None,
                f"{prefix}.pitch_x_mm",
                "Edge offsets require a parent outline; supply explicit pitch instead",
                "pitch_x_mm + pitch_y_mm",
            )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        report = self.validate_inputs(feature, context)
        if not report.is_complete:
            raise InvalidFeatureParametersError(
                "Hole pattern cannot be compiled while required inputs are missing",
                required_action="Supply the missing inputs and resubmit the spec",
                details={"missing": [m.path for m in report.missing]},
            )

        parameters = feature.parameters
        diameter = float(parameters["hole_diameter_mm"])
        count_x = int(parameters["count_x"])
        count_y = int(parameters["count_y"])
        origin, pitch_x, pitch_y = self._resolve_grid(parameters, context, count_x, count_y)
        centers = rectangular_grid(origin, count_x, count_y, pitch_x, pitch_y)

        layer = context.layer_for("hole")
        operations = [
            Operation(
                operation_id=operation_id(feature.feature_id, "holes"),
                feature_id=feature.feature_id,
                type=OperationType.CREATE_CIRCLES,
                layer=layer,
                geometry={
                    "centers_mm": [list(c.as_tuple()) for c in centers],
                    "diameter_mm": diameter,
                },
                expected={"count": len(centers), "diameter_mm": diameter},
            )
        ]
        if parameters.get("add_centermarks"):
            operations.extend(
                _centermark_operations(feature.feature_id, centers, context.layer_for("centermark"))
            )

        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=operations,
            expectations=[
                ValidationExpectation(
                    rule_id="GEO-HOLE-PATTERN-GRID",
                    feature_id=feature.feature_id,
                    operation_id=operation_id(feature.feature_id, "holes"),
                    expected={
                        "count": len(centers),
                        "diameter_mm": diameter,
                        "pitch_x_mm": pitch_x,
                        "pitch_y_mm": pitch_y,
                        "centers_mm": [list(c.as_tuple()) for c in centers],
                        "parent_feature_id": context.parent_feature_id,
                    },
                )
            ],
            defaults_applied=list(report.defaults_applied),
            assumptions=list(report.assumptions),
        )

    def _resolve_grid(
        self,
        parameters: dict[str, Any],
        context: CompileContext,
        count_x: int,
        count_y: int,
    ) -> tuple[Point2D, float, float]:
        pitch_x = _require_number(parameters, "pitch_x_mm")
        pitch_y = _require_number(parameters, "pitch_y_mm")
        offset_x = _require_number(parameters, "edge_offset_x_mm")
        offset_y = _require_number(parameters, "edge_offset_y_mm")

        if context.parent_box is not None and offset_x is not None and offset_y is not None:
            box = context.parent_box
            origin = Point2D(box.min_x + offset_x, box.min_y + offset_y)
            span_x = box.width - 2 * offset_x
            span_y = box.height - 2 * offset_y
            if span_x < 0 or span_y < 0:
                raise InvalidFeatureParametersError(
                    "Edge offsets exceed the parent outline size",
                    details={
                        "outline_width_mm": box.width,
                        "outline_height_mm": box.height,
                        "edge_offset_x_mm": offset_x,
                        "edge_offset_y_mm": offset_y,
                    },
                )
            derived_x = span_x / (count_x - 1) if count_x > 1 else 0.0
            derived_y = span_y / (count_y - 1) if count_y > 1 else 0.0
            return (
                origin,
                pitch_x if pitch_x is not None else derived_x,
                (pitch_y if pitch_y is not None else derived_y),
            )

        assert pitch_x is not None and pitch_y is not None  # guaranteed by validate_inputs
        base = context.datum or Point2D(0.0, 0.0)
        origin = Point2D(base.x + (offset_x or 0.0), base.y + (offset_y or 0.0))
        return origin, pitch_x, pitch_y


class BoltCirclePatternCompiler:
    """Holes equally spaced on a pitch circle diameter."""

    feature_type = "bolt_circle_pattern"
    schema_version = "1.0"
    description = "Holes equally spaced on a pitch circle diameter (PCD) about a centre point."
    required_parameters = ("hole_diameter_mm", "pcd_mm", "count", "center_mm")
    optional_parameters = ("start_angle_deg", "clockwise", "add_centermarks", "add_centerlines")

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        parameters = feature.parameters
        prefix = f"features[{feature.feature_id}].parameters"

        report.require(
            _require_number(parameters, "hole_diameter_mm") is not None,
            f"{prefix}.hole_diameter_mm",
            "Hole diameter is a required engineering input",
            "positive number in millimetres",
        )
        report.require(
            _require_number(parameters, "pcd_mm") is not None,
            f"{prefix}.pcd_mm",
            "Pitch circle diameter is a required engineering input",
            "positive number in millimetres",
        )
        report.require(
            isinstance(parameters.get("count"), int)
            and not isinstance(parameters.get("count"), bool),
            f"{prefix}.count",
            "Hole count is required and must be a positive integer",
            "positive integer",
        )
        # The centre defines placement, so it is never defaulted to the origin.
        report.require(
            parameters.get("center_mm") is not None
            or context.datum is not None
            or context.parent_box is not None,
            f"{prefix}.center_mm",
            "Centre or datum is required to place the bolt circle",
            "[x, y]",
            "selected_point",
            "named_datum",
        )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        report = self.validate_inputs(feature, context)
        if not report.is_complete:
            raise InvalidFeatureParametersError(
                "Bolt circle cannot be compiled while required inputs are missing",
                required_action="Supply the missing inputs and resubmit the spec",
                details={"missing": [m.path for m in report.missing]},
            )

        parameters = feature.parameters
        diameter = float(parameters["hole_diameter_mm"])
        pcd = float(parameters["pcd_mm"])
        count = int(parameters["count"])
        start_angle = float(parameters.get("start_angle_deg", 0.0))
        center = self._resolve_center(parameters, context)
        centers = bolt_circle(
            center, pcd, count, start_angle, clockwise=bool(parameters.get("clockwise", False))
        )

        operations = [
            Operation(
                operation_id=operation_id(feature.feature_id, "holes"),
                feature_id=feature.feature_id,
                type=OperationType.CREATE_CIRCLES,
                layer=context.layer_for("hole"),
                geometry={
                    "centers_mm": [list(c.as_tuple()) for c in centers],
                    "diameter_mm": diameter,
                },
                expected={"count": count, "diameter_mm": diameter},
            )
        ]
        if parameters.get("add_centermarks"):
            operations.extend(
                _centermark_operations(feature.feature_id, centers, context.layer_for("centermark"))
            )

        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=operations,
            expectations=[
                ValidationExpectation(
                    rule_id="GEO-HOLE-PATTERN-BOLT-CIRCLE",
                    feature_id=feature.feature_id,
                    operation_id=operation_id(feature.feature_id, "holes"),
                    expected={
                        "count": count,
                        "diameter_mm": diameter,
                        "pcd_mm": pcd,
                        "center_mm": list(center.as_tuple()),
                        "angular_spacing_deg": 360.0 / count,
                        "centers_mm": [list(c.as_tuple()) for c in centers],
                    },
                )
            ],
            defaults_applied=list(report.defaults_applied),
            assumptions=list(report.assumptions),
        )

    def _resolve_center(self, parameters: dict[str, Any], context: CompileContext) -> Point2D:
        center = parameters.get("center_mm")
        if center is not None:
            if not isinstance(center, list | tuple) or len(center) != 2:
                raise InvalidFeatureParametersError(
                    "center_mm must be a two-element [x, y] pair",
                    details={"center_mm": repr(center)},
                )
            return Point2D(float(center[0]), float(center[1]))
        if context.parent_box is not None:
            box = context.parent_box
            return Point2D((box.min_x + box.max_x) / 2.0, (box.min_y + box.max_y) / 2.0)
        assert context.datum is not None  # guaranteed by validate_inputs
        return context.datum
