"""Properties 53 and 65 for the read-only measurement service."""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.application.services.measurement_service import MeasurementService
from cad_harness.domain.errors import DocumentNotFoundError, InvalidFeatureParametersError
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    CircleGeometry,
    DrawingModel,
    EllipseGeometry,
    EntityRecord,
    LineGeometry,
    PolylineGeometry,
    PolylineVertex,
    ReadScope,
)
from cad_harness.domain.models.measurement import (
    SUPPORTED_ENTITY_TYPES,
    MeasurementKind,
    MeasurementRequest,
    MeasurementResult,
)
from cad_harness.geometry.tolerance import DEMO_TOLERANCE


def _entity(ref: str, geometry: object) -> EntityRecord:
    return EntityRecord.model_validate(
        {
            "entity_ref": ref,
            "entity_type": f"AcDb{type(geometry).__name__.removesuffix('Geometry')}",
            "layer": "OBJECT",
            "visible": True,
            "space": "model",
            "geometry": geometry,
            "bounding_box_mm": (0.0, 0.0, 1.0, 1.0),
        }
    )


def _model(scale: float = 1.0) -> DrawingModel:
    square = tuple(
        PolylineVertex(point_mm=(x * scale, y * scale))
        for x, y in ((0.0, 0.0), (8.0, 0.0), (8.0, 8.0), (0.0, 8.0))
    )
    open_path = tuple(
        PolylineVertex(point_mm=(x * scale, y * scale))
        for x, y in ((0.0, 0.0), (4.0, 0.0), (4.0, 3.0))
    )
    entities = (
        _entity("line-x", LineGeometry(start_mm=(0.0, 0.0), end_mm=(10 * scale, 0.0))),
        _entity("line-y", LineGeometry(start_mm=(0.0, 0.0), end_mm=(0.0, 8 * scale))),
        _entity(
            "line-parallel",
            LineGeometry(start_mm=(0.0, 4 * scale), end_mm=(10 * scale, 4 * scale)),
        ),
        _entity(
            "arc",
            ArcGeometry(
                center_mm=(0.0, 0.0),
                radius_mm=2 * scale,
                start_angle_deg=0.0,
                end_angle_deg=90.0,
            ),
        ),
        _entity(
            "hole",
            CircleGeometry(center_mm=(2 * scale, 2 * scale), radius_mm=0.5 * scale),
        ),
        _entity(
            "hole-far",
            CircleGeometry(center_mm=(5 * scale, 6 * scale), radius_mm=0.75 * scale),
        ),
        _entity("outline", PolylineGeometry(vertices=square, closed=True)),
        _entity("open", PolylineGeometry(vertices=open_path, closed=False)),
        _entity(
            "ellipse",
            EllipseGeometry(
                center_mm=(0.0, 0.0),
                major_axis_mm=3 * scale,
                minor_axis_mm=2 * scale,
                rotation_deg=0.0,
            ),
        ),
    )
    return DrawingModel(
        document_id="doc-measure",
        revision="sha256:measurement-r1",
        display_name="measurement.dxf",
        source_unit_code="mm",
        to_mm_factor=1.0,
        geometry_normalized=True,
        scope=ReadScope(),
        entities=entities,
        arc_chord_tolerance_mm=DEMO_TOLERANCE.arc_chord_tolerance_mm,
    )


