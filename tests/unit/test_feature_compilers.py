"""Feature compilers: determinism and the no-silent-defaults rule."""

from __future__ import annotations

from typing import Any

import pytest

from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.errors import UnsupportedFeatureError
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.domain.models.operation_plan import OperationType
from cad_harness.feature_catalog import registry
from cad_harness.feature_catalog.base import CompileContext
from cad_harness.feature_catalog.hole_pattern import BoltCirclePatternCompiler
from cad_harness.feature_catalog.plate import RectangularPlateCompiler
from cad_harness.geometry.primitives import Point2D
from cad_harness.geometry.tolerance import ToleranceProfile


@pytest.fixture
def context(profile: CompanyProfile, tolerance: ToleranceProfile) -> CompileContext:
    return CompileContext(profile=profile, tolerance=tolerance, datum=Point2D(0.0, 0.0))


def plate_feature(**overrides: Any) -> FeatureSpec:
    parameters: dict[str, Any] = {
        "width_mm": 160.0,
        "height_mm": 100.0,
        "thickness_mm": 12.0,
        "origin_mm": [0.0, 0.0],
    }
    parameters.update(overrides)
    return FeatureSpec(feature_id="plate-1", type="rectangular_plate", parameters=parameters)


class TestRegistry:
    def test_implemented_features_are_registered(self) -> None:
        assert registry.supported_types() == [
            "bolt_circle_pattern",
            "corner_notch",
            "edge_cutout",
            "flange",
            "keyway",
            "l_bracket",
            "linear_hole_pattern",
            "rectangular_hole_pattern",
            "rectangular_plate",
            "slot",
        ]

    def test_unknown_feature_reports_supported_alternatives(self) -> None:
        with pytest.raises(UnsupportedFeatureError) as info:
            registry.get_compiler("gear")
        assert "rectangular_plate" in info.value.details["supported"]

    def test_completed_features_are_not_planned(self) -> None:
        from cad_harness.feature_catalog import PLANNED_FEATURES

        assert PLANNED_FEATURES == ()
        for completed in ("flange", "slot", "l_bracket"):
            assert completed in registry.supported_types()

    def test_search_matches_description(self) -> None:
        assert any(e["type"] == "bolt_circle_pattern" for e in registry.search("pitch circle"))


class TestPlateCompiler:
    def test_reference_case_geometry(self, context: CompileContext) -> None:
        compiler = RectangularPlateCompiler()
        compiled = compiler.compile(plate_feature(), context)
        outline = compiled.operations[0]

        assert outline.type is OperationType.CREATE_CLOSED_POLYLINE
        assert outline.layer == "OBJECT"
        assert outline.geometry["vertices_mm"] == [
            [0.0, 0.0],
            [160.0, 0.0],
            [160.0, 100.0],
            [0.0, 100.0],
        ]
        assert outline.expected["area_mm2"] == 16000.0

    @pytest.mark.parametrize("missing", ["width_mm", "height_mm", "thickness_mm"])
    def test_missing_size_is_reported_not_defaulted(
        self, context: CompileContext, missing: str
    ) -> None:
        parameters = plate_feature().parameters.copy()
        parameters.pop(missing)
        feature = FeatureSpec(feature_id="plate-1", type="rectangular_plate", parameters=parameters)

        report = RectangularPlateCompiler().validate_inputs(feature, context)
        assert not report.is_complete
        assert any(missing in entry.path for entry in report.missing)

    def test_missing_origin_without_datum_is_reported(
        self, profile: CompanyProfile, tolerance: ToleranceProfile
    ) -> None:
        """The pilot's key behaviour: no datum means ask, never assume [0, 0]."""
        no_datum = CompileContext(profile=profile, tolerance=tolerance, datum=None)
        parameters = plate_feature().parameters.copy()
        parameters.pop("origin_mm")
        feature = FeatureSpec(feature_id="plate-1", type="rectangular_plate", parameters=parameters)

        report = RectangularPlateCompiler().validate_inputs(feature, no_datum)
        paths = [entry.path for entry in report.missing]
        assert any("origin_mm" in path for path in paths)

    def test_compilation_is_deterministic(self, context: CompileContext) -> None:
        compiler = RectangularPlateCompiler()
        first = compiler.compile(plate_feature(), context)
        second = compiler.compile(plate_feature(), context)
        assert [op.model_dump(mode="json") for op in first.operations] == [
            op.model_dump(mode="json") for op in second.operations
        ]

    def test_child_holes_are_placed_against_the_real_outline(self, context: CompileContext) -> None:
        feature = FeatureSpec(
            feature_id="plate-1",
            type="rectangular_plate",
            parameters=plate_feature().parameters,
            children=(
                FeatureSpec(
                    feature_id="plate-1-holes",
                    type="rectangular_hole_pattern",
                    parameters={
                        "hole_diameter_mm": 14.0,
                        "edge_offset_x_mm": 20.0,
                        "edge_offset_y_mm": 20.0,
                        "count_x": 2,
                        "count_y": 2,
                    },
                ),
            ),
        )
        compiled = RectangularPlateCompiler().compile(feature, context)
        holes = next(op for op in compiled.operations if op.type is OperationType.CREATE_CIRCLES)
        assert holes.geometry["centers_mm"] == [
            [20.0, 20.0],
            [140.0, 20.0],
            [20.0, 80.0],
            [140.0, 80.0],
        ]


class TestBoltCircleCompiler:
    def test_expectation_records_angular_spacing(self, context: CompileContext) -> None:
        feature = FeatureSpec(
            feature_id="flange-holes",
            type="bolt_circle_pattern",
            parameters={
                "hole_diameter_mm": 14.0,
                "pcd_mm": 120.0,
                "count": 8,
                "center_mm": [0.0, 0.0],
            },
        )
        compiled = BoltCirclePatternCompiler().compile(feature, context)
        expectation = compiled.expectations[0]
        assert expectation.expected["angular_spacing_deg"] == 45.0
        assert expectation.expected["count"] == 8

    def test_missing_pcd_is_reported(self, context: CompileContext) -> None:
        feature = FeatureSpec(
            feature_id="flange-holes",
            type="bolt_circle_pattern",
            parameters={"hole_diameter_mm": 14.0, "count": 8, "center_mm": [0.0, 0.0]},
        )
        report = BoltCirclePatternCompiler().validate_inputs(feature, context)
        assert any("pcd_mm" in entry.path for entry in report.missing)
