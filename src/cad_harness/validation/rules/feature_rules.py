"""Feature-specific geometric rules for compiled feature plans."""

from __future__ import annotations

import math
from dataclasses import dataclass

from cad_harness.domain.models.operation_plan import Operation, OperationType, ValidationExpectation
from cad_harness.domain.models.validation import Finding, Severity, ValidationStage
from cad_harness.geometry.intersections import angular_spacing_deg
from cad_harness.geometry.measure import line_angle_deg
from cad_harness.geometry.primitives import Point2D, Polyline2D
from cad_harness.validation.engine import RuleContext
from cad_harness.validation.findings import finding
from cad_harness.validation.rules.geometry_rules import PRE_STAGES


def _points(operation: Operation, key: str) -> list[Point2D]:
    return [Point2D(float(item[0]), float(item[1])) for item in operation.geometry.get(key, [])]


def _point(operation: Operation, key: str) -> Point2D:
    item = operation.geometry[key]
    return Point2D(float(item[0]), float(item[1]))


def _expectations(context: RuleContext, rule_id: str) -> list[ValidationExpectation]:
    return [
        item for item in context.require_plan().validation_expectations if item.rule_id == rule_id
    ]


def _operations(context: RuleContext) -> dict[str, Operation]:
    return {item.operation_id: item for item in context.require_plan().operations}


@dataclass(frozen=True, slots=True)
class ReferenceCircleGeometryRule:
    """Independently verify the circle operation against its semantic expectation."""

    rule_id: str = "REFERENCE_CIRCLE_GEOMETRY"
    stages: tuple[ValidationStage, ...] = PRE_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        operations = _operations(context)
        tolerance = context.tolerance
        for expectation in _expectations(context, self.rule_id):
            operation = operations.get(expectation.operation_id or "")
            if operation is None or operation.type is not OperationType.CREATE_CIRCLE:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.BLOCKING,
                        "Reference-circle expectation has no matching circle operation",
                        feature_id=expectation.feature_id,
                        operation_id=expectation.operation_id,
                        expected=OperationType.CREATE_CIRCLE.value,
                        actual=operation.type.value if operation is not None else None,
                    )
                )
                continue

            expected = expectation.expected
            expected_center = expected["center_mm"]
            center = _point(operation, "center_mm")
            target_center = Point2D(float(expected_center[0]), float(expected_center[1]))
            diameter = float(operation.geometry["diameter_mm"])
            radius = diameter / 2.0
            circumference = math.tau * radius
            area = math.pi * radius * radius
            actual = {
                "layer": operation.layer,
                "center_mm": list(center.as_tuple()),
                "radius_mm": radius,
                "diameter_mm": diameter,
                "circumference_mm": circumference,
                "area_mm2": area,
            }
            matches = (
                operation.layer == expected["layer"]
                and tolerance.length_close(center.distance_to(target_center), 0.0)
                and tolerance.length_close(radius, float(expected["radius_mm"]))
                and tolerance.length_close(diameter, float(expected["diameter_mm"]))
                and tolerance.length_close(circumference, float(expected["circumference_mm"]))
                and tolerance.area_close(area, float(expected["area_mm2"]))
            )
            if not matches:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Reference-circle geometry differs from its compiled expectation",
                        feature_id=expectation.feature_id,
                        operation_id=operation.operation_id,
                        expected=expected,
                        actual=actual,
                        tolerance=tolerance.absolute_length_mm,
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class FlangeOuterDiameterClearanceRule:
    rule_id: str = "FLANGE_OUTER_DIAMETER_CLEARANCE"
    stages: tuple[ValidationStage, ...] = PRE_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        ligament = context.profile.minimum_hole_ligament_mm
        for expectation in _expectations(context, self.rule_id):
            values = expectation.expected
            outer = float(values["outer_diameter_mm"])
            pcd = float(values["pcd_mm"])
            hole = float(values["bolt_hole_diameter_mm"])
            if ligament is None:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.BLOCKING,
                        "The profile does not declare a minimum flange ligament",
                        feature_id=expectation.feature_id,
                        operation_id=expectation.operation_id,
                        expected="minimum_hole_ligament_mm declared",
                        actual=None,
                        tolerance=context.tolerance.absolute_length_mm,
                    )
                )
                continue
            minimum = pcd + hole + 2.0 * ligament
            if outer <= minimum + context.tolerance.absolute_length_mm:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Flange outer diameter leaves insufficient material outside bolt holes",
                        feature_id=expectation.feature_id,
                        operation_id=expectation.operation_id,
                        expected={"outer_diameter_mm": f"> {minimum}"},
                        actual={"outer_diameter_mm": outer},
                        tolerance=context.tolerance.absolute_length_mm,
                        suggested_fix=(
                            "Increase the outer diameter or reduce the PCD or hole diameter"
                        ),
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class FlangeHolesOnPcdRule:
    rule_id: str = "FLANGE_HOLES_ON_PCD"
    stages: tuple[ValidationStage, ...] = PRE_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        operations = _operations(context)
        tolerance = context.tolerance
        for expectation in _expectations(context, self.rule_id):
            operation = operations.get(expectation.operation_id or "")
            if operation is None:
                continue
            centers = _points(operation, "centers_mm")
            expected = expectation.expected
            center_value = expected["center_mm"]
            center = Point2D(float(center_value[0]), float(center_value[1]))
            expected_radius = float(expected["pcd_mm"]) / 2.0
            for index, hole_center in enumerate(centers):
                actual_radius = center.distance_to(hole_center)
                if not tolerance.length_close(actual_radius, expected_radius):
                    findings.append(
                        finding(
                            self.rule_id,
                            Severity.ERROR,
                            f"Flange hole {index} is not on the pitch circle",
                            feature_id=expectation.feature_id,
                            operation_id=operation.operation_id,
                            expected={"radius_mm": expected_radius},
                            actual={"radius_mm": actual_radius},
                            tolerance=tolerance.absolute_length_mm,
                        )
                    )
            expected_count = int(expected["bolt_hole_count"])
            if len(centers) != expected_count:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.BLOCKING,
                        "Flange bolt-hole count differs from the specification",
                        feature_id=expectation.feature_id,
                        operation_id=operation.operation_id,
                        expected=expected_count,
                        actual=len(centers),
                        tolerance=tolerance.absolute_length_mm,
                    )
                )
            if len(centers) > 1:
                expected_spacing = float(expected["angular_spacing_deg"])
                for index, actual_spacing in enumerate(angular_spacing_deg(center, tuple(centers))):
                    if not tolerance.angle_close_deg(actual_spacing, expected_spacing):
                        findings.append(
                            finding(
                                self.rule_id,
                                Severity.ERROR,
                                f"Flange angular spacing {index} is non-uniform",
                                feature_id=expectation.feature_id,
                                operation_id=operation.operation_id,
                                expected={"angular_spacing_deg": expected_spacing},
                                actual={"angular_spacing_deg": actual_spacing},
                                tolerance=tolerance.angular_deg,
                            )
                        )
        return findings