def _requests(
    scale: float,
) -> tuple[tuple[MeasurementRequest, float | tuple[float, ...], str], ...]:
    return (
        (
            MeasurementRequest(
                kind=MeasurementKind.POINT_TO_POINT,
                first_point_mm=(0.0, 0.0),
                second_point_mm=(3 * scale, 4 * scale),
            ),
            5 * scale,
            "mm",
        ),
        (
            MeasurementRequest(
                kind=MeasurementKind.POINT_TO_ENTITY,
                entity_refs=("line-x",),
                first_point_mm=(5 * scale, 3 * scale),
            ),
            3 * scale,
            "mm",
        ),
        (
            MeasurementRequest(
                kind=MeasurementKind.ENTITY_TO_ENTITY,
                entity_refs=("line-x", "line-parallel"),
            ),
            4 * scale,
            "mm",
        ),
        (
            MeasurementRequest(
                kind=MeasurementKind.ANGLE_BETWEEN_LINES,
                entity_refs=("line-x", "line-y"),
            ),
            90.0,
            "deg",
        ),
        (
            MeasurementRequest(kind=MeasurementKind.ARC_LENGTH, entity_refs=("arc",)),
            math.pi * scale,
            "mm",
        ),
        (
            MeasurementRequest(kind=MeasurementKind.CONTOUR_PERIMETER, entity_refs=("outline",)),
            32 * scale,
            "mm",
        ),
        (
            MeasurementRequest(kind=MeasurementKind.CONTOUR_AREA, entity_refs=("outline",)),
            64 * scale * scale,
            "mm2",
        ),
        (
            MeasurementRequest(kind=MeasurementKind.DIAMETER, entity_refs=("hole",)),
            scale,
            "mm",
        ),
        (
            MeasurementRequest(kind=MeasurementKind.RADIUS, entity_refs=("arc",)),
            2 * scale,
            "mm",
        ),
        (
            MeasurementRequest(kind=MeasurementKind.BOUNDING_BOX, entity_refs=("hole",)),
            (1.5 * scale, 1.5 * scale, 2.5 * scale, 2.5 * scale),
            "mm",
        ),
        (
            MeasurementRequest(kind=MeasurementKind.HOLE_TO_EDGE, entity_refs=("hole", "outline")),
            1.5 * scale,
            "mm",
        ),
        (
            MeasurementRequest(
                kind=MeasurementKind.HOLE_CENTER_TO_CENTER,
                entity_refs=("hole", "hole-far"),
            ),
            5 * scale,
            "mm",
        ),
    )


def _assert_value_close(
    actual: float | tuple[float, ...],
    expected: float | tuple[float, ...],
    unit: str,
) -> None:
    tolerance = {
        "mm": DEMO_TOLERANCE.absolute_length_mm,
        "mm2": DEMO_TOLERANCE.area_mm2,
        "deg": DEMO_TOLERANCE.angular_deg,
    }[unit]
    if isinstance(expected, tuple):
        assert isinstance(actual, tuple)
        assert actual == pytest.approx(expected, abs=tolerance)
    else:
        assert isinstance(actual, float)
        assert actual == pytest.approx(expected, abs=tolerance)


# Feature: cad-ai-production-roadmap, Property 65
@given(scale=st.floats(min_value=0.5, max_value=100.0, allow_nan=False, allow_infinity=False))
@settings(max_examples=40, deadline=None)
def test_every_measurement_matches_analytic_value_and_has_provenance(scale: float) -> None:
    """**Validates: Requirements 23.1, 23.2, 23.3, 23.6.**"""
    model = _model(scale)
    service = MeasurementService()

    cases = _requests(scale)
    assert {request.kind for request, _, _ in cases} == set(MeasurementKind)
    for request, expected, unit in cases:
        result = service.measure(model, request, tolerance=DEMO_TOLERANCE)
        _assert_value_close(result.value, expected, unit)
        assert result.unit == unit
        expected_tolerance = (
            DEMO_TOLERANCE.angular_deg
            if request.kind is MeasurementKind.ANGLE_BETWEEN_LINES
            else DEMO_TOLERANCE.area_mm2
            if request.kind is MeasurementKind.CONTOUR_AREA
            else DEMO_TOLERANCE.absolute_length_mm
        )
        assert result.tolerance_used == expected_tolerance
        assert (result.document_id, result.revision) == (model.document_id, model.revision)
        assert result.measurement_basis
        assert result.entity_refs == request.entity_refs


# Feature: cad-ai-production-roadmap, Property 53
@given(scale=st.floats(min_value=0.5, max_value=100.0, allow_nan=False, allow_infinity=False))
@settings(max_examples=30, deadline=None)
def test_measurement_service_is_pure_and_bitwise_deterministic(scale: float) -> None:
    """**Validates: Requirements 23.4, 23.8.**"""
    model = _model(scale)
    before = model.model_dump_json()
    service = MeasurementService()
    for request, _, _ in _requests(scale):
        first = service.measure(model, request, tolerance=DEMO_TOLERANCE)
        second = service.measure(model, request, tolerance=DEMO_TOLERANCE)
        assert first.model_dump_json() == second.model_dump_json()
    assert model.model_dump_json() == before


