"""Properties 44-54 for deterministic material take-off."""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.property.strategies import (
    _circle_record,
    _drawing_model,
    _line_record,
    _polyline_record,
)

from cad_harness.company_rules.material_loader import load_material_table
from cad_harness.comprehension.takeoff import compute_takeoff
from cad_harness.domain.errors import MissingRequiredInputsError
from cad_harness.domain.models.drawing_model import DrawingModel, EntityRecord
from cad_harness.domain.models.takeoff import PartInput, TakeoffReport, TakeoffRequest
from cad_harness.geometry.tolerance import ToleranceProfile

MATERIALS = load_material_table("demo-materials@1.0")
TOLERANCE = ToleranceProfile(id="takeoff", version="1.0")


def _request(
    model: DrawingModel,
    *,
    thickness: float = 10.0,
    quantity: int = 2,
    material: str = "SS400",
    allowance: float | None = None,
    weld_edges: tuple[str, ...] = (),
) -> TakeoffRequest:
    return TakeoffRequest(
        document_id=model.document_id,
        parts=(
            PartInput(
                part_code="P-001",
                outline_entity_ref="outline",
                thickness_mm=thickness,
                material_code=material,
                quantity=quantity,
                stock_allowance_mm=allowance,
            ),
        ),
        weld_edges=weld_edges,
        material_profile_ref="demo-materials@1.0",
    )


def _square(ref: str, side: float, *, center: tuple[float, float] = (0.0, 0.0)) -> EntityRecord:
    x, y = center
    half = side / 2.0
    return _polyline_record(
        ref,
        ((x - half, y - half), (x + half, y - half), (x + half, y + half), (x - half, y + half)),
        closed=True,
    )


@given(
    side=st.floats(min_value=50.0, max_value=500.0, allow_nan=False),
    ratio=st.floats(min_value=0.2, max_value=0.75, allow_nan=False),
    depth=st.integers(min_value=0, max_value=3),
)
def test_property_44_net_area_alternates_by_nesting_depth(
    side: float, ratio: float, depth: int
) -> None:
    entities: list[EntityRecord] = []
    expected = 0.0
    current = side
    for level in range(depth + 1):
        entities.append(_square("outline" if level == 0 else f"nested-{level}", current))
        expected += current**2 * (1.0 if level % 2 == 0 else -1.0)
        current *= ratio
    model = _drawing_model(tuple(entities))
    line = compute_takeoff(model, _request(model), materials=MATERIALS, tolerance=TOLERANCE).parts[
        0
    ]
    assert math.isclose(line.net_area_mm2, expected, rel_tol=1.0e-10, abs_tol=1.0e-7)


@given(
    width=st.floats(min_value=10.0, max_value=500.0, allow_nan=False),
    height=st.floats(min_value=10.0, max_value=500.0, allow_nan=False),
    thickness=st.floats(min_value=0.5, max_value=500.0, allow_nan=False),
    quantity=st.integers(min_value=1, max_value=100_000),
)
def test_property_45_mass_matches_decimal_reference_and_half_up(
    width: float, height: float, thickness: float, quantity: int
) -> None:
    model = _drawing_model(
        (
            _polyline_record(
                "outline", ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height)), closed=True
            ),
        )
    )
    line = compute_takeoff(
        model,
        _request(model, thickness=thickness, quantity=quantity),
        materials=MATERIALS,
        tolerance=TOLERANCE,
    ).parts[0]
    raw = Decimal(str(width * height)) * Decimal(str(thickness)) * Decimal("7850") / Decimal("1e9")
    total_raw = raw * Decimal(quantity)
    assert math.isclose(line.unit_mass_kg_raw, float(raw), rel_tol=0.0, abs_tol=1.0e-12)
    assert line.unit_mass_kg == float(raw.quantize(Decimal(".001"), rounding=ROUND_HALF_UP))
    assert line.total_mass_kg == float(total_raw.quantize(Decimal(".001"), rounding=ROUND_HALF_UP))
    assert len(line.unit_mass_kg_raw_text.partition(".")[2]) >= 6
    assert Decimal(line.unit_mass_kg_raw_text) == raw
    assert len(line.total_mass_kg_raw_text.partition(".")[2]) >= 6
    assert Decimal(line.total_mass_kg_raw_text) == total_raw


