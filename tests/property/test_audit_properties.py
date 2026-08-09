"""Property 43: drawing-standard audit reports exact profile deviations."""

from __future__ import annotations

import json
from enum import StrEnum

from hypothesis import given
from hypothesis import strategies as st
from tests.property.strategies import (
    DefectiveDrawingModelCase,
    DrawingDefect,
    _circle_record,
    _drawing_model,
    _polyline_record,
    defective_models,
)

from cad_harness.company_rules.loader import CompanyProfile, load_profile
from cad_harness.comprehension.auditor import audit_drawing
from cad_harness.domain.models.document import LayerInfo
from cad_harness.domain.models.drawing_model import (
    DimensionGeometry,
    DrawingModel,
    EntityRecord,
    LineGeometry,
    ReadScope,
    TextGeometry,
)
from cad_harness.domain.models.validation import ValidationStage
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.golden_comparison import (
    GoldenComparisonConfig,
    compare_semantic_entities,
    compare_takeoff_reports,
)
from cad_harness.validation.engine import RuleContext, default_engine


class AuditCase(StrEnum):
    CLEAN = "clean"
    EXTRA_LAYER = "extra_layer"
    MISSING_LAYER = "missing_layer"
    BAD_LAYER_PROPERTY = "bad_layer_property"
    WRONG_ENTITY_LAYER = "wrong_entity_layer"
    BAD_DIMSTYLE = "bad_dimstyle"
    BAD_TEXTSTYLE = "bad_textstyle"
    BAD_UNITS = "bad_units"


EXPECTED_RULE = {
    AuditCase.EXTRA_LAYER: "LAYER_SET_MATCHES_PROFILE",
    AuditCase.MISSING_LAYER: "LAYER_SET_MATCHES_PROFILE",
    AuditCase.BAD_LAYER_PROPERTY: "LAYER_SET_MATCHES_PROFILE",
    AuditCase.WRONG_ENTITY_LAYER: "ENTITY_ON_EXPECTED_LAYER",
    AuditCase.BAD_DIMSTYLE: "DIMSTYLE_IN_PROFILE",
    AuditCase.BAD_TEXTSTYLE: "TEXTSTYLE_IN_PROFILE",
    AuditCase.BAD_UNITS: "DOCUMENT_UNITS_MATCH_PROFILE",
}

EXPECTED_DEFECT_RULE = {
    DrawingDefect.ZERO_LENGTH: "ZERO_LENGTH_ENTITY",
    DrawingDefect.NON_FINITE: "FINITE_COORDINATES",
    DrawingDefect.OPEN_CONTOUR: "OPEN_CONTOUR",
    DrawingDefect.SELF_INTERSECTION: "SELF_INTERSECTING_CONTOUR",
    DrawingDefect.DUPLICATE: "DUPLICATE_ENTITY",
    DrawingDefect.OVERLAP: "OVERLAPPING_ENTITY",
    DrawingDefect.HOLE_OUTSIDE: "HOLE_OUTSIDE_PART",
    DrawingDefect.HOLE_EDGE_DISTANCE: "HOLE_EDGE_DISTANCE_MIN",
    DrawingDefect.HOLE_HOLE_DISTANCE: "HOLE_LIGAMENT_MIN",
    DrawingDefect.INVALID_RADIUS: "INVALID_ARC_RADIUS",
    DrawingDefect.NON_TANGENT_FILLET: "FILLET_NOT_TANGENT",
}


def _profile_layers(profile: CompanyProfile) -> tuple[LayerInfo, ...]:
    return tuple(
        LayerInfo(
            name=layer.name,
            color_index=layer.color_index,
            linetype=layer.linetype,
            lineweight=layer.lineweight,
        )
        for layer in profile.layers
    )


