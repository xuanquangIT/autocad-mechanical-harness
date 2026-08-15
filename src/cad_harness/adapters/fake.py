"""In-memory adapter. Every test that does not specifically exercise COM uses this.

It behaves like a well-implemented backend: atomic, revision-tracked, measurement
returning. That makes it the reference against which COM's weaker guarantees are
compared in the contract suite.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from cad_harness.adapters.base import BaseAdapter
from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.errors import ComCallFailedError, StaleDocumentRevisionError
from cad_harness.domain.models.document import (
    DocumentSnapshot,
    EntitySummary,
    LayerInfo,
    SelectionSnapshot,
)
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.result import (
    CommitResult,
    CommitStatus,
    EntityResult,
    ExportResult,
    PreviewArtifact,
    PreviewResult,
    RollbackResult,
)
from cad_harness.domain.ports.autocad_adapter import (
    AdapterCapability,
    AdapterStatus,
    CommitRequest,
    ExportRequest,
    InspectRequest,
    RollbackRequest,
    SelectionRequest,
)
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.domain.value_objects.units import Unit
from cad_harness.geometry.primitives import Point2D, Polyline2D


@dataclass(slots=True)
class FakeEntity:
    entity_ref: str
    entity_type: str
    layer: str
    feature_id: str
    operation_id: str
    geometry: dict[str, Any]
    measurements: dict[str, Any]


@dataclass(slots=True)
class FakeDocument:
    document_id: str
    display_name: str = "fake-drawing.dwg"
    units: Unit = Unit.MM
    entities: dict[str, FakeEntity] = field(default_factory=dict)
    layers: list[str] = field(
        default_factory=lambda: ["0", "OBJECT", "HIDDEN", "CENTER", "DIM", "TEXT"]
    )
    #: Bumped on every write so revisions change even if geometry repeats.
    write_counter: int = 0
    snapshots: dict[str, dict[str, FakeEntity]] = field(default_factory=dict)


class FakeAutoCADAdapter(BaseAdapter):
    """Deterministic, dependency-free CAD backend."""

    adapter_type = "fake"
    supported_operations = frozenset(OperationType)
    capabilities = frozenset(
        {
            AdapterCapability.INSPECT_DOCUMENT,
            AdapterCapability.INSPECT_SELECTION,
            AdapterCapability.PREVIEW,
            AdapterCapability.COMMIT,
            AdapterCapability.EXPORT,
            AdapterCapability.ATOMIC_TRANSACTION,
            AdapterCapability.DOCUMENT_LOCK,
            AdapterCapability.UNDO_GROUP,
            AdapterCapability.STABLE_METADATA,
            AdapterCapability.CHECKPOINT_RESTORE,
        }
    )

    def __init__(self, document: FakeDocument | None = None) -> None:
        self.document = document or FakeDocument(document_id=new_id(IdPrefix.DOCUMENT))
        self._entity_counter = 0
        #: idempotency_key -> previous result, so a retry cannot duplicate entities.
        self._executions: dict[str, CommitResult] = {}

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def status(self) -> AdapterStatus:
        return AdapterStatus(
            adapter_type=self.adapter_type,
            available=True,
            capabilities=tuple(sorted(self.capabilities, key=lambda c: c.value)),
            cad_application="FakeCAD",
            cad_version="0.0.0",
            active_document_id=self.document.document_id,
            message="In-memory adapter. No AutoCAD involved.",
        )

    def current_revision(self) -> str:
        return self.revision_from(
            self.document.document_id,
            [
                self.document.write_counter,
                sorted(
                    (e.entity_ref, e.entity_type, e.layer, sha256_of(e.geometry))
                    for e in self.document.entities.values()
                ),
            ],
        )

    def inspect_document(self, request: InspectRequest) -> DocumentSnapshot:
        self.require(AdapterCapability.INSPECT_DOCUMENT)
        return DocumentSnapshot(
            document_id=self.document.document_id,
            revision=self.current_revision(),
            path_hash=sha256_of(self.document.display_name),
            display_name=self.document.display_name,
            units=self.document.units,
            layers=tuple(LayerInfo(name=name) for name in self.document.layers),
            dimension_styles=("Standard", "DEMO-ISO-MM"),
            text_styles=("Standard", "DEMO-ISO"),
            entity_count=len(self.document.entities),
        )

    def inspect_selection(self, request: SelectionRequest) -> SelectionSnapshot:
        self.require(AdapterCapability.INSPECT_SELECTION)
        entities = tuple(self.document.entities.values())
        selected = entities[: request.max_entities]
        return SelectionSnapshot(
            document_id=self.document.document_id,
            revision=self.current_revision(),
            entities=tuple(
                EntitySummary(
                    entity_ref=entity.entity_ref,
                    entity_type=entity.entity_type,
                    layer=entity.layer,
                    feature_id=entity.feature_id,
                    measurements={
                        key: value
                        for key, value in entity.measurements.items()
                        if isinstance(value, bool | float | int | str)
                    },
                )
                for entity in selected
            ),
            truncated=len(selected) < len(entities),
        )

    def validate_revision(self, document_id: str, expected_revision: str) -> bool:
        return (
            document_id == self.document.document_id
            and self.current_revision() == expected_revision
        )

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    def preview(self, plan: OperationPlan) -> PreviewResult:
        """Preview never touches the document, so it only reports what it would do."""
        self.require(AdapterCapability.PREVIEW)
        return PreviewResult(
            preview_id=new_id(IdPrefix.PREVIEW),
            job_id=plan.job_id,
            plan_hash=plan.plan_hash or plan.compute_hash(),
            artifacts=(
                PreviewArtifact(kind="semantic_diff", artifact_ref="memory://fake/semantic_diff"),
            ),
        )

    def commit(self, request: CommitRequest) -> CommitResult:
        self.require(AdapterCapability.COMMIT)

        # Idempotent retry: replay the recorded result instead of writing again.
        previous = self._executions.get(request.idempotency_key)
        if previous is not None:
            return previous

        current = self.current_revision()
        if current != request.expected_revision:
            raise StaleDocumentRevisionError(
                "Document changed since the plan was approved",
                required_action="Re-inspect, regenerate preview, validate and approve again",
                details={
                    "expected_revision": request.expected_revision,
                    "actual_revision": current,
                },
            )

        checkpoint_id = None
        if request.create_checkpoint:
            checkpoint_id = new_id(IdPrefix.CHECKPOINT)
            self.document.snapshots[checkpoint_id] = deepcopy(self.document.entities)

        # Stage against a private document copy.  Commit reconciles retained entities
        # in place, so update preserves the identity of its target FakeEntity.
        working_entities = deepcopy(self.document.entities)
        results: list[EntityResult] = []
        original_counter = self._entity_counter
        try:
            for operation in request.plan.operations:
                built = self._execute_operation(operation, working_entities)
                for entity, entity_result in built:
                    if operation.type is not OperationType.DELETE_ENTITY:
                        working_entities[entity.entity_ref] = entity
                    results.append(entity_result)
        except Exception:
            self._entity_counter = original_counter
            if checkpoint_id:
                self.document.snapshots.pop(checkpoint_id, None)
            raise

        self._apply_entities(working_entities)
        self.document.write_counter += 1

        result = CommitResult(
            job_id=request.plan.job_id,
            plan_hash=request.plan.plan_hash or request.plan.compute_hash(),
            status=CommitStatus.COMMITTED,
            entity_results=tuple(results),
            previous_revision=current,
            new_revision=self.current_revision(),
            checkpoint_id=checkpoint_id,
            undo_group=f"fake-undo-{self.document.write_counter}",
        )
        self._executions[request.idempotency_key] = result
        return result

    def rollback(self, request: RollbackRequest) -> RollbackResult:
        self.require(AdapterCapability.CHECKPOINT_RESTORE)
        if not self.validate_revision(request.document_id, request.current_revision):
            from cad_harness.domain.errors import StaleDocumentRevisionError

            raise StaleDocumentRevisionError(
                "Document changed after rollback approval",
                required_action="Review and approve rollback for the current revision",
                details={"approved_revision": request.current_revision},
            )
        if request.checkpoint_id and request.checkpoint_id in self.document.snapshots:
            self.document.entities = deepcopy(self.document.snapshots[request.checkpoint_id])
            self.document.write_counter += 1
            return RollbackResult(
                job_id=request.job_id,
                restored_revision=self.current_revision(),
                checkpoint_id=request.checkpoint_id,
                method="checkpoint_restore",
            )
        from cad_harness.domain.errors import RollbackNotAvailableError

        raise RollbackNotAvailableError(
            "No checkpoint available for this job",
            required_action="Restore the document manually or re-plan from a fresh inspection",
            details={"checkpoint_id": request.checkpoint_id},
        )

    def export(self, request: ExportRequest) -> ExportResult:
        self.require(AdapterCapability.EXPORT)
        return ExportResult(
            document_id=self.document.document_id,
            format=request.format,
            artifact_ref=f"memory://fake/export.{request.format}",
            byte_size=0,
        )

    # ------------------------------------------------------------------ #
    # Operation mapping
    # ------------------------------------------------------------------ #

    def _next_ref(self) -> str:
        self._entity_counter += 1
        return f"fake:handle:{self._entity_counter:X}"

    def _execute_operation(
        self, operation: Operation, entities: dict[str, FakeEntity]
    ) -> list[tuple[FakeEntity, EntityResult]]:
        if operation.type is OperationType.UPDATE_ENTITY:
            entity = self._target_entity(operation, entities)
            properties = operation.geometry.get("properties", {})
            if not isinstance(properties, dict):
                raise ComCallFailedError(
                    "Update properties must be an object",
                    required_action="Recreate the remediation plan with valid properties",
                    details={"operation_id": operation.operation_id},
                )
            entity.layer = operation.layer
            existing = entity.geometry.get("properties", {})
            entity.geometry["properties"] = {
                **(existing if isinstance(existing, dict) else {}),
                **properties,
            }
            return [
                (
                    entity,
                    EntityResult(
                        operation_id=operation.operation_id,
                        feature_id=operation.feature_id,
                        entity_ref=entity.entity_ref,
                        entity_type=entity.entity_type,
                        measurements={"layer": entity.layer},
                    ),
                )
            ]
        if operation.type is OperationType.DELETE_ENTITY:
            entity = self._target_entity(operation, entities)
            del entities[entity.entity_ref]
            return [
                (
                    entity,
                    EntityResult(
                        operation_id=operation.operation_id,
                        feature_id=operation.feature_id,
                        entity_ref=entity.entity_ref,
                        entity_type=entity.entity_type,
                        measurements={"deleted": True},
                    ),
                )
            ]
        return self._build_entities(operation)

    def _target_entity(self, operation: Operation, entities: dict[str, FakeEntity]) -> FakeEntity:
        entity_ref = operation.target_entity_ref
        entity = entities.get(entity_ref) if entity_ref else None
        if entity is None:
            raise ComCallFailedError(
                "The operation references an entity that is not present in the fake document",
                required_action="Re-inspect the drawing and recreate the remediation plan",
                details={
                    "reason": "entity_reference_not_found",
                    "entity_ref": entity_ref,
                    "document_id": self.document.document_id,
                },
            )
        return entity

    def _apply_entities(self, working_entities: dict[str, FakeEntity]) -> None:
        """Apply a staged document without replacing retained entity objects."""
        for entity_ref in set(self.document.entities) - set(working_entities):
            del self.document.entities[entity_ref]
        for entity_ref, staged in working_entities.items():
            existing = self.document.entities.get(entity_ref)
            if existing is None:
                self.document.entities[entity_ref] = staged
                continue
            existing.entity_type = staged.entity_type
            existing.layer = staged.layer
            existing.feature_id = staged.feature_id
            existing.operation_id = staged.operation_id
            existing.geometry = staged.geometry
            existing.measurements = staged.measurements

    def _build_entities(self, operation: Operation) -> list[tuple[FakeEntity, EntityResult]]:
        entity_type = self.entity_type_for(operation)

        if operation.type is OperationType.CREATE_CIRCLES:
            # One CAD entity per circle, but a single aggregate measurement per operation.
            centers = operation.geometry.get("centers_mm", [])
            diameter = float(operation.geometry.get("diameter_mm", 0.0))
            built: list[tuple[FakeEntity, EntityResult]] = []
            refs: list[str] = []
            for center in centers:
                ref = self._next_ref()
                refs.append(ref)
                built.append(
                    (
                        FakeEntity(
                            entity_ref=ref,
                            entity_type=entity_type,
                            layer=operation.layer,
                            feature_id=operation.feature_id,
                            operation_id=operation.operation_id,
                            geometry={"center_mm": center, "diameter_mm": diameter},
                            measurements={"diameter_mm": diameter},
                        ),
                        EntityResult(
                            operation_id=operation.operation_id,
                            feature_id=operation.feature_id,
                            entity_ref=ref,
                            entity_type=entity_type,
                            measurements={
                                "count": len(centers),
                                "diameter_mm": diameter,
                                "center_mm": center,
                                "area_mm2": math.pi * (diameter / 2.0) ** 2,
                            },
                        ),
                    )
                )
            return built

        ref = self._next_ref()
        measurements = self._measure(operation)
        entity = FakeEntity(
            entity_ref=ref,
            entity_type=entity_type,
            layer=operation.layer,
            feature_id=operation.feature_id,
            operation_id=operation.operation_id,
            geometry=dict(operation.geometry),
            measurements=measurements,
        )
        return [
            (
                entity,
                EntityResult(
                    operation_id=operation.operation_id,
                    feature_id=operation.feature_id,
                    entity_ref=ref,
                    entity_type=entity_type,
                    measurements=measurements,
                ),
            )
        ]

    def _measure(self, operation: Operation) -> dict[str, Any]:
        """Re-measure from geometry rather than echoing ``expected``.

        Echoing expectations would make post-commit validation vacuous.
        """
        if operation.type in {
            OperationType.CREATE_CLOSED_POLYLINE,
            OperationType.CREATE_POLYLINE,
        }:
            vertices = [
                Point2D(float(v[0]), float(v[1])) for v in operation.geometry.get("vertices_mm", [])
            ]
            closed = operation.type is OperationType.CREATE_CLOSED_POLYLINE
            polyline = Polyline2D(tuple(vertices), closed=closed)
            box = polyline.bounding_box()
            return {
                "closed": closed,
                "vertex_count": len(vertices),
                "width_mm": box.width,
                "height_mm": box.height,
                "area_mm2": polyline.area() if closed else 0.0,
                "perimeter_mm": polyline.perimeter(),
            }
        if operation.type is OperationType.CREATE_LINE:
            start = Point2D(*map(float, operation.geometry.get("start_mm", ())))
            end = Point2D(*map(float, operation.geometry.get("end_mm", ())))
            return {
                "start_mm": list(start.as_tuple()),
                "end_mm": list(end.as_tuple()),
                "length_mm": start.distance_to(end),
            }
        if operation.type is OperationType.CREATE_CIRCLE:
            center = [float(value) for value in operation.geometry.get("center_mm", ())]
            if "radius_mm" in operation.geometry:
                radius = float(operation.geometry["radius_mm"])
                measurements: dict[str, Any] = {"center_mm": center, "radius_mm": radius}
            else:
                diameter = float(operation.geometry.get("diameter_mm", 0.0))
                radius = diameter / 2.0
                measurements = {"center_mm": center, "diameter_mm": diameter}
            if "radius_mm" in operation.expected and "radius_mm" not in measurements:
                measurements["radius_mm"] = radius
            if "diameter_mm" in operation.expected and "diameter_mm" not in measurements:
                measurements["diameter_mm"] = radius * 2.0
            if "area_mm2" in operation.expected:
                measurements["area_mm2"] = math.pi * radius * radius
            return measurements
        if operation.type is OperationType.CREATE_ARC:
            center = [float(value) for value in operation.geometry.get("center_mm", ())]
            radius = float(operation.geometry.get("radius_mm", 0.0))
            start_angle = float(operation.geometry.get("start_angle_deg", 0.0))
            end_angle = float(operation.geometry.get("end_angle_deg", 0.0))
            measurements = {
                "center_mm": center,
                "radius_mm": radius,
                "start_angle_deg": start_angle,
                "end_angle_deg": end_angle,
            }
            if "arc_length_mm" in operation.expected:
                measurements["arc_length_mm"] = radius * math.radians(
                    (end_angle - start_angle) % 360.0
                )
            return measurements
        return dict(operation.geometry)