@pytest.mark.parametrize(
    ("field", "value", "path"),
    [
        ("thickness", 0.499, "parts[0].thickness_mm"),
        ("thickness", 500.001, "parts[0].thickness_mm"),
        ("quantity", 0, "parts[0].quantity"),
        ("quantity", 100_001, "parts[0].quantity"),
        ("allowance", -0.001, "parts[0].stock_allowance_mm"),
        ("allowance", 500.001, "parts[0].stock_allowance_mm"),
        ("material", "UNKNOWN", "parts[0].material_code"),
    ],
)
def test_property_46_invalid_inputs_are_never_clamped_or_defaulted(
    field: str, value: object, path: str
) -> None:
    model = _drawing_model((_square("outline", 100.0),))
    arguments: dict[str, object] = {}
    arguments[field] = value
    with pytest.raises(MissingRequiredInputsError) as excinfo:
        compute_takeoff(
            model,
            _request(model, **arguments),  # type: ignore[arg-type]
            materials=MATERIALS,
            tolerance=TOLERANCE,
        )
    assert excinfo.value.details["path"] == path
    assert "actual" in excinfo.value.details


@given(
    hole_radius=st.floats(min_value=1.0, max_value=10.0, allow_nan=False),
    quantity=st.integers(min_value=1, max_value=100),
)
def test_properties_47_49_52_report_provenance_partition_and_hole_metamorphism(
    hole_radius: float, quantity: int
) -> None:
    outline = _square("outline", 100.0)
    without_model = _drawing_model((outline,))
    with_model = _drawing_model((outline, _circle_record("hole", (0.0, 0.0), hole_radius)))
    without = compute_takeoff(
        without_model,
        _request(without_model, quantity=quantity),
        materials=MATERIALS,
        tolerance=TOLERANCE,
    ).parts[0]
    report = compute_takeoff(
        with_model,
        _request(with_model, quantity=quantity),
        materials=MATERIALS,
        tolerance=TOLERANCE,
    )
    line = report.parts[0]
    hole_area = math.pi * hole_radius**2
    hole_perimeter = 2.0 * math.pi * hole_radius
    assert math.isclose(without.net_area_mm2 - line.net_area_mm2, hole_area, abs_tol=1.0e-8)
    assert math.isclose(line.cut_length_mm, line.outer_cut_length_mm + line.inner_cut_length_mm)
    assert math.isclose(line.cut_length_mm - without.cut_length_mm, hole_perimeter, abs_tol=1.0e-8)
    assert line.pierce_count == without.pierce_count + 1
    assert line.hole_groups[0].count == 1
    source_refs = {entity.entity_ref for entity in with_model.entities}
    assert all(refs and set(refs) <= source_refs for refs in line.evidence.values())
    assert report.document_id == with_model.document_id
    assert report.revision == with_model.revision
    assert report.material_profile_id == MATERIALS.profile_id
    assert report.material_profile_version == MATERIALS.version
    assert all(key in report.units for key in line.evidence)


@given(
    radii=st.lists(st.floats(min_value=1.0, max_value=8.0, allow_nan=False), min_size=1, max_size=8)
)
def test_property_50_hole_grouping_is_complete_tolerant_and_sorted(radii: list[float]) -> None:
    ordered = sorted(radii)
    centers = tuple(((-140.0 + index * 40.0), 0.0) for index in range(len(ordered)))
    entities = (
        _square("outline", 400.0),
        *(
            _circle_record(f"hole-{index}", center, radius)
            for index, (center, radius) in enumerate(zip(centers, ordered, strict=True))
        ),
    )
    model = _drawing_model(entities)
    groups = (
        compute_takeoff(model, _request(model), materials=MATERIALS, tolerance=TOLERANCE)
        .parts[0]
        .hole_groups
    )
    assert sum(group.count for group in groups) == len(radii)
    assert [group.diameter_mm for group in groups] == sorted(group.diameter_mm for group in groups)


