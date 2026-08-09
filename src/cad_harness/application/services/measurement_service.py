"""Read-only analytic measurements over a normalized DrawingModel."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal, assert_never, cast

from pydantic import ValidationError

from cad_harness.application.process_runner import (
    JsonValue,
    ProcessWorkerCommand,
    run_process_worker,
)
from cad_harness.application.timeout import OperationDeadline
from cad_harness.domain.errors import (
    DocumentNotFoundError,
    HarnessError,
    InvalidFeatureParametersError,
)
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    CircleGeometry,
    DrawingModel,
    EntityRecord,
    LineGeometry,
    PolylineGeometry,
)
from cad_harness.domain.models.measurement import (
    SUPPORTED_ENTITY_TYPES,
    MeasurementKind,
    MeasurementRequest,
    MeasurementResult,
)
from cad_harness.geometry.areas import Contour, ContourEdge, CurveContour, LineEdge
from cad_harness.geometry.curves import (
    CurveParams,
    normalize_arc,
    normalize_bulge,
    normalize_circle,
)
from cad_harness.geometry.measure import (
    MeasurableEntity,
    arc_length,
    center_center_distance,
    closed_contour_area,
    contour_length,
    entity_entity_distance,
    entity_set_bounding_box,
    hole_boundary_distance,
    line_angle_deg,
    point_entity_distance,
    point_point_distance,
)
from cad_harness.geometry.primitives import Circle2D, Point2D, Polyline2D
from cad_harness.geometry.tolerance import ToleranceProfile


def _invalid(
    message: str, request: MeasurementRequest, *, details: dict[str, object] | None = None
) -> InvalidFeatureParametersError:
    return InvalidFeatureParametersError(
        message,
        required_action=(
            "Supply the documented points and supported entity types for this measurement"
        ),
        details={
            "measurement_kind": request.kind.value,
            "supported_entity_types": sorted(SUPPORTED_ENTITY_TYPES[request.kind]),
            **(details or {}),
        },
    )


def _entity_kind(entity: EntityRecord) -> str:
    return entity.geometry.kind


def _resolve_entities(
    model: DrawingModel, request: MeasurementRequest, *, expected_count: int | None
) -> tuple[EntityRecord, ...]:
    if expected_count is not None and len(request.entity_refs) != expected_count:
        raise _invalid(
            f"Measurement requires exactly {expected_count} entity reference(s)",
            request,
            details={"received_count": len(request.entity_refs)},
        )
    if expected_count is None and not request.entity_refs:
        raise _invalid("Measurement requires at least one entity reference", request)
    by_ref = {entity.entity_ref: entity for entity in model.entities}
    resolved: list[EntityRecord] = []
    for entity_ref in request.entity_refs:
        entity = by_ref.get(entity_ref)
        if entity is None:
            raise DocumentNotFoundError(
                "Measurement entity reference does not exist in the drawing revision",
                required_action="Read the drawing again and use an entity_ref from that result",
                details={"entity_ref": entity_ref, "revision": model.revision},
            )
        if _entity_kind(entity) not in SUPPORTED_ENTITY_TYPES[request.kind]:
            raise _invalid(
                "Measurement is undefined for the supplied entity type",
                request,
                details={
                    "entity_ref": entity_ref,
                    "actual_entity_type": _entity_kind(entity),
                },
            )
        resolved.append(entity)
    return tuple(resolved)


def _kernel_entity(entity: EntityRecord) -> MeasurableEntity:
    geometry = entity.geometry
    if isinstance(geometry, LineGeometry):
        return LineEdge(Point2D(*geometry.start_mm), Point2D(*geometry.end_mm))
    if isinstance(geometry, ArcGeometry):
        return normalize_arc(
            Point2D(*geometry.center_mm),
            geometry.radius_mm,
            geometry.start_angle_deg,
            geometry.end_angle_deg,
        )
    if isinstance(geometry, CircleGeometry):
        return Circle2D(Point2D(*geometry.center_mm), 2.0 * geometry.radius_mm)
    if isinstance(geometry, PolylineGeometry):
        vertices = geometry.vertices
        segment_count = len(vertices) if geometry.closed else max(0, len(vertices) - 1)
        if any(abs(vertices[index].bulge) > 1.0e-15 for index in range(segment_count)):
            edges: list[ContourEdge] = []
            for index in range(segment_count):
                start = Point2D(*vertices[index].point_mm)
                end = Point2D(*vertices[(index + 1) % len(vertices)].point_mm)
                bulge = vertices[index].bulge
                edges.append(
                    LineEdge(start, end)
                    if abs(bulge) <= 1.0e-15
                    else normalize_bulge(start, end, bulge)
                )
            return CurveContour(tuple(edges))
        return Polyline2D(
            tuple(Point2D(*vertex.point_mm) for vertex in vertices),
            closed=geometry.closed,
        )
    raise InvalidFeatureParametersError("Entity geometry is not measurable")


def _point(request: MeasurementRequest, which: str) -> Point2D:
    raw = request.first_point_mm if which == "first" else request.second_point_mm
    if raw is None:
        raise _invalid(f"Measurement requires {which}_point_mm", request)
    return Point2D(*raw)


def _contour_entity(entity: EntityRecord) -> Contour:
    """Return a closed kernel contour or explain the measured open gap."""
    geometry = entity.geometry
    if isinstance(geometry, CircleGeometry):
        return CurveContour((normalize_circle(Point2D(*geometry.center_mm), geometry.radius_mm),))
    if isinstance(geometry, PolylineGeometry) and not geometry.closed:
        gap = Point2D(*geometry.vertices[0].point_mm).distance_to(
            Point2D(*geometry.vertices[-1].point_mm)
        )
        raise InvalidFeatureParametersError(
            "Contour measurement is undefined for an open contour",
            required_action="Close the contour and retry",
            details={"gap_mm": gap, "entity_ref": entity.entity_ref},
        )
    kernel = _kernel_entity(entity)
    if isinstance(kernel, Polyline2D | CurveContour):
        return kernel
    raise InvalidFeatureParametersError("Entity is not a contour")


def _require_role_types(
    request: MeasurementRequest, entities: tuple[EntityRecord, ...], expected: tuple[type, ...]
) -> None:
    for entity, geometry_type in zip(entities, expected, strict=True):
        if not isinstance(entity.geometry, geometry_type):
            raise _invalid(
                "Measurement entity references do not satisfy their required roles",
                request,
                details={
                    "entity_ref": entity.entity_ref,
                    "actual_entity_type": _entity_kind(entity),
                    "required_entity_type": geometry_type.__name__,
                },
            )


def _result(
    model: DrawingModel,
    request: MeasurementRequest,
    tolerance: ToleranceProfile,
    *,
    value: float | tuple[float, float, float, float],
    unit: Literal["mm", "mm2", "deg"],
    basis: tuple[str, ...],
) -> MeasurementResult:
    return MeasurementResult(
        kind=request.kind,
        value=value,
        unit=unit,
        tolerance_used=(
            tolerance.angular_deg
            if unit == "deg"
            else tolerance.area_mm2
            if unit == "mm2"
            else tolerance.absolute_length_mm
        ),
        document_id=model.document_id,
        revision=model.revision,
        measurement_basis=basis,
        entity_refs=request.entity_refs,
    )


class MeasurementService:
    """Pure service: no adapter, approval, persistence or current-document state."""

    def measure(
        self,
        model: DrawingModel,
        request: MeasurementRequest,
        *,
        tolerance: ToleranceProfile,
    ) -> MeasurementResult:
        kind = request.kind
        if not model.geometry_normalized:
            raise _invalid(
                "Measurement requires a DrawingModel normalized to millimetres",
                request,
                details={"geometry_normalized": False, "source_unit_code": model.source_unit_code},
            )
        if kind is MeasurementKind.POINT_TO_POINT:
            if request.entity_refs:
                raise _invalid("Point-to-point measurement does not accept entity_refs", request)
            point_distance = point_point_distance(
                _point(request, "first"), _point(request, "second")
            )
            return _result(
                model,
                request,
                tolerance,
                value=point_distance,
                unit="mm",
                basis=("explicit_point", "explicit_point"),
            )

        count = None if kind is MeasurementKind.BOUNDING_BOX else 1
        if kind in {
            MeasurementKind.ENTITY_TO_ENTITY,
            MeasurementKind.ANGLE_BETWEEN_LINES,
            MeasurementKind.HOLE_TO_EDGE,
            MeasurementKind.HOLE_CENTER_TO_CENTER,
        }:
            count = 2
        entities = _resolve_entities(model, request, expected_count=count)
        kernel = tuple(_kernel_entity(entity) for entity in entities)
        value: float | tuple[float, float, float, float]
        unit: Literal["mm", "mm2", "deg"]
        basis: tuple[str, ...]

        if kind is MeasurementKind.POINT_TO_ENTITY:
            value = point_entity_distance(_point(request, "first"), kernel[0], tolerance)
            unit, basis = "mm", ("explicit_point", "nearest_point")
        elif kind is MeasurementKind.ENTITY_TO_ENTITY:
            value = entity_entity_distance(kernel[0], kernel[1], tolerance)
            unit, basis = "mm", ("nearest_point", "nearest_point")
        elif kind is MeasurementKind.ANGLE_BETWEEN_LINES:
            first, second = kernel
            assert isinstance(first, LineEdge) and isinstance(second, LineEdge)
            value = line_angle_deg(first.start, first.end, second.start, second.end, tolerance)
            unit, basis = "deg", ("line_direction", "line_direction")
        elif kind is MeasurementKind.ARC_LENGTH:
            curve = kernel[0]
            assert isinstance(curve, CurveParams)
            value = arc_length(curve)
            unit, basis = "mm", ("arc_parameterization",)
        elif kind is MeasurementKind.CONTOUR_PERIMETER:
            value = contour_length(_contour_entity(entities[0]))
            unit, basis = "mm", ("closed_boundary",)
        elif kind is MeasurementKind.CONTOUR_AREA:
            value = closed_contour_area(_contour_entity(entities[0]))
            unit, basis = "mm2", ("closed_boundary",)
        elif kind is MeasurementKind.DIAMETER:
            source = entities[0].geometry
            assert isinstance(source, ArcGeometry | CircleGeometry)
            value = source.radius_mm * 2.0
            unit, basis = "mm", ("circle_or_arc_center",)
        elif kind is MeasurementKind.RADIUS:
            source = entities[0].geometry
            assert isinstance(source, ArcGeometry | CircleGeometry)
            value = source.radius_mm
            unit, basis = "mm", ("circle_or_arc_center",)
        elif kind is MeasurementKind.BOUNDING_BOX:
            box = entity_set_bounding_box(kernel, tolerance)
            value = (box.min_x, box.min_y, box.max_x, box.max_y)
            unit, basis = "mm", ("entity_extents",)
        elif kind is MeasurementKind.HOLE_TO_EDGE:
            _require_role_types(request, entities, (CircleGeometry, PolylineGeometry))
            hole, boundary = kernel
            assert isinstance(hole, Circle2D)
            assert isinstance(boundary, Polyline2D | CurveContour)
            value = hole_boundary_distance(hole, boundary, tolerance)
            unit, basis = "mm", ("hole_boundary", "nearest_outline_point")
        elif kind is MeasurementKind.HOLE_CENTER_TO_CENTER:
            _require_role_types(request, entities, (CircleGeometry, CircleGeometry))
            first, second = kernel
            assert isinstance(first, Circle2D) and isinstance(second, Circle2D)
            value = center_center_distance(first, second)
            unit, basis = "mm", ("center", "center")
        else:  # pragma: no cover - exhaustive enum guard
            assert_never(kind)
        return _result(model, request, tolerance, value=value, unit=unit, basis=basis)

    def measure_cancellable(
        self,
        model: DrawingModel,
        request: MeasurementRequest,
        *,
        tolerance: ToleranceProfile,
        deadline: OperationDeadline,
    ) -> MeasurementResult:
        """Killable application boundary without polluting the pure result model."""
        deadline.checkpoint()
        result = run_process_worker(
            deadline,
            ProcessWorkerCommand.MEASURE,
            {
                "model": cast(JsonValue, model.model_dump(mode="json")),
                "request": cast(JsonValue, request.model_dump(mode="json")),
                "tolerance": cast(JsonValue, asdict(tolerance)),
            },
        )
        deadline.checkpoint()
        try:
            return MeasurementResult.model_validate(result.get("measurement"))
        except ValidationError as exc:
            raise HarnessError(
                "Isolated measurement worker returned an invalid result",
                details={"command": ProcessWorkerCommand.MEASURE.value},
            ) from exc


__all__ = ["MeasurementService"]
