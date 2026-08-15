"""Compile selected drawing-audit findings into the existing guarded write plan."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field

from cad_harness.domain.errors import (
    DocumentNotFoundError,
    InvalidFeatureParametersError,
    MissingRequiredInputsError,
    StaleDocumentRevisionError,
)
from cad_harness.domain.models.base import ContractModel
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    DrawingModel,
    EntityRecord,
    LineGeometry,
    PolylineGeometry,
)
from cad_harness.domain.models.operation_plan import (
    Operation,
    OperationPlan,
    OperationType,
    ValidationExpectation,
)
from cad_harness.domain.models.validation import Finding
from cad_harness.domain.ports.repositories import DrawingAuditStore
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.feature_catalog.base import operation_id
from cad_harness.geometry.curves import CurveParams, normalize_arc
from cad_harness.geometry.fillet_chamfer import fillet_vertex
from cad_harness.geometry.intersections import line_intersection
from cad_harness.geometry.primitives import Point2D
from cad_harness.geometry.tolerance import ToleranceProfile

type FindingRef = tuple[str, str]

_DELETE_RULES = frozenset({"DUPLICATE_ENTITY", "ZERO_LENGTH_ENTITY"})
_NON_AUTOMATIC_RULES: dict[str, tuple[str, ...]] = {
    "HOLE_EDGE_DISTANCE_MIN": ("remediation.hole.center_or_diameter",),
    "HOLE_LIGAMENT_MIN": ("remediation.hole.center_or_diameter",),
    "HOLE_OUTSIDE_PART": ("remediation.hole.center",),
}
_DIRECT_RULES = frozenset(
    {
        "OPEN_CONTOUR",
        "DUPLICATE_ENTITY",
        "ZERO_LENGTH_ENTITY",
        "ENTITY_ON_EXPECTED_LAYER",
        "DIMSTYLE_IN_PROFILE",
        "TEXTSTYLE_IN_PROFILE",
        "DIMENSION_TEXT_MATCHES_GEOMETRY",
        "FILLET_NOT_TANGENT",
        "OVERLAPPING_ENTITY",
    }
)


class OperationSource(ContractModel):
    """Trace one generated operation back to the selected finding that caused it."""

    operation_id: str
    rule_id: str
    entity_ref: str


class RemediationResult(ContractModel):
    """A hashed write plan plus an explicit selected-finding trace."""

    plan: OperationPlan
    audit_id: str
    operation_sources: tuple[OperationSource, ...]
    selected_findings: tuple[FindingRef, ...]
    technical_inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


def _missing(rule_id: str, entity_ref: str, *paths: str) -> MissingRequiredInputsError:
    return MissingRequiredInputsError(
        "The selected finding needs an engineering decision before it can be repaired",
        required_action="Supply the listed technical value and compile a new remediation plan",
        details={
            "rule_id": rule_id,
            "entity_ref": entity_ref,
            "missing_paths": list(paths),
        },
    )


def _point(value: tuple[float, float]) -> Point2D:
    return Point2D(float(value[0]), float(value[1]))


def _curve_geometry(curve: CurveParams) -> dict[str, float | list[float]]:
    assert curve.radius_mm is not None
    return {
        "center_mm": [curve.center.x, curve.center.y],
        "radius_mm": curve.radius_mm,
        "start_angle_deg": curve.start_angle_deg,
        "end_angle_deg": curve.end_angle_deg,
    }


def _entity_endpoints(entity: EntityRecord) -> tuple[Point2D, Point2D]:
    geometry = entity.geometry
    if isinstance(geometry, LineGeometry):
        return _point(geometry.start_mm), _point(geometry.end_mm)
    if isinstance(geometry, ArcGeometry):
        curve = normalize_arc(
            _point(geometry.center_mm),
            geometry.radius_mm,
            geometry.start_angle_deg,
            geometry.end_angle_deg,
        )
        return curve.start_point, curve.end_point
    if isinstance(geometry, PolylineGeometry) and geometry.vertices:
        return _point(geometry.vertices[0].point_mm), _point(geometry.vertices[-1].point_mm)
    raise InvalidFeatureParametersError(
        "The open-contour finding does not reference endpoint geometry",
        required_action="Audit the drawing again and select a supported open contour",
        details={"entity_ref": entity.entity_ref, "entity_type": geometry.kind},
    )


def _nearest_endpoint_pair(first: EntityRecord, second: EntityRecord) -> tuple[Point2D, Point2D]:
    candidates = tuple(
        (a.distance_to(b), first_index, second_index, a, b)
        for first_index, a in enumerate(_entity_endpoints(first))
        for second_index, b in enumerate(_entity_endpoints(second))
    )
    _, _, _, first_point, second_point = min(candidates, key=lambda item: item[:3])
    return first_point, second_point


def _technical_values(
    technical_inputs: Mapping[str, Mapping[str, Any]], rule_id: str, entity_ref: str
) -> Mapping[str, Any]:
    return technical_inputs.get(f"{rule_id}:{entity_ref}", {})


class RemediationService:
    """Pure compiler; it cannot preview, approve or commit its own output."""

    def __init__(self, tolerance: ToleranceProfile, audit_store: DrawingAuditStore) -> None:
        self._tolerance = tolerance
        self._audit_store = audit_store

    def compile_plan(
        self,
        *,
        job_id: str,
        model: DrawingModel,
        audit_id: str,
        selected_rule_findings: Sequence[FindingRef],
        technical_inputs: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> RemediationResult:
        if not model.geometry_normalized:
            raise InvalidFeatureParametersError(
                "Remediation requires geometry normalized to millimetres",
                details={"geometry_normalized": False},
            )
        evidence = self._audit_store.get_drawing_audit(audit_id)
        if evidence is None:
            raise DocumentNotFoundError(
                "The persisted drawing audit does not exist",
                required_action="Run and persist a new drawing audit before remediation",
                details={"audit_id": audit_id},
            )
        if evidence.document_id != model.document_id or evidence.revision != model.revision:
            raise StaleDocumentRevisionError(
                "The drawing revision no longer matches the audited revision",
                required_action="Audit the current drawing revision before compiling remediation",
                details={
                    "audit_id": audit_id,
                    "audit_document_id": evidence.document_id,
                    "audit_revision": evidence.revision,
                    "drawing_document_id": model.document_id,
                    "drawing_revision": model.revision,
                },
            )
        audit_report = evidence.report
        selected = tuple(selected_rule_findings)
        if not selected:
            raise InvalidFeatureParametersError(
                "At least one concrete audit finding must be selected",
                required_action="Select findings by (rule_id, entity_ref)",
            )
        if len(set(selected)) != len(selected):
            raise InvalidFeatureParametersError(
                "A remediation selection cannot contain duplicate finding references"
            )
        effective_profile_ref = audit_report.profile_ref
        if not effective_profile_ref:
            raise _missing("AUDIT_PROFILE", "document", "remediation.profile_ref")

        finding_by_ref: dict[FindingRef, Finding] = {}
        for finding in audit_report.findings:
            if finding.entity_ref is not None:
                finding_by_ref.setdefault((finding.rule_id, finding.entity_ref), finding)
        unknown = [item for item in selected if item not in finding_by_ref]
        if unknown:
            raise InvalidFeatureParametersError(
                "A selected finding is not present in the supplied audit report",
                required_action="Audit again and select a finding from that exact report",
                details={"unknown_findings": [list(item) for item in unknown]},
            )

        entities = {entity.entity_ref: entity for entity in model.entities}
        technical = technical_inputs or {}
        allowed_technical_keys: dict[str, set[str]] = {}
        for rule_id, entity_ref in selected:
            if rule_id == "FILLET_NOT_TANGENT":
                allowed_technical_keys[f"{rule_id}:{entity_ref}"] = {"radius_mm"}
            elif rule_id == "OVERLAPPING_ENTITY":
                allowed_technical_keys[f"{rule_id}:{entity_ref}"] = {"strategy"}
        unexpected_technical = {
            key: sorted(values)
            for key, values in technical.items()
            if key not in allowed_technical_keys or set(values) - allowed_technical_keys[key]
        }
        if unexpected_technical:
            raise InvalidFeatureParametersError(
                "Remediation accepts only the documented engineering values, never coordinates",
                required_action="Remove unrequested technical inputs and compile again",
                details={"unexpected_technical_inputs": unexpected_technical},
            )
        operations: list[Operation] = []
        sources: list[OperationSource] = []
        expectations: list[ValidationExpectation] = []
        for rule_id, entity_ref in selected:
            finding = finding_by_ref[(rule_id, entity_ref)]
            entity = entities.get(entity_ref)
            if entity is None:
                raise InvalidFeatureParametersError(
                    "The selected finding references an entity absent from the drawing revision",
                    details={"rule_id": rule_id, "entity_ref": entity_ref},
                )
            if rule_id in _NON_AUTOMATIC_RULES:
                raise _missing(rule_id, entity_ref, *_NON_AUTOMATIC_RULES[rule_id])
            if rule_id not in _DIRECT_RULES:
                raise _missing(
                    rule_id,
                    entity_ref,
                    f"remediation.{rule_id.lower()}.strategy",
                )

            generated = self._compile_finding(
                model,
                finding,
                entity,
                _technical_values(technical, rule_id, entity_ref),
            )
            for operation in generated:
                operations.append(operation)
                sources.append(
                    OperationSource(
                        operation_id=operation.operation_id,
                        rule_id=rule_id,
                        entity_ref=entity_ref,
                    )
                )
            expectations.append(
                ValidationExpectation(
                    rule_id=rule_id,
                    feature_id=f"remediation:{rule_id}:{entity_ref}",
                    expected={"resolved": True, "entity_ref": entity_ref},
                )
            )

        plan = OperationPlan(
            plan_id=new_id(IdPrefix.PLAN),
            job_id=job_id,
            document_id=model.document_id,
            expected_revision=evidence.revision,
            profile_ref=effective_profile_ref,
            operations=tuple(operations),
            validation_expectations=tuple(expectations),
        ).with_hash()
        return RemediationResult(
            plan=plan,
            audit_id=audit_id,
            operation_sources=tuple(sources),
            selected_findings=selected,
            technical_inputs={key: dict(values) for key, values in technical.items()},
        )

    def _compile_finding(
        self,
        model: DrawingModel,
        finding: Finding,
        entity: EntityRecord,
        technical: Mapping[str, Any],
    ) -> tuple[Operation, ...]:
        rule_id = finding.rule_id
        entity_ref = entity.entity_ref
        feature_id = f"remediation:{rule_id}:{entity_ref}"
        if rule_id in _DELETE_RULES:
            return (
                Operation(
                    operation_id=operation_id(feature_id, "delete"),
                    feature_id=feature_id,
                    type=OperationType.DELETE_ENTITY,
                    layer=entity.layer,
                    target_entity_ref=entity_ref,
                    expected={"remediates_rule_id": rule_id},
                ),
            )
        if rule_id == "OVERLAPPING_ENTITY":
            if technical.get("strategy") != "delete_selected":
                raise _missing(rule_id, entity_ref, "remediation.overlap.strategy")
            return (
                Operation(
                    operation_id=operation_id(feature_id, "delete-selected"),
                    feature_id=feature_id,
                    type=OperationType.DELETE_ENTITY,
                    layer=entity.layer,
                    target_entity_ref=entity_ref,
                    expected={
                        "remediates_rule_id": rule_id,
                        "strategy": "delete_selected",
                    },
                ),
            )
        if rule_id == "OPEN_CONTOUR":
            return self._close_contour(model, finding, entity, feature_id)
        if rule_id == "ENTITY_ON_EXPECTED_LAYER":
            if not isinstance(finding.expected, str) or not finding.expected:
                raise _missing(rule_id, entity_ref, "finding.expected_layer")
            return (
                self._update(
                    entity,
                    feature_id,
                    "layer",
                    layer=finding.expected,
                    properties={},
                    expected={"layer": finding.expected},
                ),
            )
        if rule_id in {"DIMSTYLE_IN_PROFILE", "TEXTSTYLE_IN_PROFILE"}:
            allowed = finding.expected
            if not isinstance(allowed, list) or not allowed or not isinstance(allowed[0], str):
                raise _missing(rule_id, entity_ref, "finding.expected_style")
            return (
                self._update(
                    entity,
                    feature_id,
                    "style",
                    layer=entity.layer,
                    properties={"StyleName": allowed[0]},
                    expected={"style_name": allowed[0]},
                ),
            )
        if rule_id == "DIMENSION_TEXT_MATCHES_GEOMETRY":
            if not isinstance(finding.expected, int | float) or not math.isfinite(
                float(finding.expected)
            ):
                raise _missing(rule_id, entity_ref, "finding.expected_measurement")
            return (
                self._update(
                    entity,
                    feature_id,
                    "text-override",
                    layer=entity.layer,
                    properties={"TextOverride": ""},
                    expected={"measurement_mm": float(finding.expected)},
                ),
            )
        if rule_id == "FILLET_NOT_TANGENT":
            radius = technical.get("radius_mm")
            if not isinstance(radius, int | float) or not math.isfinite(float(radius)):
                raise _missing(rule_id, entity_ref, "remediation.fillet.radius_mm")
            return self._rebuild_fillet(model, entity, feature_id, float(radius))
        raise AssertionError(f"unhandled remediation rule: {rule_id}")

    @staticmethod
    def _update(
        entity: EntityRecord,
        feature_id: str,
        suffix: str,
        *,
        layer: str,
        properties: dict[str, Any],
        expected: dict[str, Any],
    ) -> Operation:
        return Operation(
            operation_id=operation_id(feature_id, suffix),
            feature_id=feature_id,
            type=OperationType.UPDATE_ENTITY,
            layer=layer,
            geometry={"properties": properties},
            expected=expected,
            target_entity_ref=entity.entity_ref,
        )

    def _close_contour(
        self,
        model: DrawingModel,
        finding: Finding,
        entity: EntityRecord,
        feature_id: str,
    ) -> tuple[Operation, ...]:
        other_ref = (
            finding.actual.get("other_entity_ref") if isinstance(finding.actual, dict) else None
        )
        if isinstance(entity.geometry, PolylineGeometry) and not entity.geometry.closed:
            start, end = _entity_endpoints(entity)
        elif isinstance(other_ref, str) and other_ref in {
            candidate.entity_ref for candidate in model.entities
        }:
            other = next(item for item in model.entities if item.entity_ref == other_ref)
            start, end = _nearest_endpoint_pair(entity, other)
        else:
            raise _missing("OPEN_CONTOUR", entity.entity_ref, "remediation.open_contour.strategy")
        gap = start.distance_to(end)
        if self._tolerance.is_coincident(gap):
            raise InvalidFeatureParametersError(
                "The selected contour is already closed within tolerance",
                details={"entity_ref": entity.entity_ref, "gap_mm": gap},
            )
        return (
            Operation(
                operation_id=operation_id(feature_id, "closure"),
                feature_id=feature_id,
                type=OperationType.CREATE_LINE,
                layer=entity.layer,
                geometry={
                    "start_mm": [start.x, start.y],
                    "end_mm": [end.x, end.y],
                },
                expected={"length_mm": gap, "remediates_rule_id": "OPEN_CONTOUR"},
            ),
        )

    def _rebuild_fillet(
        self,
        model: DrawingModel,
        arc_entity: EntityRecord,
        feature_id: str,
        radius_mm: float,
    ) -> tuple[Operation, ...]:
        if radius_mm <= 0.0:
            raise InvalidFeatureParametersError(
                "Fillet radius must be positive",
                details={"radius_mm": radius_mm},
            )
        index = next(
            (position for position, item in enumerate(model.entities) if item is arc_entity), None
        )
        if index is None or index == 0 or index == len(model.entities) - 1:
            raise _missing("FILLET_NOT_TANGENT", arc_entity.entity_ref, "fillet.adjacent_lines")
        previous, following = model.entities[index - 1], model.entities[index + 1]
        if not isinstance(previous.geometry, LineGeometry) or not isinstance(
            following.geometry, LineGeometry
        ):
            raise _missing("FILLET_NOT_TANGENT", arc_entity.entity_ref, "fillet.adjacent_lines")
        first_start, first_end = _entity_endpoints(previous)
        second_start, second_end = _entity_endpoints(following)
        vertex = line_intersection(
            first_start,
            first_end,
            second_start,
            second_end,
            self._tolerance,
        )
        if vertex is None:
            raise _missing("FILLET_NOT_TANGENT", arc_entity.entity_ref, "fillet.vertex")
        previous_far = max((first_start, first_end), key=lambda point: point.distance_to(vertex))
        following_far = max((second_start, second_end), key=lambda point: point.distance_to(vertex))
        rebuilt = fillet_vertex(
            previous_far,
            vertex,
            following_far,
            radius_mm,
            self._tolerance,
        )
        previous_property = (
            "StartPoint"
            if first_start.distance_to(vertex) <= first_end.distance_to(vertex)
            else "EndPoint"
        )
        following_property = (
            "StartPoint"
            if second_start.distance_to(vertex) <= second_end.distance_to(vertex)
            else "EndPoint"
        )
        update_previous = self._update(
            previous,
            feature_id,
            "previous-tangent",
            layer=previous.layer,
            properties={previous_property: [rebuilt.tangent_in.x, rebuilt.tangent_in.y, 0.0]},
            expected={"endpoint_mm": list(rebuilt.tangent_in.as_tuple())},
        )
        delete_arc = Operation(
            operation_id=operation_id(feature_id, "delete-old-arc"),
            feature_id=feature_id,
            type=OperationType.DELETE_ENTITY,
            layer=arc_entity.layer,
            target_entity_ref=arc_entity.entity_ref,
            expected={"remediates_rule_id": "FILLET_NOT_TANGENT"},
        )
        update_following = self._update(
            following,
            feature_id,
            "following-tangent",
            layer=following.layer,
            properties={following_property: [rebuilt.tangent_out.x, rebuilt.tangent_out.y, 0.0]},
            expected={"endpoint_mm": list(rebuilt.tangent_out.as_tuple())},
        )
        create_arc = Operation(
            operation_id=operation_id(feature_id, "replacement-arc"),
            feature_id=feature_id,
            type=OperationType.CREATE_ARC,
            layer=arc_entity.layer,
            geometry=_curve_geometry(rebuilt.arc),
            expected={"radius_mm": radius_mm, "tangent": True},
        )
        return update_previous, delete_arc, update_following, create_arc


__all__ = [
    "FindingRef",
    "OperationSource",
    "RemediationResult",
    "RemediationService",
]