@given(length=st.floats(min_value=1.0, max_value=80.0, allow_nan=False))
def test_property_51_weld_length_uses_only_declared_edges(length: float) -> None:
    weld = _line_record("weld", (-length / 2.0, 0.0), (length / 2.0, 0.0))
    model = _drawing_model((_square("outline", 100.0), weld))
    undeclared = compute_takeoff(
        model, _request(model), materials=MATERIALS, tolerance=TOLERANCE
    ).parts[0]
    declared = compute_takeoff(
        model,
        _request(model, weld_edges=("weld",)),
        materials=MATERIALS,
        tolerance=TOLERANCE,
    ).parts[0]
    assert undeclared.weld_length_mm == 0.0
    assert math.isclose(declared.weld_length_mm, length)


def test_property_48_overlapping_holes_are_excluded_completely() -> None:
    model = _drawing_model(
        (
            _square("outline", 100.0),
            _circle_record("hole-a", (-2.0, 0.0), 5.0),
            _circle_record("hole-b", (2.0, 0.0), 5.0),
        )
    )
    report = compute_takeoff(model, _request(model), materials=MATERIALS, tolerance=TOLERANCE)
    line = report.parts[0]
    assert math.isclose(line.net_area_mm2, 10_000.0)
    assert line.inner_cut_length_mm == 0.0
    assert line.pierce_count == 1
    assert line.hole_groups == ()
    assert [finding.rule_id for finding in report.excluded_contours] == ["OVERLAPPING_HOLES"]

    outside_model = _drawing_model(
        (_square("outline", 100.0), _circle_record("outside-hole", (48.0, 0.0), 5.0))
    )
    outside_report = compute_takeoff(
        outside_model,
        _request(outside_model),
        materials=MATERIALS,
        tolerance=TOLERANCE,
    )
    assert math.isclose(outside_report.parts[0].net_area_mm2, 10_000.0)
    outside_finding = next(
        finding
        for finding in outside_report.excluded_contours
        if finding.rule_id == "HOLE_OUTSIDE_PART"
    )
    assert outside_finding.measurement["outside_area_mm2"] > 0.0

    concave_outline = _polyline_record(
        "outline",
        ((-50.0, -50.0), (50.0, -50.0), (50.0, -10.0), (0.0, -10.0), (0.0, 50.0), (-50.0, 50.0)),
        closed=True,
    )
    concave_model = _drawing_model(
        (concave_outline, _circle_record("concave-outside", (1.0, -9.0), 5.0))
    )
    concave_report = compute_takeoff(
        concave_model,
        _request(concave_model),
        materials=MATERIALS,
        tolerance=TOLERANCE,
    )
    assert any(
        finding.rule_id == "HOLE_OUTSIDE_PART" for finding in concave_report.excluded_contours
    )

    open_model = _drawing_model(
        (_square("outline", 100.0), _line_record("open-cut", (-10.0, 0.0), (10.0, 0.0)))
    )
    open_report = compute_takeoff(
        open_model, _request(open_model), materials=MATERIALS, tolerance=TOLERANCE
    )
    assert math.isclose(open_report.parts[0].net_area_mm2, 10_000.0)
    assert any(finding.rule_id == "OPEN_CONTOUR" for finding in open_report.excluded_contours)


def test_properties_53_54_takeoff_is_pure_deterministic_and_json_round_trips() -> None:
    model = _drawing_model((_square("outline", 100.0), _circle_record("hole", (0.0, 0.0), 4.0)))
    before = model.model_dump(mode="json")
    first = compute_takeoff(model, _request(model), materials=MATERIALS, tolerance=TOLERANCE)
    second = compute_takeoff(model, _request(model), materials=MATERIALS, tolerance=TOLERANCE)
    assert model.model_dump(mode="json") == before
    assert first == second
    assert TakeoffReport.model_validate(first.model_dump(mode="json")) == first