def _clean_model(profile: CompanyProfile) -> DrawingModel:
    return DrawingModel(
        document_id="doc-audit",
        revision="sha256:audit-r1",
        display_name="audit.dxf",
        source_unit_code=profile.canonical_unit,
        to_mm_factor=1.0,
        geometry_normalized=True,
        scope=ReadScope(),
        entities=(
            EntityRecord(
                entity_ref="line-1",
                entity_type="AcDbLine",
                layer="OBJECT",
                visible=True,
                space="model",
                geometry=LineGeometry(start_mm=(0.0, 0.0), end_mm=(20.0, 0.0)),
                bounding_box_mm=(0.0, 0.0, 20.0, 0.0),
            ),
            EntityRecord(
                entity_ref="text-1",
                entity_type="AcDbText",
                layer="TEXT",
                visible=True,
                space="model",
                geometry=TextGeometry(
                    insertion_mm=(0.0, 5.0),
                    height_mm=2.5,
                    text_style=profile.text_style or "",
                    content="NOTE",
                ),
                bounding_box_mm=(0.0, 5.0, 10.0, 7.5),
            ),
            EntityRecord(
                entity_ref="dimension-1",
                entity_type="AcDbDimension",
                layer="DIM",
                visible=True,
                space="model",
                geometry=DimensionGeometry(
                    dimension_type="linear",
                    dimension_style=profile.dimension_style or "",
                    measurement_mm=20.0,
                    text_override=None,
                    measured_entity_refs=("line-1",),
                ),
                bounding_box_mm=(0.0, -5.0, 20.0, 0.0),
            ),
        ),
        layers=_profile_layers(profile),
        dimension_styles=profile.dimension_styles,
        text_styles=profile.text_styles,
        arc_chord_tolerance_mm=0.01,
    )


def _company_compliant(model: DrawingModel, profile: CompanyProfile) -> DrawingModel:
    return model.model_copy(
        update={
            "source_unit_code": profile.canonical_unit,
            "to_mm_factor": 1.0,
            "geometry_normalized": True,
            "layers": _profile_layers(profile),
            "dimension_styles": profile.dimension_styles,
            "text_styles": profile.text_styles,
        }
    )


def _mutate(model: DrawingModel, cases: frozenset[AuditCase]) -> DrawingModel:
    if AuditCase.EXTRA_LAYER in cases:
        model = model.model_copy(
            update={"layers": (*model.layers, LayerInfo(name="VENDOR", color_index=2))}
        )
    if AuditCase.MISSING_LAYER in cases:
        model = model.model_copy(
            update={"layers": tuple(layer for layer in model.layers if layer.name != "HIDDEN")}
        )
    if AuditCase.BAD_LAYER_PROPERTY in cases:
        model = model.model_copy(
            update={
                "layers": tuple(
                    layer.model_copy(update={"color_index": 6}) if layer.name == "OBJECT" else layer
                    for layer in model.layers
                )
            }
        )
    if AuditCase.WRONG_ENTITY_LAYER in cases:
        model = model.model_copy(
            update={
                "entities": (
                    model.entities[0].model_copy(update={"layer": "HIDDEN"}),
                    *model.entities[1:],
                )
            }
        )
    if AuditCase.BAD_DIMSTYLE in cases:
        entity = model.entities[2]
        geometry = entity.geometry.model_copy(update={"dimension_style": "VENDOR-DIM"})
        model = model.model_copy(
            update={
                "entities": (*model.entities[:2], entity.model_copy(update={"geometry": geometry}))
            }
        )
    if AuditCase.BAD_TEXTSTYLE in cases:
        entity = model.entities[1]
        geometry = entity.geometry.model_copy(update={"text_style": "VENDOR-TEXT"})
        model = model.model_copy(
            update={
                "entities": (
                    model.entities[0],
                    entity.model_copy(update={"geometry": geometry}),
                    model.entities[2],
                )
            }
        )
    if AuditCase.BAD_UNITS in cases:
        model = model.model_copy(update={"source_unit_code": "in", "to_mm_factor": 25.4})
    return model


