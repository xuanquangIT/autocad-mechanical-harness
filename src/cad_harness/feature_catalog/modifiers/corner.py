"""Corner fillet and chamfer outline modifiers."""

from __future__ import annotations

from typing import Any

from cad_harness.domain.errors import InvalidFeatureParametersError, MissingRequiredInputsError
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.drawing_spec import ModifierSpec
from cad_harness.domain.models.operation_plan import ValidationExpectation
from cad_harness.feature_catalog.base import CompileContext, InputReport
from cad_harness.feature_catalog.modifiers.base import (
    ModifiedOutline,
    ReplacedCorner,
    modifier_feature_id,
)
from cad_harness.geometry.curves import linearize_curve
from cad_harness.geometry.fillet_chamfer import chamfer_vertex, fillet_vertex
from cad_harness.geometry.primitives import Point2D, Polyline2D


def _number(parameters: dict[str, Any], key: str) -> float | None:
    value = parameters.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise InvalidFeatureParametersError(
            f"Parameter '{key}' must be numeric", details={key: repr(value)}
        )
    return float(value)


def _indices(parameters: dict[str, Any], vertex_count: int) -> tuple[int, ...] | None:
    value = parameters.get("vertex_indices")
    if value is None:
        return None
    if not isinstance(value, list | tuple) or not value:
        raise InvalidFeatureParametersError("vertex_indices must be a non-empty integer list")
    if any(not isinstance(item, int) or isinstance(item, bool) for item in value):
        raise InvalidFeatureParametersError("Every target vertex index must be an integer")
    result = tuple(int(item) for item in value)
    if len(set(result)) != len(result):
        raise InvalidFeatureParametersError("Target vertex indices must be unique")
    if any(item < 0 or item >= vertex_count for item in result):
        raise InvalidFeatureParametersError(
            "Target vertex index is outside the outline",
            details={"vertex_indices": list(result), "vertex_count": vertex_count},
        )
    return result


def _missing(report: InputReport, message: str) -> MissingRequiredInputsError:
    return MissingRequiredInputsError(
        message,
        required_action="Supply every missing modifier input and resubmit the spec",
        details={"missing_inputs": [item.model_dump(mode="json") for item in report.missing]},
    )


class CornerFilletModifier:
    modifier_type = "corner_fillet"
    schema_version = SCHEMA_VERSION

    def validate_inputs(
        self, spec: ModifierSpec, outline: Polyline2D, context: CompileContext
    ) -> InputReport:
        report = InputReport()
        report.require(
            _number(spec.parameters, "radius_mm") is not None,
            f"modifiers[{spec.type}].parameters.radius_mm",
            "Fillet radius is required",
            "positive number in millimetres",
        )
        report.require(
            spec.parameters.get("vertex_indices") is not None,
            f"modifiers[{spec.type}].parameters.vertex_indices",
            "At least one target corner is required",
            "non-empty integer list",
        )
        if spec.parameters.get("vertex_indices") is not None:
            _indices(spec.parameters, len(outline.vertices))
        return report

    def apply(
        self,
        spec: ModifierSpec,
        outline: Polyline2D,
        context: CompileContext,
        *,
        modifier_index: int,
    ) -> ModifiedOutline:
        report = self.validate_inputs(spec, outline, context)
        if not report.is_complete:
            raise _missing(report, "Corner fillet inputs are incomplete")
        radius = float(spec.parameters["radius_mm"])
        indices = _indices(spec.parameters, len(outline.vertices))
        assert indices is not None
        modifier_id = modifier_feature_id(
            context.parent_feature_id or "unbound", self.modifier_type, modifier_index
        )
        replacements: dict[int, ReplacedCorner] = {}
        expectations: list[ValidationExpectation] = []
        for index in indices:
            previous = outline.vertices[(index - 1) % len(outline.vertices)]
            vertex = outline.vertices[index]
            following = outline.vertices[(index + 1) % len(outline.vertices)]
            result = fillet_vertex(previous, vertex, following, radius, context.tolerance)
            points = linearize_curve(result.arc, context.tolerance.arc_chord_tolerance_mm)
            replacements[index] = ReplacedCorner(index, vertex, points, result.arc)
            expectations.append(
                ValidationExpectation(
                    rule_id="CORNER_FILLET_GEOMETRY",
                    feature_id=modifier_id,
                    expected={
                        "vertex_index": index,
                        "radius_mm": radius,
                        "actual_radius_mm": result.arc.radius_mm,
                        "maximum_allowed_mm": result.maximum_radius_mm,
                        "tangent_in_mm": list(result.tangent_in.as_tuple()),
                        "tangent_out_mm": list(result.tangent_out.as_tuple()),
                    },
                )
            )
        modified_vertices: list[Point2D] = []
        for index, vertex in enumerate(outline.vertices):
            replacement = replacements.get(index)
            modified_vertices.extend(replacement.replacement_points if replacement else (vertex,))
        return ModifiedOutline(
            modifier_id,
            Polyline2D(tuple(modified_vertices), closed=True),
            tuple(replacements[index] for index in indices),
            tuple(expectations),
        )


