"""Correctness properties 39-42 for drawing feature recognition."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.property.strategies import (
    RecognitionCase,
    _arc_record,
    _circle_record,
    _drawing_model,
    _line_record,
    _polyline_record,
    recognition_cases,
)

from cad_harness.company_rules.loader import load_profile
from cad_harness.comprehension.recognizer import recognize
from cad_harness.domain.errors import InvalidFeatureParametersError, MissingRequiredInputsError
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    CircleGeometry,
    LineGeometry,
    MeasuredValue,
    PolylineGeometry,
)
from cad_harness.domain.models.operation_plan import OperationType
from cad_harness.domain.models.recognition import (
    RecognitionReport,
    RecognizedFeature,
    RecognizedFeatureType,
)
from cad_harness.feature_catalog import CompileContext, get_compiler, supported_types
from cad_harness.geometry.curves import normalize_arc
from cad_harness.geometry.patterns import slot_end_arcs, slot_outline
from cad_harness.geometry.primitives import Point2D
from cad_harness.geometry.tolerance import ToleranceProfile

PROFILE = load_profile("demo-profile")
TOLERANCE = PROFILE.tolerance()


def _all_features(report: RecognitionReport) -> tuple[RecognizedFeature, ...]:
    return (
        *report.features,
        *(candidate.feature for group in report.ambiguous_groups for candidate in group),
    )


def _recognized(case: RecognitionCase) -> RecognizedFeature:
    report = recognize(case.model, tolerance=TOLERANCE, profile=PROFILE)
    return next(
        feature for feature in _all_features(report) if feature.feature_type is case.expected_type
    )


def test_polygonized_circle_is_not_misclassified_as_many_chamfers() -> None:
    vertices = tuple(
        (
            100.0 + 40.0 * math.cos(2.0 * math.pi * index / 128),
            75.0 + 40.0 * math.sin(2.0 * math.pi * index / 128),
        )
        for index in range(128)
    )
    model = _drawing_model((_polyline_record("tessellated-circle", vertices, closed=True),))

    report = recognize(model, tolerance=TOLERANCE, profile=PROFILE)

    assert all(
        feature.feature_type is not RecognizedFeatureType.CHAMFER_CORNER
        for feature in _all_features(report)
    )


@given(case=recognition_cases())
def test_property_39_recognition_recovers_synthetic_parameters(
    case: RecognitionCase,
) -> None:
    feature = _recognized(case)
    for name, expected in case.expected_parameters.items():
        assert TOLERANCE.length_close(feature.parameters[name].value, expected)


@given(
    gap=st.floats(min_value=0.1, max_value=50.0, allow_nan=False, allow_infinity=False),
    center_x=st.floats(min_value=-100.0, max_value=100.0, allow_nan=False),
    center_y=st.floats(min_value=-100.0, max_value=100.0, allow_nan=False),
    pitch=st.floats(min_value=10.0, max_value=80.0, allow_nan=False),
    hole_radius=st.floats(min_value=0.5, max_value=4.0, allow_nan=False),
)
def test_property_40_ambiguity_and_open_contours_are_explicit(
    gap: float,
    center_x: float,
    center_y: float,
    pitch: float,
    hole_radius: float,
) -> None:
    half = pitch / 2.0
    outline = _polyline_record(
        "outline",
        (
            (center_x - pitch, center_y - pitch),
            (center_x + pitch, center_y - pitch),
            (center_x + pitch, center_y + pitch),
            (center_x - pitch, center_y + pitch),
        ),
        closed=True,
    )
    centers = (
        (center_x - half, center_y - half),
        (center_x + half, center_y - half),
        (center_x - half, center_y + half),
        (center_x + half, center_y + half),
    )
    square_holes = tuple(
        _circle_record(f"hole-{index}", center, hole_radius) for index, center in enumerate(centers)
    )
    ambiguous = recognize(
        _drawing_model((outline, *square_holes)), tolerance=TOLERANCE, profile=PROFILE
    )
    pattern_group = next(
        group
        for group in ambiguous.ambiguous_groups
        if {candidate.feature.feature_type for candidate in group}
        >= {
            RecognizedFeatureType.RECTANGULAR_HOLE_PATTERN,
            RecognizedFeatureType.BOLT_CIRCLE_PATTERN,
        }
    )
    assert {candidate.feature.feature_type for candidate in pattern_group} == {
        RecognizedFeatureType.RECTANGULAR_HOLE_PATTERN,
        RecognizedFeatureType.BOLT_CIRCLE_PATTERN,
    }
    assert all(
        candidate.feature.entity_refs == pattern_group[0].feature.entity_refs
        for candidate in pattern_group
    )
    assert "best_match" not in type(pattern_group[0]).model_fields

    slot_center = Point2D(center_x, center_y)
    slot_width = 2.0 * hole_radius
    slot_length = max(pitch, slot_width + 2.0)
    slot_points = slot_outline(slot_center, slot_length, slot_width, 0.0)
    right_arc, left_arc = slot_end_arcs(slot_points, slot_width)
    top_left, top_right, bottom_right, bottom_left = slot_points
    slot_report = recognize(
        _drawing_model(
            (
                _line_record("slot-top", top_left.as_tuple(), top_right.as_tuple()),
                _arc_record("slot-right", right_arc),
                _line_record("slot-bottom", bottom_right.as_tuple(), bottom_left.as_tuple()),
                _arc_record("slot-left", left_arc),
            )
        ),
        tolerance=TOLERANCE,
        profile=PROFILE,
    )
    slot_group = next(
        group
        for group in slot_report.ambiguous_groups
        if {candidate.feature.feature_type for candidate in group}
        == {RecognizedFeatureType.PART_OUTLINE, RecognizedFeatureType.SLOT}
    )
    assert all(
        candidate.feature.entity_refs == slot_group[0].feature.entity_refs
        for candidate in slot_group
    )

    open_model = _drawing_model(
        (_polyline_record("open", ((0.0, 0.0), (10.0, 0.0), (10.0, gap)), closed=False),)
    )
    open_report = recognize(open_model, tolerance=TOLERANCE, profile=PROFILE)
    assert len(open_report.open_contours) == 1
    assert TOLERANCE.length_close(open_report.open_contours[0].gap_mm, (100.0 + gap**2) ** 0.5)
    assert not any(
        feature.feature_type is RecognizedFeatureType.PART_OUTLINE
        for feature in _all_features(open_report)
    )


@given(case=recognition_cases())
def test_property_41_recognition_never_infers_technical_values(
    case: RecognitionCase,
) -> None:
    feature = _recognized(case)
    assert not {"thickness_mm", "material_code", "tolerance_class", "standard"}.intersection(
        feature.parameters
    )
    assert all(
        measured.provenance == "measured" and measured.entity_refs
        for measured in feature.parameters.values()
    )


def _assert_points_match(
    actual: list[list[float]], expected: list[tuple[float, float]], tolerance: ToleranceProfile
) -> None:
    assert len(actual) == len(expected)
    unmatched = [Point2D(*point) for point in expected]
    for value in actual:
        point = Point2D(float(value[0]), float(value[1]))
        index = next(
            (
                position
                for position, candidate in enumerate(unmatched)
                if tolerance.is_coincident(point.distance_to(candidate))
            ),
            None,
        )
        assert index is not None
        unmatched.pop(index)
    assert not unmatched


@given(case=recognition_cases())
def test_property_42_every_recognized_type_compile_round_trips(
    case: RecognitionCase,
) -> None:
    feature = _recognized(case)
    spec = feature.to_feature_spec("round-trip")
    compiled = get_compiler(spec.type).compile(
        spec,
        CompileContext(
            profile=PROFILE,
            tolerance=TOLERANCE,
            datum=Point2D(0.0, 0.0),
            source_model=case.model,
        ),
    )
    source_entities = [
        entity for entity in case.model.entities if entity.entity_ref in feature.entity_refs
    ]

    if feature.feature_type is RecognizedFeatureType.PART_OUTLINE:
        source = source_entities[0].geometry
        assert isinstance(source, PolylineGeometry)
        compiled_lines = [
            operation
            for operation in compiled.operations
            if operation.type is OperationType.CREATE_LINE
        ]
        _assert_points_match(
            [operation.geometry["start_mm"] for operation in compiled_lines]
            + [operation.geometry["end_mm"] for operation in compiled_lines],
            [vertex.point_mm for vertex in source.vertices] * 2,
            TOLERANCE,
        )
        return

    if feature.feature_type is RecognizedFeatureType.CIRCULAR_HOLE:
        source = source_entities[0].geometry
        assert isinstance(source, CircleGeometry)
        operation = compiled.operations[0]
        _assert_points_match([operation.geometry["center_mm"]], [source.center_mm], TOLERANCE)
        assert TOLERANCE.length_close(
            float(operation.geometry["diameter_mm"]), 2.0 * source.radius_mm
        )
        return

    if feature.feature_type in {
        RecognizedFeatureType.RECTANGULAR_HOLE_PATTERN,
        RecognizedFeatureType.BOLT_CIRCLE_PATTERN,
    }:
        operation = compiled.operations[0]
        source_centers = []
        for entity in source_entities:
            assert isinstance(entity.geometry, CircleGeometry)
            source_centers.append(entity.geometry.center_mm)
        _assert_points_match(operation.geometry["centers_mm"], source_centers, TOLERANCE)
        assert TOLERANCE.length_close(
            float(operation.geometry["diameter_mm"]),
            2.0 * source_entities[0].geometry.radius_mm,  # type: ignore[union-attr]
        )
        return

    if feature.feature_type is RecognizedFeatureType.CHAMFER_CORNER:
        source = source_entities[0].geometry
        assert isinstance(source, PolylineGeometry)
        edge_index = round(feature.evidence["source_edge_index"])
        start = source.vertices[edge_index].point_mm
        end = source.vertices[(edge_index + 1) % len(source.vertices)].point_mm
        operation = compiled.operations[0]
        _assert_points_match(
            [operation.geometry["start_mm"], operation.geometry["end_mm"]],
            [start, end],
            TOLERANCE,
        )
        return

    source_lines = [
        entity.geometry for entity in source_entities if isinstance(entity.geometry, LineGeometry)
    ]
    source_arcs = [
        entity.geometry for entity in source_entities if isinstance(entity.geometry, ArcGeometry)
    ]
    compiled_lines = [
        operation
        for operation in compiled.operations
        if operation.type is OperationType.CREATE_LINE
    ]
    compiled_arcs = [
        operation for operation in compiled.operations if operation.type is OperationType.CREATE_ARC
    ]
    if feature.feature_type is RecognizedFeatureType.FILLET_CORNER:
        assert len(compiled_arcs) == 1
        source_arc = next(
            arc
            for arc in source_arcs
            if TOLERANCE.length_close(arc.radius_mm, feature.parameters["radius_mm"].value)
        )
        operation = compiled_arcs[0]
        _assert_points_match([operation.geometry["center_mm"]], [source_arc.center_mm], TOLERANCE)
        assert TOLERANCE.length_close(float(operation.geometry["radius_mm"]), source_arc.radius_mm)
        return

    assert len(source_lines) == len(compiled_lines) == 2
    assert len(source_arcs) == len(compiled_arcs) == 2
    _assert_points_match(
        [operation.geometry["start_mm"] for operation in compiled_lines]
        + [operation.geometry["end_mm"] for operation in compiled_lines],
        [line.start_mm for line in source_lines] + [line.end_mm for line in source_lines],
        TOLERANCE,
    )
    _assert_points_match(
        [operation.geometry["center_mm"] for operation in compiled_arcs],
        [arc.center_mm for arc in source_arcs],
        TOLERANCE,
    )


@pytest.mark.parametrize(
    "feature_type",
    [
        RecognizedFeatureType.CIRCULAR_HOLE,
        RecognizedFeatureType.FILLET_CORNER,
        RecognizedFeatureType.CHAMFER_CORNER,
    ],
)
def test_source_bound_specs_are_hidden_and_require_a_trusted_model(
    feature_type: RecognizedFeatureType,
) -> None:
    feature = RecognizedFeature(
        feature_type=feature_type,
        source_revision="rev-1",
        entity_refs=("source",),
        parameters={},
    )
    spec = feature.to_feature_spec("source-bound")
    assert spec.type not in supported_types()
    with pytest.raises(MissingRequiredInputsError):
        get_compiler(spec.type).compile(spec, CompileContext(profile=PROFILE, tolerance=TOLERANCE))


def test_measured_values_cannot_be_overridden() -> None:
    feature = RecognizedFeature(
        feature_type=RecognizedFeatureType.SLOT,
        source_revision="rev-1",
        entity_refs=("source",),
        parameters={
            name: MeasuredValue(
                value=value,
                unit="deg" if name == "angle_deg" else "mm",
                provenance="measured",
                entity_refs=("source",),
            )
            for name, value in {
                "length_mm": 20.0,
                "width_mm": 5.0,
                "center_x_mm": 0.0,
                "center_y_mm": 0.0,
                "angle_deg": 0.0,
            }.items()
        },
    )
    with pytest.raises(InvalidFeatureParametersError):
        feature.to_feature_spec("slot", user_supplied={"length_mm": 25.0})


@pytest.mark.parametrize("reserved", ["source_revision", "source_entity_refs", "source_edge_index"])
def test_source_bound_specs_reject_all_caller_parameters(reserved: str) -> None:
    feature = RecognizedFeature(
        feature_type=RecognizedFeatureType.CIRCULAR_HOLE,
        source_revision="rev-1",
        entity_refs=("source",),
        parameters={},
    )
    with pytest.raises(InvalidFeatureParametersError):
        feature.to_feature_spec("hole", user_supplied={reserved: "rebound"})


def test_equal_diameter_holes_from_separate_parts_never_form_one_pattern() -> None:
    first_outline = _polyline_record(
        "part-a", ((0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)), closed=True
    )
    second_outline = _polyline_record(
        "part-b", ((60.0, 0.0), (100.0, 0.0), (100.0, 40.0), (60.0, 40.0)), closed=True
    )
    holes = (
        _circle_record("a-1", (10.0, 10.0), 2.0),
        _circle_record("a-2", (30.0, 30.0), 2.0),
        _circle_record("b-1", (70.0, 10.0), 2.0),
        _circle_record("b-2", (90.0, 30.0), 2.0),
    )
    report = recognize(
        _drawing_model((first_outline, second_outline, *holes)),
        tolerance=TOLERANCE,
        profile=PROFILE,
    )
    assert not any(
        feature.feature_type
        in {
            RecognizedFeatureType.RECTANGULAR_HOLE_PATTERN,
            RecognizedFeatureType.BOLT_CIRCLE_PATTERN,
        }
        for feature in _all_features(report)
    )


def test_two_arcs_and_two_non_tangent_lines_are_not_a_slot() -> None:
    first_arc = normalize_arc(Point2D(0.0, 0.0), 1.0, 270.0, 90.0)
    second_arc = normalize_arc(Point2D(10.0, 2.0), 1.0, 90.0, 270.0)
    entities = (
        _line_record("top", (0.0, 1.0), (10.0, 3.0)),
        _arc_record("right", second_arc),
        _line_record("bottom", (10.0, 1.0), (0.0, -1.0)),
        _arc_record("left", first_arc),
    )
    report = recognize(_drawing_model(entities), tolerance=TOLERANCE, profile=PROFILE)
    assert not any(
        feature.feature_type is RecognizedFeatureType.SLOT for feature in _all_features(report)
    )
