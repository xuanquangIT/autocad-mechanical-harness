"""Validation rules for generated and read-back annotation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cad_harness.annotation.placement import TextBox, overlap_ratio
from cad_harness.domain.models.drawing_model import DimensionGeometry, DrawingModel
from cad_harness.domain.models.operation_plan import OperationType
from cad_harness.domain.models.validation import Finding, Severity, ValidationStage
from cad_harness.validation.engine import RuleContext
from cad_harness.validation.findings import finding


@dataclass(frozen=True, slots=True)
class DimensionTextMatchesGeometryRule:
    rule_id: str = "DIMENSION_TEXT_MATCHES_GEOMETRY"
    stages: tuple[ValidationStage, ...] = (ValidationStage.PLAN, ValidationStage.DRAWING_AUDIT)

    def evaluate(self, context: RuleContext) -> list[Finding]:
        records: list[tuple[str | None, str | None, str | None, float, float]] = []
        if context.plan is not None:
            dimension_types = {
                OperationType.CREATE_LINEAR_DIMENSION,
                OperationType.CREATE_ALIGNED_DIMENSION,
                OperationType.CREATE_DIAMETER_DIMENSION,
                OperationType.CREATE_RADIUS_DIMENSION,
                OperationType.CREATE_ANGULAR_DIMENSION,
            }
            for operation in context.plan.operations:
                if operation.type not in dimension_types:
                    continue
                expected = operation.geometry.get("measurement_mm")
                actual = operation.expected.get("text_value_mm")
                if isinstance(expected, int | float) and isinstance(actual, int | float):
                    records.append(
                        (
                            operation.feature_id,
                            operation.operation_id,
                            None,
                            float(expected),
                            float(actual),
                        )
                    )
        raw_dimensions = context.extras.get("dimensions", ())
        if isinstance(raw_dimensions, list | tuple):
            for raw in raw_dimensions:
                if isinstance(raw, dict):
                    expected = raw.get("measurement_mm")
                    actual = raw.get("text_value_mm")
                    if isinstance(expected, int | float) and isinstance(actual, int | float):
                        records.append((None, None, None, float(expected), float(actual)))
        if context.plan is None:
            drawing = context.require_drawing_model()
            if isinstance(drawing, DrawingModel):
                for entity in drawing.entities:
                    geometry = entity.geometry
                    if not isinstance(geometry, DimensionGeometry):
                        continue
                    if geometry.measurement_mm is None or geometry.text_override is None:
                        continue
                    try:
                        override = float(geometry.text_override.strip())
                    except ValueError:
                        continue
                    records.append(
                        (None, None, entity.entity_ref, geometry.measurement_mm, override)
                    )
        findings: list[Finding] = []
        for feature_id, operation_id, entity_ref, expected, actual in records:
            if context.tolerance.length_close(expected, actual):
                continue
            findings.append(
                finding(
                    self.rule_id,
                    Severity.ERROR,
                    "Dimension text does not match measured geometry",
                    feature_id=feature_id,
                    operation_id=operation_id,
                    entity_ref=entity_ref,
                    expected=expected,
                    actual=actual,
                    tolerance=context.tolerance.absolute_length_mm,
                    suggested_fix="Reset the override to the measured dimension value",
                )
            )
        return findings


@dataclass(frozen=True, slots=True)
class AnnotationOverlapRule:
    rule_id: str = "ANNOTATION_OVERLAP"
    stages: tuple[ValidationStage, ...] = (ValidationStage.PLAN, ValidationStage.PRE_COMMIT)

    def evaluate(self, context: RuleContext) -> list[Finding]:
        boxes: list[tuple[str, str, TextBox]] = []
        for operation in context.require_plan().operations:
            raw = operation.geometry.get("text_bbox_mm")
            if isinstance(raw, list | tuple) and len(raw) == 4:
                boxes.append(
                    (
                        operation.feature_id,
                        operation.operation_id,
                        TextBox(*(float(value) for value in raw)),
                    )
                )
        limit = context.profile.annotation_rules.maximum_text_overlap_ratio
        maximum = 0.10 if limit is None else limit
        findings: list[Finding] = []
        for index, (feature_id, operation_id, first) in enumerate(boxes):
            for _, other_id, second in boxes[index + 1 :]:
                ratio = overlap_ratio(first, second)
                if ratio <= maximum:
                    continue
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.WARNING,
                        "Annotation text bounding boxes exceed the allowed overlap",
                        feature_id=feature_id,
                        operation_id=operation_id,
                        expected={"maximum_overlap_ratio": maximum},
                        actual={"overlap_ratio": ratio, "other_operation_id": other_id},
                        tolerance=maximum,
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class AnnotationProfileRule:
    rule_id: str = "ANNOTATION_PROFILE_MATCH"
    stages: tuple[ValidationStage, ...] = (ValidationStage.PLAN, ValidationStage.PRE_COMMIT)

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        for operation in context.require_plan().operations:
            kind = operation.geometry.get("annotation_kind")
            if kind is None and operation.type not in {
                OperationType.CREATE_CENTERMARK,
                OperationType.CREATE_CENTERLINE,
            }:
                continue
            expected_layer = self._expected_layer(operation, context)
            if operation.layer != expected_layer:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Annotation is on the wrong profile layer",
                        feature_id=operation.feature_id,
                        operation_id=operation.operation_id,
                        expected=expected_layer,
                        actual=operation.layer,
                    )
                )
            if ("dimension" in str(kind) or kind == "hole_diameter") and operation.geometry.get(
                "dimstyle"
            ) != context.profile.dimension_style:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Dimension uses the wrong dimstyle",
                        operation_id=operation.operation_id,
                        expected=context.profile.dimension_style,
                        actual=operation.geometry.get("dimstyle"),
                    )
                )
            if (
                "textstyle" in operation.geometry
                and operation.geometry.get("textstyle") != context.profile.text_style
            ):
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Annotation uses the wrong textstyle",
                        operation_id=operation.operation_id,
                        expected=context.profile.text_style,
                        actual=operation.geometry.get("textstyle"),
                    )
                )
        return findings

    @staticmethod
    def _expected_layer(operation: Any, context: RuleContext) -> str:
        operation_type = operation.type
        annotation_kind = operation.geometry.get("annotation_kind")
        if operation_type is OperationType.CREATE_CENTERMARK:
            return context.profile.layer_for("centermark")
        if operation_type is OperationType.CREATE_CENTERLINE:
            return context.profile.layer_for("centerline")
        if annotation_kind == "hole_callout" or operation_type in {
            OperationType.CREATE_LINEAR_DIMENSION,
            OperationType.CREATE_ALIGNED_DIMENSION,
            OperationType.CREATE_DIAMETER_DIMENSION,
            OperationType.CREATE_RADIUS_DIMENSION,
            OperationType.CREATE_ANGULAR_DIMENSION,
        }:
            return context.profile.layer_for("dimension")
        return context.profile.layer_for("text")


@dataclass(frozen=True, slots=True)
class GdtDatumExistsRule:
    rule_id: str = "GDT_DATUM_EXISTS"
    stages: tuple[ValidationStage, ...] = (ValidationStage.PLAN, ValidationStage.PRE_COMMIT)

    def evaluate(self, context: RuleContext) -> list[Finding]:
        datums: set[str] = set()
        frames: list[tuple[str, tuple[str, ...]]] = []
        for operation in context.require_plan().operations:
            kind = operation.geometry.get("annotation_kind")
            if kind == "gdt_datum_symbol":
                identifier = operation.geometry.get("datum_identifier")
                if isinstance(identifier, str):
                    datums.add(identifier)
            elif kind == "gdt_feature_control_frame":
                refs = operation.geometry.get("datum_references", [])
                frames.append((operation.operation_id, tuple(str(value) for value in refs)))
        findings: list[Finding] = []
        for operation_id, references in frames:
            missing = tuple(reference for reference in references if reference not in datums)
            if missing:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Feature control frame references an undefined datum symbol",
                        operation_id=operation_id,
                        expected={"defined_datums": sorted(datums)},
                        actual={"missing_datums": list(missing)},
                    )
                )
        return findings
