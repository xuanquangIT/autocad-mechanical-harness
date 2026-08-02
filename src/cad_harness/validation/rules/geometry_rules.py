"""Geometry validation rules (architecture section 15.1).

Each rule measures the plan and compares against the expectations the compiler
recorded. No rule trusts a value just because the compiler produced it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cad_harness.domain.models.operation_plan import Operation, OperationType
from cad_harness.domain.models.validation import Finding, Severity, ValidationStage
from cad_harness.geometry.intersections import circles_overlap
from cad_harness.geometry.predicates import (
    is_orthogonal_rectangle,
    polyline_self_intersects,
)
from cad_harness.geometry.primitives import Point2D, Polyline2D
from cad_harness.validation.engine import RuleContext
from cad_harness.validation.findings import finding

PRE_STAGES = (
    ValidationStage.PLAN,
    ValidationStage.PREVIEW_GEOMETRY,
    ValidationStage.PRE_COMMIT,
)


def _points(operation: Operation, key: str) -> list[Point2D]:
    raw = operation.geometry.get(key, [])
    return [Point2D(float(p[0]), float(p[1])) for p in raw]


def _outline_operations(context: RuleContext) -> dict[str, Operation]:
    """Closed outlines keyed by feature id, used as containment references."""
    return {
        op.feature_id: op
        for op in context.plan.operations
        if op.type is OperationType.CREATE_CLOSED_POLYLINE
    }


def _parent_of(context: RuleContext, feature_id: str) -> str | None:
    for expectation in context.plan.validation_expectations:
        if expectation.feature_id == feature_id:
            parent = expectation.expected.get("parent_feature_id")
            if isinstance(parent, str):
                return parent
    return None


@dataclass(frozen=True, slots=True)
class FiniteCoordinatesRule:
    """NaN or Infinity anywhere in the plan is blocking, never a warning."""

    rule_id: str = "GEO-FINITE-COORDS"
    stages: tuple[ValidationStage, ...] = PRE_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        for operation in context.plan.operations:
            for key, value in operation.geometry.items():
                for number in _flatten_numbers(value):
                    if not math.isfinite(number):
                        findings.append(
                            finding(
                                self.rule_id,
                                Severity.BLOCKING,
                                f"Non-finite coordinate in '{key}'",
                                feature_id=operation.feature_id,
                                operation_id=operation.operation_id,
                                expected="finite number",
                                actual=repr(number),
                                suggested_fix="Recompile the spec; the kernel must not emit NaN",
                            )
                        )
        return findings


def _flatten_numbers(value: object) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int | float):
        return [float(value)]
    if isinstance(value, list | tuple):
        return [n for item in value for n in _flatten_numbers(item)]
    return []


@dataclass(frozen=True, slots=True)
class ClosedOutlineRule:
    """Closed polylines must be closed, simple, non-degenerate and match expectations."""

    rule_id: str = "GEO-OUTLINE-CLOSED"
    stages: tuple[ValidationStage, ...] = PRE_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        tolerance = context.tolerance
        for operation in context.plan.operations:
            if operation.type is not OperationType.CREATE_CLOSED_POLYLINE:
                continue
            vertices = _points(operation, "vertices_mm")
            if len(vertices) < 3:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.BLOCKING,
                        "A closed outline needs at least three vertices",
                        feature_id=operation.feature_id,
                        operation_id=operation.operation_id,
                        expected=">= 3 vertices",
                        actual=len(vertices),
                    )
                )
                continue

            polyline = Polyline2D(tuple(vertices), closed=True)

            if polyline.has_zero_length_segment(tolerance):
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.BLOCKING,
                        "Outline contains a zero-length segment",
                        feature_id=operation.feature_id,
                        operation_id=operation.operation_id,
                        expected="all segments longer than tolerance",
                        actual="zero-length segment present",
                        tolerance=tolerance.absolute_length_mm,
                    )
                )

            if polyline_self_intersects(polyline, tolerance):
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.BLOCKING,
                        "Outline is self-intersecting",
                        feature_id=operation.feature_id,
                        operation_id=operation.operation_id,
                        expected="simple closed polyline",
                        actual="self-intersecting",
                        suggested_fix="Check vertex ordering in the feature compiler",
                    )
                )

            expected_area = operation.expected.get("area_mm2")
            if isinstance(expected_area, int | float):
                actual_area = polyline.area()
                if not tolerance.area_close(float(expected_area), actual_area):
                    findings.append(
                        finding(
                            self.rule_id,
                            Severity.ERROR,
                            "Outline area does not match the compiled expectation",
                            feature_id=operation.feature_id,
                            operation_id=operation.operation_id,
                            expected=float(expected_area),
                            actual=actual_area,
                            tolerance=tolerance.area_mm2,
                            measurement={"area_mm2": actual_area},
                        )
                    )

            if operation.expected.get("vertex_count") == 4 and not is_orthogonal_rectangle(
                polyline, tolerance
            ):
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Outline was expected to be an axis-aligned rectangle",
                        feature_id=operation.feature_id,
                        operation_id=operation.operation_id,
                        expected="4 orthogonal edges",
                        actual="non-orthogonal edge present",
                        tolerance=tolerance.absolute_length_mm,
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class HolePlacementRule:
    """Holes must sit inside their parent outline with enough material at the edge."""

    rule_id: str = "GEO-HOLE-PLACEMENT"
    stages: tuple[ValidationStage, ...] = PRE_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        outlines = _outline_operations(context)
        minimum_edge = context.profile.minimum_hole_edge_distance_mm

        for operation in context.plan.operations:
            if operation.type is not OperationType.CREATE_CIRCLES:
                continue
            centers = _points(operation, "centers_mm")
            diameter = float(operation.geometry.get("diameter_mm", 0.0))
            radius = diameter / 2.0

            parent_id = _parent_of(context, operation.feature_id)
            parent_op = outlines.get(parent_id) if parent_id else None
            if parent_op is None:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.INFO,
                        "Hole pattern has no parent outline in this plan; containment not checked",
                        feature_id=operation.feature_id,
                        operation_id=operation.operation_id,
                        expected="parent outline present",
                        actual=parent_id,
                    )
                )
                continue

            box = Polyline2D(tuple(_points(parent_op, "vertices_mm")), closed=True).bounding_box()

            for index, center in enumerate(centers):
                clearance = box.distance_to_edge(center) - radius
                if clearance < 0:
                    findings.append(
                        finding(
                            self.rule_id,
                            Severity.BLOCKING,
                            f"Hole {index} crosses or lies outside the part boundary",
                            feature_id=operation.feature_id,
                            operation_id=operation.operation_id,
                            expected="hole fully inside the outline",
                            actual={"clearance_mm": clearance},
                            tolerance=context.tolerance.absolute_length_mm,
                            measurement={"center_mm": list(center.as_tuple())},
                        )
                    )
                elif minimum_edge is not None and clearance < minimum_edge:
                    findings.append(
                        finding(
                            self.rule_id,
                            Severity.ERROR,
                            f"Hole {index} is closer to the edge than the profile minimum",
                            feature_id=operation.feature_id,
                            operation_id=operation.operation_id,
                            expected={"minimum_edge_distance_mm": minimum_edge},
                            actual={"edge_distance_mm": clearance},
                            tolerance=context.tolerance.absolute_length_mm,
                            suggested_fix="Increase the edge offset or reduce the hole diameter",
                        )
                    )
        return findings


@dataclass(frozen=True, slots=True)
class HoleSpacingRule:
    """Adjacent holes must leave the profile's minimum ligament of material."""

    rule_id: str = "GEO-HOLE-LIGAMENT"
    stages: tuple[ValidationStage, ...] = PRE_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        ligament = context.profile.minimum_hole_ligament_mm or 0.0

        for operation in context.plan.operations:
            if operation.type is not OperationType.CREATE_CIRCLES:
                continue
            centers = _points(operation, "centers_mm")
            diameter = float(operation.geometry.get("diameter_mm", 0.0))
            for i, first in enumerate(centers):
                for j, second in enumerate(centers[i + 1 :], start=i + 1):
                    if circles_overlap(
                        first, diameter, second, diameter, minimum_ligament_mm=ligament
                    ):
                        findings.append(
                            finding(
                                self.rule_id,
                                Severity.BLOCKING
                                if first.distance_to(second) < diameter
                                else Severity.ERROR,
                                f"Holes {i} and {j} are too close together",
                                feature_id=operation.feature_id,
                                operation_id=operation.operation_id,
                                expected={"minimum_center_distance_mm": diameter + ligament},
                                actual={"center_distance_mm": first.distance_to(second)},
                                tolerance=context.tolerance.absolute_length_mm,
                            )
                        )
        return findings