def _arc_endpoints(operation: Operation) -> tuple[Point2D, Point2D, Point2D]:
    from cad_harness.geometry.curves import normalize_arc

    center = _point(operation, "center_mm")
    curve = normalize_arc(
        center,
        float(operation.geometry["radius_mm"]),
        float(operation.geometry["start_angle_deg"]),
        float(operation.geometry["end_angle_deg"]),
    )
    return center, curve.start_point, curve.end_point


@dataclass(frozen=True, slots=True)
class SlotArcTangencyRule:
    rule_id: str = "SLOT_ARC_TANGENCY"
    stages: tuple[ValidationStage, ...] = PRE_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        operations = _operations(context)
        tolerance = context.tolerance
        for expectation in _expectations(context, self.rule_id):
            line_ids = expectation.expected.get("line_operation_ids", [])
            arc_ids = expectation.expected.get("arc_operation_ids", [])
            lines = [operations[item] for item in line_ids if item in operations]
            arcs = [operations[item] for item in arc_ids if item in operations]
            for arc in arcs:
                center, *endpoints = _arc_endpoints(arc)
                for endpoint_index, endpoint in enumerate(endpoints):
                    candidates = [
                        (
                            min(
                                endpoint.distance_to(_point(line, "start_mm")),
                                endpoint.distance_to(_point(line, "end_mm")),
                            ),
                            line,
                        )
                        for line in lines
                    ]
                    if not candidates:
                        continue
                    _, line = min(candidates, key=lambda item: item[0])
                    angle = line_angle_deg(
                        _point(line, "start_mm"),
                        _point(line, "end_mm"),
                        center,
                        endpoint,
                        tolerance,
                    )
                    if not tolerance.angle_close_deg(angle, 90.0):
                        findings.append(
                            finding(
                                self.rule_id,
                                Severity.ERROR,
                                f"Slot arc endpoint {endpoint_index} is not tangent to its flank",
                                feature_id=expectation.feature_id,
                                operation_id=arc.operation_id,
                                expected={"tangent_angle_deg": 90.0},
                                actual={"tangent_angle_deg": angle},
                                tolerance=tolerance.angular_deg,
                            )
                        )
        return findings


