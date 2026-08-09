"""Unit examples for the completed flange, slot, and L-bracket compilers."""

from __future__ import annotations

import pytest

from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.errors import InvalidFeatureParametersError, MissingRequiredInputsError
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.domain.models.operation_plan import OperationType
from cad_harness.feature_catalog import describe_all
from cad_harness.feature_catalog.base import CompileContext
from cad_harness.feature_catalog.bracket import LBracketCompiler
from cad_harness.feature_catalog.flange import FlangeCompiler
from cad_harness.feature_catalog.slot import SlotCompiler


def _context() -> CompileContext:
    profile = load_profile("demo-profile")
    return CompileContext(profile=profile, tolerance=profile.tolerance())


def test_flange_compiles_outer_bore_and_kernel_generated_bolt_circle() -> None:
    feature = FeatureSpec(
        feature_id="flange-1",
        type="flange",
        parameters={
            "outer_diameter_mm": 160.0,
            "bore_diameter_mm": 60.0,
            "bolt_hole_count": 8,
            "bolt_hole_diameter_mm": 14.0,
            "pcd_mm": 120.0,
            "datum": [10.0, 20.0],
        },
    )
    compiled = FlangeCompiler().compile(feature, _context())
    assert [item.type for item in compiled.operations] == [
        OperationType.CREATE_CIRCLE,
        OperationType.CREATE_CIRCLE,
        OperationType.CREATE_CIRCLES,
    ]
    assert len(compiled.operations[-1].geometry["centers_mm"]) == 8
    assert {item.rule_id for item in compiled.expectations} == {
        "FLANGE_OUTER_DIAMETER_CLEARANCE",
        "FLANGE_HOLES_ON_PCD",
    }


def test_slot_emits_two_lines_two_arcs_and_tangency_expectation() -> None:
    feature = FeatureSpec(
        feature_id="slot-1",
        type="slot",
        parameters={"length_mm": 60.0, "width_mm": 20.0, "center_mm": [0.0, 0.0]},
    )
    compiled = SlotCompiler().compile(feature, _context())
    assert [item.type for item in compiled.operations] == [
        OperationType.CREATE_LINE,
        OperationType.CREATE_ARC,
        OperationType.CREATE_LINE,
        OperationType.CREATE_ARC,
    ]
    assert compiled.expectations[0].rule_id == "SLOT_ARC_TANGENCY"


def test_l_bracket_uses_closed_kernel_outline_with_optional_fillet() -> None:
    feature = FeatureSpec(
        feature_id="bracket-1",
        type="l_bracket",
        parameters={
            "leg_a_mm": 100.0,
            "leg_b_mm": 80.0,
            "thickness_mm": 10.0,
            "origin_mm": [5.0, 7.0],
            "inner_fillet_radius_mm": 4.0,
        },
    )
    compiled = LBracketCompiler().compile(feature, _context())
    outline = compiled.operations[0]
    assert outline.type is OperationType.CREATE_CLOSED_POLYLINE
    assert len(outline.geometry["vertices_mm"]) > 6
    assert compiled.expectations[0].expected == {"leg_angle_deg": 90.0, "closed": True}


@pytest.mark.parametrize(
    ("compiler", "feature"),
    [
        (FlangeCompiler(), FeatureSpec(feature_id="f", type="flange", parameters={})),
        (SlotCompiler(), FeatureSpec(feature_id="s", type="slot", parameters={})),
        (LBracketCompiler(), FeatureSpec(feature_id="b", type="l_bracket", parameters={})),
    ],
)
def test_compile_refuses_missing_inputs_with_actionable_formats(compiler, feature) -> None:
    report = compiler.validate_inputs(feature, _context())
    assert {item.path.rsplit(".", 1)[-1] for item in report.missing} == set(
        compiler.required_parameters
    )
    assert all(item.accepted_formats for item in report.missing)
    with pytest.raises(MissingRequiredInputsError) as caught:
        compiler.compile(feature, _context())
    assert caught.value.details["missing_inputs"]


def test_invalid_bracket_thickness_is_rejected() -> None:
    feature = FeatureSpec(
        feature_id="bracket-invalid",
        type="l_bracket",
        parameters={
            "leg_a_mm": 10.0,
            "leg_b_mm": 20.0,
            "thickness_mm": 10.0,
            "origin_mm": [0.0, 0.0],
        },
    )
    with pytest.raises(InvalidFeatureParametersError):
        LBracketCompiler().compile(feature, _context())


def test_describe_all_publishes_current_versioned_feature_schemas() -> None:
    descriptions = {item["type"]: item for item in describe_all()}
    for feature_type in ("flange", "slot", "l_bracket"):
        assert descriptions[feature_type]["schema_version"] == SCHEMA_VERSION
        assert descriptions[feature_type]["required_parameters"]