@dataclass(frozen=True, slots=True)
class PatternIntegrityRule:
    """Hole count, PCD and angular spacing must match the compiled expectation."""

    rule_id: str = "GEO-PATTERN-INTEGRITY"
    stages: tuple[ValidationStage, ...] = PRE_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        tolerance = context.tolerance
        operations = {op.operation_id: op for op in context.plan.operations}

        for expectation in context.plan.validation_expectations:
            operation = operations.get(expectation.operation_id or "")
            if operation is None or operation.type is not OperationType.CREATE_CIRCLES:
                continue

            centers = _points(operation, "centers_mm")
            expected_count = expectation.expected.get("count")
            if isinstance(expected_count, int) and len(centers) != expected_count:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.BLOCKING,
                        "Pattern hole count does not match the specification",
                        feature_id=operation.feature_id,
                        operation_id=operation.operation_id,
                        expected=expected_count,
                        actual=len(centers),
                    )
                )

            pcd = expectation.expected.get("pcd_mm")
            center_mm = expectation.expected.get("center_mm")
            if isinstance(pcd, int | float) and isinstance(center_mm, list):
                pattern_center = Point2D(float(center_mm[0]), float(center_mm[1]))
                expected_radius = float(pcd) / 2.0
                for index, hole in enumerate(centers):
                    actual_radius = pattern_center.distance_to(hole)
                    if not tolerance.length_close(expected_radius, actual_radius):
                        findings.append(
                            finding(
                                self.rule_id,
                                Severity.ERROR,
                                f"Hole {index} does not lie on the pitch circle",
                                feature_id=operation.feature_id,
                                operation_id=operation.operation_id,
                                expected={"radius_mm": expected_radius},
                                actual={"radius_mm": actual_radius},
                                tolerance=tolerance.absolute_length_mm,
                            )
                        )
        return findings