@dataclass(frozen=True, slots=True)
class LBracketLegPerpendicularityRule:
    rule_id: str = "LBRACKET_LEG_PERPENDICULARITY"
    stages: tuple[ValidationStage, ...] = PRE_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        operations = _operations(context)
        tolerance = context.tolerance
        for expectation in _expectations(context, self.rule_id):
            operation = operations.get(expectation.operation_id or "")
            if operation is None:
                continue
            vertices = _points(operation, "vertices_mm")
            closed = operation.type is OperationType.CREATE_CLOSED_POLYLINE
            if not closed:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.BLOCKING,
                        "L-bracket outline is not a closed polyline",
                        feature_id=expectation.feature_id,
                        operation_id=operation.operation_id,
                        expected={"closed": True},
                        actual={"closed": False},
                        tolerance=tolerance.coincidence_mm,
                    )
                )
            if len(vertices) < 3:
                continue
            outline = Polyline2D(tuple(vertices), closed=closed)
            angle = line_angle_deg(
                outline.vertices[0],
                outline.vertices[1],
                outline.vertices[0],
                outline.vertices[-1],
                tolerance,
            )
            if not tolerance.angle_close_deg(angle, 90.0):
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "L-bracket legs are not perpendicular",
                        feature_id=expectation.feature_id,
                        operation_id=operation.operation_id,
                        expected={"leg_angle_deg": 90.0},
                        actual={"leg_angle_deg": angle},
                        tolerance=tolerance.angular_deg,
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class NoUndeclaredContourIntersectionRule:
    """Block contour crossings unless the feature relationship is declared parent-child."""

    rule_id: str = "NO_UNDECLARED_CONTOUR_INTERSECTION"
    stages: tuple[ValidationStage, ...] = PRE_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        from cad_harness.geometry.areas import ContourForest
        from cad_harness.geometry.intersections import contour_intersections

        contour_operations: list[tuple[Operation, Polyline2D]] = []
        for operation in context.require_plan().operations:
            if operation.type is not OperationType.CREATE_CLOSED_POLYLINE:
                continue
            points = _points(operation, "vertices_mm")
            if len(points) >= 3:
                contour_operations.append((operation, Polyline2D(tuple(points), closed=True)))
        if len(contour_operations) < 2:
            return []

        # Build the containment forest once so findings can report the actual geometric
        # relation, independently of caller declarations.
        forest = ContourForest.build(
            tuple(contour for _, contour in contour_operations), context.tolerance
        )
        findings: list[Finding] = []
        for first_index, (first_operation, first) in enumerate(contour_operations):
            for second_index in range(first_index + 1, len(contour_operations)):
                second_operation, second = contour_operations[second_index]
                declared = {
                    (
                        first_operation.feature_id,
                        first_operation.expected.get("parent_feature_id"),
                    ),
                    (
                        second_operation.feature_id,
                        second_operation.expected.get("parent_feature_id"),
                    ),
                }
                related = (first_operation.feature_id, second_operation.feature_id) in declared or (
                    second_operation.feature_id,
                    first_operation.feature_id,
                ) in declared
                crossings = contour_intersections(first, second, context.tolerance)
                if not crossings or related:
                    continue
                first_parent = forest.nodes[first_index].parent_index
                second_parent = forest.nodes[second_index].parent_index
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.BLOCKING,
                        "Contours intersect without a declared parent-child relationship",
                        feature_id=second_operation.feature_id,
                        operation_id=second_operation.operation_id,
                        expected={"intersection_count": 0, "declared_relationship": True},
                        actual={
                            "intersection_count": len(crossings),
                            "declared_relationship": False,
                            "first_feature_id": first_operation.feature_id,
                            "derived_parent_indices": [first_parent, second_parent],
                        },
                        tolerance=context.tolerance.coincidence_mm,
                        suggested_fix=(
                            "Declare the parent-child relationship or separate the contours"
                        ),
                    )
                )
        return findings
