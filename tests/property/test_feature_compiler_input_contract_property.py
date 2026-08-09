"""Property 15: completed compilers have an exact, fail-closed input contract."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.errors import MissingRequiredInputsError
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.feature_catalog.base import CompileContext, FeatureCompiler
from cad_harness.feature_catalog.bracket import LBracketCompiler
from cad_harness.feature_catalog.flange import FlangeCompiler
from cad_harness.feature_catalog.slot import SlotCompiler


@dataclass(frozen=True)
class CompilerCase:
    compiler: FeatureCompiler
    parameters: dict[str, object]


CASES = {
    "flange": CompilerCase(
        FlangeCompiler(),
        {
            "outer_diameter_mm": 160.0,
            "bore_diameter_mm": 60.0,
            "bolt_hole_count": 8,
            "bolt_hole_diameter_mm": 14.0,
            "pcd_mm": 120.0,
            "datum": [0.0, 0.0],
        },
    ),
    "slot": CompilerCase(
        SlotCompiler(),
        {"length_mm": 60.0, "width_mm": 20.0, "center_mm": [0.0, 0.0]},
    ),
    "l_bracket": CompilerCase(
        LBracketCompiler(),
        {
            "leg_a_mm": 100.0,
            "leg_b_mm": 80.0,
            "thickness_mm": 10.0,
            "origin_mm": [0.0, 0.0],
        },
    ),
}


@st.composite
def compiler_with_missing_subset(draw: st.DrawFn) -> tuple[str, frozenset[str]]:
    feature_type = draw(st.sampled_from(tuple(CASES)))
    required = CASES[feature_type].compiler.required_parameters
    missing = draw(st.frozensets(st.sampled_from(required), min_size=1, max_size=len(required)))
    return feature_type, missing


# Feature: cad-ai-production-roadmap, Property 15: Compiler báo đúng mọi input còn thiếu và không sinh operation khi còn thiếu
@given(case_and_missing=compiler_with_missing_subset())
@settings(max_examples=100, deadline=None)
def test_compilers_report_exactly_all_missing_inputs_and_emit_no_operations(
    case_and_missing: tuple[str, frozenset[str]],
) -> None:
    """**Validates: Requirements 7.1, 7.2, 7.3, 7.4**"""
    profile = load_profile("demo-profile")
    context = CompileContext(profile=profile, tolerance=profile.tolerance(), datum=None)
    feature_type, missing = case_and_missing
    case = CASES[feature_type]
    parameters = {key: value for key, value in case.parameters.items() if key not in missing}
    feature = FeatureSpec(
        feature_id=f"property-15-{feature_type}",
        type=feature_type,
        parameters=parameters,
    )

    report = case.compiler.validate_inputs(feature, context)
    actual_missing = {item.path.rsplit(".", 1)[-1] for item in report.missing}
    assert actual_missing == set(missing)
    assert all(item.accepted_formats for item in report.missing)
    with pytest.raises(MissingRequiredInputsError) as caught:
        case.compiler.compile(feature, context)
    assert {
        item["path"].rsplit(".", 1)[-1] for item in caught.value.details["missing_inputs"]
    } == set(missing)

    complete = FeatureSpec(
        feature_id=f"property-15-complete-{feature_type}",
        type=feature_type,
        parameters=case.parameters,
    )
    assert case.compiler.compile(complete, context).operations
