"""Source-bound recompilers that cannot be invoked from an ordinary DrawingSpec."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cad_harness.comprehension.contours import AssembledContour, EdgeRecord, analyze_contours
from cad_harness.domain.errors import InvalidFeatureParametersError, MissingRequiredInputsError
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.drawing_model import CircleGeometry, EntityRecord
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.domain.models.operation_plan import Operation, OperationType
from cad_harness.feature_catalog.base import (
    CompileContext,
    CompiledFeature,
    InputReport,
    operation_id,
)
from cad_harness.geometry.areas import LineEdge
from cad_harness.geometry.curves import CurveParams


@dataclass(frozen=True, slots=True)
class _SourceSelection:
    refs: tuple[str, ...]
    entities: tuple[EntityRecord, ...]
    contour: AssembledContour | None
    edge_index: int | None


class _RecognizedCompilerBase:
    internal_only = True
    schema_version = SCHEMA_VERSION
    optional_parameters: tuple[str, ...] = ()

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        prefix = f"features[{feature.feature_id}].parameters"
        report.require(
            context.source_model is not None,
            f"{prefix}.source_model",
            "Internal recognition recompilation requires the trusted source DrawingModel",
            "DrawingModel supplied by the read/remediation service",
        )
        report.require(
            isinstance(feature.parameters.get("source_revision"), str),
            f"{prefix}.source_revision",
            "Source revision is required",
            "drawing revision fingerprint",
        )
        refs = feature.parameters.get("source_entity_refs")
        report.require(
            isinstance(refs, list | tuple)
            and bool(refs)
            and all(isinstance(item, str) for item in refs),
            f"{prefix}.source_entity_refs",
            "At least one source entity reference is required",
            "non-empty string list",
        )
        return report

    def _selection(self, feature: FeatureSpec, context: CompileContext) -> _SourceSelection:
        report = self.validate_inputs(feature, context)
        if not report.is_complete:
            raise MissingRequiredInputsError(
                "Recognition source binding is incomplete",
                required_action="Re-run recognition against the current drawing revision",
                details={
                    "missing_inputs": [item.model_dump(mode="json") for item in report.missing]
                },
            )
        model = context.source_model
        assert model is not None
        revision = str(feature.parameters["source_revision"])
        if revision != model.revision:
            raise InvalidFeatureParametersError(
                "Recognition source revision is stale",
                required_action="Read and recognize the current drawing again",
                details={"expected_revision": revision, "actual_revision": model.revision},
            )
        refs = tuple(str(item) for item in feature.parameters["source_entity_refs"])
        by_ref = {entity.entity_ref: entity for entity in model.entities}
        missing = [ref for ref in refs if ref not in by_ref]
        if missing:
            raise InvalidFeatureParametersError(
                "Recognition source entities no longer exist",
                required_action="Read and recognize the current drawing again",
                details={"missing_entity_refs": missing},
            )
        analysis = analyze_contours(model, context.tolerance)
        contour = next(
            (
                item
                for item in analysis.contours
                if set(item.entity_refs) == set(refs) or set(refs).issubset(item.entity_refs)
            ),
            None,
        )
        raw_index = feature.parameters.get("source_edge_index")
        edge_index = raw_index if isinstance(raw_index, int) else None
        return _SourceSelection(refs, tuple(by_ref[ref] for ref in refs), contour, edge_index)

    @staticmethod
    def _edge_operation(feature_id: str, index: int, record: EdgeRecord, layer: str) -> Operation:
        edge = record.edge
        if isinstance(edge, LineEdge):
            return Operation(
                operation_id=operation_id(feature_id, f"source-edge-{index}"),
                feature_id=feature_id,
                type=OperationType.CREATE_LINE,
                layer=layer,
                geometry={
                    "start_mm": list(edge.start.as_tuple()),
                    "end_mm": list(edge.end.as_tuple()),
                },
                expected={"source_entity_ref": record.entity_ref},
            )
        assert isinstance(edge, CurveParams) and edge.radius_mm is not None
        operation_type = OperationType.CREATE_CIRCLE if edge.is_full else OperationType.CREATE_ARC
        geometry: dict[str, Any] = {
            "center_mm": list(edge.center.as_tuple()),
            "radius_mm": edge.radius_mm,
        }
        if not edge.is_full:
            geometry.update(
                {
                    "start_angle_deg": edge.start_angle_deg,
                    "end_angle_deg": edge.end_angle_deg,
                }
            )
        return Operation(
            operation_id=operation_id(feature_id, f"source-edge-{index}"),
            feature_id=feature_id,
            type=operation_type,
            layer=layer,
            geometry=geometry,
            expected={"source_entity_ref": record.entity_ref, "radius_mm": edge.radius_mm},
        )


class RecognizedPartOutlineCompiler(_RecognizedCompilerBase):
    feature_type = "_recognized_part_outline"
    description = "Internal source-bound reconstruction of a recognized part outline."
    required_parameters = ("source_revision", "source_entity_refs")

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        selection = self._selection(feature, context)
        if selection.contour is None or selection.contour.is_circle:
            raise InvalidFeatureParametersError("Source entities do not form a part outline")
        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=[
                self._edge_operation(
                    feature.feature_id, index, record, context.layer_for("outline")
                )
                for index, record in enumerate(selection.contour.edges)
            ],
        )


class RecognizedCircularHoleCompiler(_RecognizedCompilerBase):
    feature_type = "_recognized_circular_hole"
    description = "Internal source-bound reconstruction of one recognized circular hole."
    required_parameters = ("source_revision", "source_entity_refs")

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        selection = self._selection(feature, context)
        if len(selection.entities) != 1 or not isinstance(
            selection.entities[0].geometry, CircleGeometry
        ):
            raise InvalidFeatureParametersError("Source selection is not one circular hole")
        geometry = selection.entities[0].geometry
        assert isinstance(geometry, CircleGeometry)
        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=[
                Operation(
                    operation_id=operation_id(feature.feature_id, "source-circle"),
                    feature_id=feature.feature_id,
                    type=OperationType.CREATE_CIRCLE,
                    layer=context.layer_for("hole"),
                    geometry={
                        "center_mm": list(geometry.center_mm),
                        "radius_mm": geometry.radius_mm,
                    },
                    expected={"diameter_mm": 2.0 * geometry.radius_mm},
                )
            ],
        )


class _RecognizedCornerCompiler(_RecognizedCompilerBase):
    def _source_edge(self, feature: FeatureSpec, context: CompileContext) -> EdgeRecord:
        selection = self._selection(feature, context)
        if selection.contour is None or selection.edge_index is None:
            raise InvalidFeatureParametersError(
                "Recognized corner lacks bound source edge topology"
            )
        if not 0 <= selection.edge_index < len(selection.contour.edges):
            raise InvalidFeatureParametersError("Recognized corner source edge index is invalid")
        return selection.contour.edges[selection.edge_index]


class RecognizedFilletCornerCompiler(_RecognizedCornerCompiler):
    feature_type = "_recognized_fillet_corner"
    description = "Internal source-bound reconstruction of a recognized fillet arc."
    required_parameters = ("source_revision", "source_entity_refs", "source_edge_index")

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        edge = self._source_edge(feature, context)
        if not isinstance(edge.edge, CurveParams) or edge.edge.is_full:
            raise InvalidFeatureParametersError("Recognized fillet source edge is not an arc")
        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=[
                self._edge_operation(feature.feature_id, 0, edge, context.layer_for("outline"))
            ],
        )


class RecognizedChamferCornerCompiler(_RecognizedCornerCompiler):
    feature_type = "_recognized_chamfer_corner"
    description = "Internal source-bound reconstruction of a recognized chamfer edge."
    required_parameters = ("source_revision", "source_entity_refs", "source_edge_index")

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        edge = self._source_edge(feature, context)
        if not isinstance(edge.edge, LineEdge):
            raise InvalidFeatureParametersError("Recognized chamfer source edge is not a line")
        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=[
                self._edge_operation(feature.feature_id, 0, edge, context.layer_for("outline"))
            ],
        )