@given(
    cases=st.frozensets(
        st.sampled_from(tuple(case for case in AuditCase if case is not AuditCase.CLEAN))
    )
)
def test_property_43_audit_matches_exact_profile_deviation(
    cases: frozenset[AuditCase],
) -> None:
    profile = load_profile("demo-profile")
    model = _mutate(_clean_model(profile), cases)
    report = default_engine().run(
        ValidationStage.DRAWING_STANDARD,
        RuleContext(
            profile=profile,
            tolerance=ToleranceProfile(id="audit", version="1.0"),
            drawing_model=model,
        ),
        job_id="job-audit",
        entities_examined=len(model.entities),
    )

    assert report.profile_ref == profile.as_ref()
    assert report.company_approved is False
    assert report.entities_examined == len(model.entities)
    if not cases:
        assert report.findings == ()
        assert report.blocking_count == report.error_count == report.warning_count == 0
        assert report.info_count == 0
        return

    expected_rule_ids = [
        *("LAYER_SET_MATCHES_PROFILE",)
        * len(
            cases
            & {
                AuditCase.EXTRA_LAYER,
                AuditCase.MISSING_LAYER,
                AuditCase.BAD_LAYER_PROPERTY,
            }
        ),
        *(
            EXPECTED_RULE[case]
            for case in (
                AuditCase.WRONG_ENTITY_LAYER,
                AuditCase.BAD_DIMSTYLE,
                AuditCase.BAD_TEXTSTYLE,
                AuditCase.BAD_UNITS,
            )
            if case in cases
        ),
    ]
    assert [finding.rule_id for finding in report.findings] == expected_rule_ids
    assert all(
        finding.expected is not None and finding.actual is not None for finding in report.findings
    )
    assert report.warning_count == int(AuditCase.EXTRA_LAYER in cases)
    assert report.error_count == len(report.findings) - report.warning_count
    assert report.blocking_count == report.info_count == 0
    dumped = report.model_dump(mode="json")
    assert dumped["error_count"] + dumped["warning_count"] == len(report.findings)


@given(case=defective_models())
def test_property_62_auditor_finds_injected_defect_with_complete_evidence(
    case: DefectiveDrawingModelCase,
) -> None:
    profile = load_profile("demo-profile")
    model = _company_compliant(case.model, profile)
    before = model.model_dump(mode="json")
    report = audit_drawing(model, profile=profile, tolerance=profile.tolerance())
    rule_ids = [finding.rule_id for finding in report.findings]

    assert rule_ids == [EXPECTED_DEFECT_RULE[case.defect]]
    assert rule_ids == sorted(rule_ids)
    assert report.entities_examined == len(model.entities)
    assert (
        report.blocking_count + report.error_count + report.warning_count + report.info_count
        == len(report.findings)
    )
    assert all(
        finding.entity_ref
        and finding.expected is not None
        and finding.actual is not None
        and finding.tolerance is not None
        and finding.message
        and finding.suggested_fix
        for finding in report.findings
    )
    assert model.model_dump(mode="json") == before
    repeated = audit_drawing(model, profile=profile, tolerance=profile.tolerance())
    assert repeated.findings == report.findings


def test_property_62_clean_drawing_has_no_audit_findings() -> None:
    profile = load_profile("demo-profile")
    model = _company_compliant(
        _drawing_model(
            (
                _polyline_record(
                    "outline",
                    ((-50.0, -50.0), (50.0, -50.0), (50.0, 50.0), (-50.0, 50.0)),
                    closed=True,
                ),
                _circle_record("hole", (0.0, 0.0), 2.0),
                _circle_record("independent-round-part", (55.0, 0.0), 2.0),
            )
        ),
        profile,
    )
    report = audit_drawing(model, profile=profile, tolerance=profile.tolerance())
    assert report.findings == ()


def test_dimension_override_is_compared_with_measured_geometry() -> None:
    profile = load_profile("demo-profile")
    dimension = (
        _clean_model(profile)
        .entities[2]
        .model_copy(
            update={
                "geometry": DimensionGeometry(
                    dimension_type="linear",
                    dimension_style=profile.dimension_style or "",
                    measurement_mm=20.0,
                    text_override="25.0",
                    measured_entity_refs=("source-line",),
                )
            }
        )
    )
    model = _company_compliant(_drawing_model((dimension,)), profile)
    report = audit_drawing(model, profile=profile, tolerance=profile.tolerance())
    finding = next(
        item for item in report.findings if item.rule_id == "DIMENSION_TEXT_MATCHES_GEOMETRY"
    )
    assert finding.entity_ref == dimension.entity_ref
    assert finding.expected == 20.0
    assert finding.actual == 25.0


