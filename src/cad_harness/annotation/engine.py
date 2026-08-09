"""Deterministic phase-two annotation compiler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar

from cad_harness.annotation.dimension_rules import (
    GeometryMeasurements,
    Hole,
    aligned_hole_pairs,
    measure_geometry,
)
from cad_harness.annotation.placement import DEFAULT_OFFSETS_MM, TextBox, place_text
from cad_harness.annotation.title_block import resolve_title_block
from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.errors import StandardProfileNotFoundError
from cad_harness.domain.models.drawing_spec import DefaultRecord, DrawingSpec, MissingInput
from cad_harness.domain.models.operation_plan import Operation, OperationType, ValidationExpectation
from cad_harness.domain.models.validation import Finding
from cad_harness.geometry.primitives import Point2D
from cad_harness.geometry.tolerance import ToleranceProfile

RequiredValue = TypeVar("RequiredValue")


@dataclass(slots=True)
class AnnotationResult:
    operations: list[Operation] = field(default_factory=list)
    expectations: list[ValidationExpectation] = field(default_factory=list)
    defaults_applied: list[DefaultRecord] = field(default_factory=list)
    missing_inputs: list[MissingInput] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


class AnnotationEngine:
    """Reads phase-one geometry and emits annotation operations after it."""

    def __init__(self, profile: CompanyProfile, tolerance: ToleranceProfile) -> None:
        self.profile = profile
        self.tolerance = tolerance

    def annotate(
        self,
        *,
        geometry_operations: tuple[Operation, ...],
        spec: DrawingSpec,
        datum: Point2D | None,
    ) -> AnnotationResult:
        if (
            not geometry_operations
            and spec.annotations.dimensions == "none"
            and not self._has_non_dimension_annotations(spec)
        ):
            return AnnotationResult()
        if spec.annotations.dimensions == "none" and not self._has_non_dimension_annotations(spec):
            return AnnotationResult()
        text_style = self._required("text_style", self.profile.text_style)
        text_height = self._required(
            "annotation_rules.text_height_mm", self.profile.annotation_rules.text_height_mm
        )
        result = AnnotationResult()
        occupied: list[TextBox] = []
        if spec.annotations.dimensions != "none" and geometry_operations:
            style = self._required("dimension_style", self.profile.dimension_style)
            measurements = measure_geometry(
                geometry_operations,
                hole_layer=self.profile.layer_for("hole"),
                tolerance=self.tolerance,
            )
            self._dimensions(result, measurements, datum, style, text_style, text_height, occupied)
            self._centers(result, measurements)
            self._hole_table(result, measurements, text_style, text_height, occupied)
        self._title_block(result, spec, text_style, text_height, occupied)
        self._gdt(result, spec, text_style, text_height, occupied)
        return result

    def _dimensions(
        self,
        result: AnnotationResult,
        measured: GeometryMeasurements,
        datum: Point2D | None,
        dimstyle: str,
        textstyle: str,
        text_height: float,
        occupied: list[TextBox],
    ) -> None:
        dimension_layer = self.profile.layer_for("dimension")
        dimensions = (
            (
                "overall-width",
                "annotation:overall",
                measured.width_mm,
                (measured.min_x, measured.min_y),
                (measured.max_x, measured.min_y),
                (measured.min_x, measured.min_y - 8.0),
            ),
            (
                "overall-height",
                "annotation:overall",
                measured.height_mm,
                (measured.min_x, measured.min_y),
                (measured.min_x, measured.max_y),
                (measured.min_x - 8.0, measured.min_y),
            ),
        )
        for suffix, feature_id, value, start, end, anchor in dimensions:
            result.operations.append(
                self._dimension_operation(
                    suffix,
                    feature_id,
                    value,
                    start,
                    end,
                    anchor,
                    dimension_layer,
                    dimstyle,
                    textstyle,
                    text_height,
                    occupied,
                    result,
                )
            )
        threshold = self._required(
            "annotation_rules.hole_callout_min_count",
            self.profile.annotation_rules.hole_callout_min_count,
        )
        for group_index, group in enumerate(measured.hole_groups):
            first = group.holes[0]
            if len(group.holes) >= threshold:
                text = f"{len(group.holes)} × Ø{_number(group.diameter_mm)}"  # noqa: RUF001
                result.operations.append(
                    self._text_operation(
                        suffix=f"hole-callout-{group_index}",
                        feature_id=first.feature_id,
                        text=text,
                        anchor=first.center.as_tuple(),
                        layer=dimension_layer,
                        textstyle=textstyle,
                        text_height=text_height,
                        occupied=occupied,
                        result=result,
                        annotation_kind="hole_callout",
                        extra={"diameter_mm": group.diameter_mm, "count": len(group.holes)},
                    )
                )
            else:
                result.operations.append(
                    self._diameter_operation(
                        group_index,
                        first,
                        group.diameter_mm,
                        dimension_layer,
                        dimstyle,
                        textstyle,
                        text_height,
                        occupied,
                        result,
                    )
                )
            if datum is None:
                result.missing_inputs.append(
                    MissingInput(
                        path="drawing.datum",
                        reason="A datum is required for hole location dimensions",
                        accepted_formats=("point_mm", "resolved named datum"),
                    )
                )
                continue
            for hole_index, hole in enumerate(group.holes):
                for axis, value in (
                    ("x", abs(hole.center.x - datum.x)),
                    ("y", abs(hole.center.y - datum.y)),
                ):
                    end = (hole.center.x, datum.y) if axis == "x" else (datum.x, hole.center.y)
                    result.operations.append(
                        self._dimension_operation(
                            f"hole-{group_index}-{hole_index}-{axis}",
                            hole.feature_id,
                            value,
                            datum.as_tuple(),
                            end,
                            hole.center.as_tuple(),
                            dimension_layer,
                            dimstyle,
                            textstyle,
                            text_height,
                            occupied,
                            result,
                            annotation_kind=f"hole_location_{axis}",
                        )
                    )

    def _dimension_operation(
        self,
        suffix: str,
        feature_id: str,
        value: float,
        start: tuple[float, float],
        end: tuple[float, float],
        anchor: tuple[float, float],
        layer: str,
        dimstyle: str,
        textstyle: str,
        text_height: float,
        occupied: list[TextBox],
        result: AnnotationResult,
        annotation_kind: str = "linear_dimension",
    ) -> Operation:
        operation_id = f"op:annotation:{suffix}"
        text = _number(value)
        position, box, warning = self._place(
            text, anchor, text_height, occupied, feature_id, operation_id
        )
        if warning is not None:
            result.findings.append(warning)
        operation = Operation(
            operation_id=operation_id,
            feature_id=feature_id,
            type=OperationType.CREATE_LINEAR_DIMENSION,
            layer=layer,
            geometry={
                "start_mm": list(start),
                "end_mm": list(end),
                "text_position_mm": list(position),
                "measurement_mm": value,
                "text_value": text,
                "dimstyle": dimstyle,
                "textstyle": textstyle,
                "text_height_mm": text_height,
                "text_bbox_mm": box.as_list(),
                "annotation_kind": annotation_kind,
            },
            expected={"measurement_mm": value, "text_value_mm": value},
        )
        result.expectations.append(
            ValidationExpectation(
                rule_id="DIMENSION_TEXT_MATCHES_GEOMETRY",
                feature_id=feature_id,
                operation_id=operation_id,
                expected={"measurement_mm": value, "text_value_mm": value},
            )
        )
        return operation

    def _diameter_operation(
        self,
        index: int,
        hole: Hole,
        diameter: float,
        layer: str,
        dimstyle: str,
        textstyle: str,
        text_height: float,
        occupied: list[TextBox],
        result: AnnotationResult,
    ) -> Operation:
        operation_id = f"op:annotation:hole-diameter-{index}"
        text = f"Ø{_number(diameter)}"
        position, box, warning = self._place(
            text, hole.center.as_tuple(), text_height, occupied, hole.feature_id, operation_id
        )
        if warning is not None:
            result.findings.append(warning)
        result.expectations.append(
            ValidationExpectation(
                rule_id="DIMENSION_TEXT_MATCHES_GEOMETRY",
                feature_id=hole.feature_id,
                operation_id=operation_id,
                expected={"measurement_mm": diameter, "text_value_mm": diameter},
            )
        )
        return Operation(
            operation_id=operation_id,
            feature_id=hole.feature_id,
            type=OperationType.CREATE_DIAMETER_DIMENSION,
            layer=layer,
            geometry={
                "center_mm": list(hole.center.as_tuple()),
                "text_position_mm": list(position),
                "measurement_mm": diameter,
                "text_value": text,
                "dimstyle": dimstyle,
                "textstyle": textstyle,
                "text_height_mm": text_height,
                "text_bbox_mm": box.as_list(),
                "annotation_kind": "hole_diameter",
            },
            expected={"measurement_mm": diameter, "text_value_mm": diameter},
        )

    def _centers(self, result: AnnotationResult, measured: GeometryMeasurements) -> None:
        center_layer = self.profile.layer_for("centermark")
        line_layer = self.profile.layer_for("centerline")
        holes = tuple(hole for group in measured.hole_groups for hole in group.holes)
        for index, hole in enumerate(holes):
            result.operations.append(
                Operation(
                    operation_id=f"op:annotation:centermark-{index}",
                    feature_id=hole.feature_id,
                    type=OperationType.CREATE_CENTERMARK,
                    layer=center_layer,
                    geometry={"center_mm": list(hole.center.as_tuple())},
                    expected={"center_mm": list(hole.center.as_tuple())},
                )
            )
        for index, (first, second) in enumerate(aligned_hole_pairs(holes, self.tolerance)):
            result.operations.append(
                Operation(
                    operation_id=f"op:annotation:centerline-{index}",
                    feature_id=f"annotation:centerline:{first.feature_id}",
                    type=OperationType.CREATE_CENTERLINE,
                    layer=line_layer,
                    geometry={
                        "start_mm": list(first.center.as_tuple()),
                        "end_mm": list(second.center.as_tuple()),
                    },
                    expected={"coaxial": True},
                )
            )

    def _hole_table(
        self,
        result: AnnotationResult,
        measured: GeometryMeasurements,
        textstyle: str,
        text_height: float,
        occupied: list[TextBox],
    ) -> None:
        if not self.profile.annotation_rules.hole_table:
            return
        for index, group in enumerate(measured.hole_groups):
            symbol = chr(ord("A") + index)
            self._append_text(
                result,
                suffix=f"hole-table-{index}",
                feature_id="annotation:hole-table",
                text=f"{symbol} | {len(group.holes)} | Ø{_number(group.diameter_mm)}",
                anchor=(measured.max_x + 15.0, measured.max_y - index * text_height * 1.5),
                layer=self.profile.layer_for("text"),
                textstyle=textstyle,
                text_height=text_height,
                occupied=occupied,
                annotation_kind="hole_table_row",
                extra={
                    "symbol": symbol,
                    "count": len(group.holes),
                    "diameter_mm": group.diameter_mm,
                },
            )

    def _title_block(
        self,
        result: AnnotationResult,
        spec: DrawingSpec,
        textstyle: str,
        text_height: float,
        occupied: list[TextBox],
    ) -> None:
        if spec.annotations.title_block is None:
            return
        resolved = resolve_title_block(spec, self.profile)
        result.missing_inputs.extend(resolved.missing_inputs)
        for index, (name, record) in enumerate(resolved.values.items()):
            result.defaults_applied.append(record)
            self._append_text(
                result,
                suffix=f"title-block-{name}",
                feature_id="annotation:title-block",
                text=f"{name}: {record.value}",
                anchor=(0.0, -30.0 - index * text_height * 1.5),
                layer=self.profile.layer_for("text"),
                textstyle=textstyle,
                text_height=text_height,
                occupied=occupied,
                annotation_kind="title_block_field",
                extra={
                    "field_name": name,
                    "source": record.source,
                    "source_version": record.source_version,
                },
            )

    def _gdt(
        self,
        result: AnnotationResult,
        spec: DrawingSpec,
        textstyle: str,
        text_height: float,
        occupied: list[TextBox],
    ) -> None:
        layer = self.profile.layer_for("text")
        for symbol in spec.annotations.datum_symbols:
            self._append_text(
                result,
                suffix=f"gdt-datum-{symbol.identifier}",
                feature_id=symbol.feature_id,
                text=f"[{symbol.identifier}]",
                anchor=symbol.position_mm,
                layer=layer,
                textstyle=textstyle,
                text_height=text_height,
                occupied=occupied,
                annotation_kind="gdt_datum_symbol",
                extra={"datum_identifier": symbol.identifier},
            )
        for frame in spec.annotations.feature_control_frames:
            body = " | ".join((frame.characteristic, frame.tolerance_text, *frame.datum_references))
            self._append_text(
                result,
                suffix=f"gdt-frame-{frame.frame_id}",
                feature_id=frame.feature_id,
                text=body,
                anchor=frame.position_mm,
                layer=layer,
                textstyle=textstyle,
                text_height=text_height,
                occupied=occupied,
                annotation_kind="gdt_feature_control_frame",
                extra={
                    "frame_id": frame.frame_id,
                    "datum_references": list(frame.datum_references),
                    "certifies_tolerance_chain": False,
                },
            )

    def _append_text(self, result: AnnotationResult, **kwargs: object) -> None:
        result.operations.append(self._text_operation(result=result, **kwargs))  # type: ignore[arg-type]

    def _text_operation(
        self,
        *,
        suffix: str,
        feature_id: str,
        text: str,
        anchor: tuple[float, float],
        layer: str,
        textstyle: str,
        text_height: float,
        occupied: list[TextBox],
        result: AnnotationResult,
        annotation_kind: str,
        extra: dict[str, object],
    ) -> Operation:
        operation_id = f"op:annotation:{suffix}"
        position, box, warning = self._place(
            text, anchor, text_height, occupied, feature_id, operation_id
        )
        if warning is not None:
            result.findings.append(warning)
        return Operation(
            operation_id=operation_id,
            feature_id=feature_id,
            type=OperationType.CREATE_TEXT,
            layer=layer,
            geometry={
                "position_mm": list(position),
                "text": text,
                "textstyle": textstyle,
                "text_height_mm": text_height,
                "text_bbox_mm": box.as_list(),
                "annotation_kind": annotation_kind,
                **extra,
            },
            expected={"text": text},
        )

    def _place(
        self,
        text: str,
        anchor: tuple[float, float],
        height: float,
        occupied: list[TextBox],
        feature_id: str,
        operation_id: str,
    ) -> tuple[tuple[float, float], TextBox, Finding | None]:
        offsets = self.profile.annotation_rules.placement_offsets_mm or DEFAULT_OFFSETS_MM
        ratio = self.profile.annotation_rules.maximum_text_overlap_ratio
        return place_text(
            text=text,
            anchor=anchor,
            text_height_mm=height,
            occupied=occupied,
            offsets=offsets,
            maximum_overlap_ratio=0.10 if ratio is None else ratio,
            feature_id=feature_id,
            operation_id=operation_id,
        )

    @staticmethod
    def _has_non_dimension_annotations(spec: DrawingSpec) -> bool:
        annotations = spec.annotations
        return bool(
            annotations.title_block
            or annotations.datum_symbols
            or annotations.feature_control_frames
        )

    @staticmethod
    def _required(key: str, value: RequiredValue | None) -> RequiredValue:
        if value is None or value == "":
            raise StandardProfileNotFoundError(
                f"Standard profile is missing required annotation configuration '{key}'",
                required_action="Add the missing annotation configuration to the selected profile",
                details={"missing_config_key": key},
            )
        return value