class CornerChamferModifier:
    modifier_type = "corner_chamfer"
    schema_version = SCHEMA_VERSION

    def validate_inputs(
        self, spec: ModifierSpec, outline: Polyline2D, context: CompileContext
    ) -> InputReport:
        report = InputReport()
        report.require(
            _number(spec.parameters, "distance_1_mm") is not None,
            f"modifiers[{spec.type}].parameters.distance_1_mm",
            "First chamfer distance is required",
            "positive number in millimetres",
        )
        report.require(
            spec.parameters.get("vertex_indices") is not None,
            f"modifiers[{spec.type}].parameters.vertex_indices",
            "At least one target corner is required",
            "non-empty integer list",
        )
        second = _number(spec.parameters, "distance_2_mm")
        angle = _number(spec.parameters, "angle_deg")
        report.require(
            second is not None or angle is not None,
            f"modifiers[{spec.type}].parameters.distance_2_mm",
            "A second distance or angle is required",
            "distance_2_mm",
            "angle_deg",
        )
        if second is not None and angle is not None:
            raise InvalidFeatureParametersError("Provide exactly one of distance_2_mm or angle_deg")
        if spec.parameters.get("vertex_indices") is not None:
            _indices(spec.parameters, len(outline.vertices))
        return report

    def apply(
        self,
        spec: ModifierSpec,
        outline: Polyline2D,
        context: CompileContext,
        *,
        modifier_index: int,
    ) -> ModifiedOutline:
        report = self.validate_inputs(spec, outline, context)
        if not report.is_complete:
            raise _missing(report, "Corner chamfer inputs are incomplete")
        distance_1 = float(spec.parameters["distance_1_mm"])
        distance_2 = _number(spec.parameters, "distance_2_mm")
        angle = _number(spec.parameters, "angle_deg")
        indices = _indices(spec.parameters, len(outline.vertices))
        assert indices is not None
        modifier_id = modifier_feature_id(
            context.parent_feature_id or "unbound", self.modifier_type, modifier_index
        )
        replacements: dict[int, ReplacedCorner] = {}
        expectations: list[ValidationExpectation] = []
        for index in indices:
            previous = outline.vertices[(index - 1) % len(outline.vertices)]
            vertex = outline.vertices[index]
            following = outline.vertices[(index + 1) % len(outline.vertices)]
            result = chamfer_vertex(
                previous,
                vertex,
                following,
                distance_1,
                distance_second_mm=distance_2,
                angle_deg=angle,
                tolerance=context.tolerance,
            )
            points = (result.point_on_first, result.point_on_second)
            replacements[index] = ReplacedCorner(index, vertex, points)
            expectations.append(
                ValidationExpectation(
                    rule_id="CORNER_CHAMFER_GEOMETRY",
                    feature_id=modifier_id,
                    expected={
                        "vertex_index": index,
                        "distance_1_mm": result.distance_first_mm,
                        "distance_2_mm": result.distance_second_mm,
                        "angle_deg": result.angle_deg,
                        "actual_angle_deg": result.angle_deg,
                    },
                )
            )
        modified_vertices: list[Point2D] = []
        for index, vertex in enumerate(outline.vertices):
            replacement = replacements.get(index)
            modified_vertices.extend(replacement.replacement_points if replacement else (vertex,))
        return ModifiedOutline(
            modifier_id,
            Polyline2D(tuple(modified_vertices), closed=True),
            tuple(replacements[index] for index in indices),
            tuple(expectations),
        )