@given(
    length_mm=st.floats(min_value=0.1, max_value=10_000.0, allow_nan=False),
    area_mm2=st.floats(min_value=0.1, max_value=1_000_000.0, allow_nan=False),
    noise=st.floats(min_value=-0.0004, max_value=0.0004, allow_nan=False),
    reverse=st.booleans(),
)
def test_property_71_golden_comparison_is_semantic_not_byte_based(
    length_mm: float,
    area_mm2: float,
    noise: float,
    reverse: bool,
) -> None:
    tolerance = ToleranceProfile(
        id="golden-property",
        version="1.0",
        absolute_length_mm=0.001,
        area_mm2=0.01,
    )
    config = GoldenComparisonConfig(tolerance=tolerance)
    expected_entities = {
        "generated_at": "2026-01-01T00:00:00Z",
        "entities": [
            {
                "entity_ref": "acad:handle:10",
                "operation_id": "op-outline",
                "feature_id": "plate",
                "entity_type": "polyline",
                "layer": "OBJECT",
                "style": {"linetype": "CONTINUOUS", "lineweight": 0.25},
                "measurements": {"length_mm": length_mm, "area_mm2": area_mm2},
            },
            {
                "entity_ref": "acad:handle:11",
                "operation_id": "op-hole",
                "feature_id": "hole-1",
                "entity_type": "circle",
                "layer": "OBJECT",
                "style": {"linetype": "CONTINUOUS", "lineweight": 0.25},
                "measurements": {"diameter_mm": 8.0},
            },
        ],
    }
    actual_items = [
        {
            **entity,
            "entity_ref": f"acad:handle:{100 + index}",
            "measurements": {
                key: value + noise if isinstance(value, float) else value
                for key, value in entity["measurements"].items()
            },
        }
        for index, entity in enumerate(expected_entities["entities"])
    ]
    if reverse:
        actual_items.reverse()
    actual_entities = {
        "generated_at": "2030-12-31T23:59:59Z",
        "entities": actual_items,
    }

    expected_bytes = json.dumps(expected_entities, sort_keys=True).encode()
    actual_bytes = json.dumps(actual_entities, indent=2, sort_keys=False).encode()
    assert expected_bytes != actual_bytes
    assert compare_semantic_entities(expected_entities, actual_entities, config=config).matches

    wrong_layer = {
        **actual_entities,
        "entities": [
            {**actual_items[0], "layer": "HIDDEN"},
            *actual_items[1:],
        ],
    }
    assert not compare_semantic_entities(expected_entities, wrong_layer, config=config).matches

    expected_takeoff = {
        "document_id": "doc-1",
        "revision": "sha256:stable",
        "parts": [
            {
                "part_code": "P-01",
                "quantity": 2,
                "net_area_mm2": area_mm2,
                "total_mass_kg": length_mm,
                "hole_groups": [
                    {"diameter_mm": 8.0, "count": 1, "entity_refs": ["acad:handle:11"]}
                ],
                "evidence": {"outline": ["acad:handle:10"]},
            }
        ],
        "created_at": "2026-01-01T00:00:00Z",
    }
    actual_takeoff = {
        **expected_takeoff,
        "created_at": "2030-12-31T23:59:59Z",
        "parts": [
            {
                **expected_takeoff["parts"][0],
                "net_area_mm2": area_mm2 + noise,
                "total_mass_kg": length_mm + noise,
                "hole_groups": [
                    {"diameter_mm": 8.0, "count": 1, "entity_refs": ["acad:handle:999"]}
                ],
                "evidence": {"outline": ["acad:handle:999"]},
            }
        ],
    }
    assert compare_takeoff_reports(expected_takeoff, actual_takeoff, config=config).matches
    wrong_quantity = {
        **actual_takeoff,
        "parts": [{**actual_takeoff["parts"][0], "quantity": 3}],
    }
    assert not compare_takeoff_reports(expected_takeoff, wrong_quantity, config=config).matches