def test_undefined_measurements_return_evidence_rich_errors() -> None:
    model = _model()
    service = MeasurementService()
    unsupported = MeasurementRequest(kind=MeasurementKind.ARC_LENGTH, entity_refs=("line-x",))
    with pytest.raises(InvalidFeatureParametersError) as unsupported_error:
        service.measure(model, unsupported, tolerance=DEMO_TOLERANCE)
    assert unsupported_error.value.details["supported_entity_types"] == sorted(
        SUPPORTED_ENTITY_TYPES[MeasurementKind.ARC_LENGTH]
    )

    missing = MeasurementRequest(kind=MeasurementKind.RADIUS, entity_refs=("missing-ref",))
    with pytest.raises(DocumentNotFoundError) as missing_error:
        service.measure(model, missing, tolerance=DEMO_TOLERANCE)
    assert missing_error.value.details["entity_ref"] == "missing-ref"

    open_area = MeasurementRequest(kind=MeasurementKind.CONTOUR_AREA, entity_refs=("open",))
    with pytest.raises(InvalidFeatureParametersError) as open_error:
        service.measure(model, open_area, tolerance=DEMO_TOLERANCE)
    assert open_error.value.details["gap_mm"] == pytest.approx(5.0)

    coincident_but_open = _entity(
        "open-zero-gap",
        PolylineGeometry(
            vertices=(
                PolylineVertex(point_mm=(0.0, 0.0)),
                PolylineVertex(point_mm=(1.0, 0.0)),
                PolylineVertex(point_mm=(0.0, 0.0)),
            ),
            closed=False,
        ),
    )
    zero_gap_model = model.model_copy(update={"entities": (*model.entities, coincident_but_open)})
    for kind in (MeasurementKind.CONTOUR_AREA, MeasurementKind.CONTOUR_PERIMETER):
        with pytest.raises(InvalidFeatureParametersError) as zero_gap_error:
            service.measure(
                zero_gap_model,
                MeasurementRequest(kind=kind, entity_refs=("open-zero-gap",)),
                tolerance=DEMO_TOLERANCE,
            )
        assert zero_gap_error.value.details["gap_mm"] == 0.0

    reversed_roles = MeasurementRequest(
        kind=MeasurementKind.HOLE_TO_EDGE, entity_refs=("outline", "hole")
    )
    with pytest.raises(InvalidFeatureParametersError):
        service.measure(model, reversed_roles, tolerance=DEMO_TOLERANCE)


def test_measurement_rejects_drawing_that_is_not_normalized_to_mm() -> None:
    model = _model().model_copy(
        update={"to_mm_factor": None, "geometry_normalized": False, "source_unit_code": "unknown"}
    )
    request = MeasurementRequest(kind=MeasurementKind.RADIUS, entity_refs=("hole",))
    with pytest.raises(InvalidFeatureParametersError) as error:
        MeasurementService().measure(model, request, tolerance=DEMO_TOLERANCE)
    assert error.value.details["geometry_normalized"] is False


def test_circle_and_bulged_contours_use_exact_curve_metrics() -> None:
    model = _model()
    service = MeasurementService()
    circle_area = service.measure(
        model,
        MeasurementRequest(kind=MeasurementKind.CONTOUR_AREA, entity_refs=("hole",)),
        tolerance=DEMO_TOLERANCE,
    )
    circle_perimeter = service.measure(
        model,
        MeasurementRequest(kind=MeasurementKind.CONTOUR_PERIMETER, entity_refs=("hole",)),
        tolerance=DEMO_TOLERANCE,
    )
    assert circle_area.value == pytest.approx(math.pi * 0.25)
    assert circle_perimeter.value == pytest.approx(math.pi)


def test_measurement_result_rejects_malformed_value_shape() -> None:
    with pytest.raises(ValueError):
        MeasurementResult.model_validate(
            {
                "kind": MeasurementKind.BOUNDING_BOX,
                "value": (0.0, 1.0, 2.0),
                "unit": "mm",
                "tolerance_used": DEMO_TOLERANCE.absolute_length_mm,
                "document_id": "doc-measure",
                "revision": "sha256:r1",
                "measurement_basis": ("entity_extents",),
                "entity_refs": ("hole",),
            }
        )
    with pytest.raises(ValueError):
        MeasurementResult(
            kind=MeasurementKind.RADIUS,
            value=(1.0, 2.0, 3.0, 4.0),
            unit="mm",
            tolerance_used=DEMO_TOLERANCE.absolute_length_mm,
            document_id="doc-measure",
            revision="sha256:r1",
            measurement_basis=("circle_or_arc_center",),
            entity_refs=("hole",),
        )
