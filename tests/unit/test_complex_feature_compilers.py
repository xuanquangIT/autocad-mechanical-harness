"""Focused examples for Task 10 modifiers and complex feature compilers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.drawing_spec import FeatureSpec, ModifierSpec
from cad_harness.feature_catalog import describe_all
from cad_harness.feature_catalog.base import CompileContext
from cad_harness.feature_catalog.corner_notch import CornerNotchCompiler
from cad_harness.feature_catalog.edge_cutout import EdgeCutoutCompiler
from cad_harness.feature_catalog.keyway import KeywayCompiler
from cad_harness.feature_catalog.linear_hole_pattern import LinearHolePatternCompiler
from cad_harness.feature_catalog.modifiers import CornerFilletModifier
from cad_harness.geometry.primitives import BoundingBox, Point2D, Polyline2D


def _context(*, parent: bool = False) -> CompileContext:
    profile = load_profile("demo-profile")
    outline = Polyline2D(
        (Point2D(0.0, 0.0), Point2D(100.0, 0.0), Point2D(100.0, 80.0), Point2D(0.0, 80.0)),
        closed=True,
    )
    return CompileContext(
        profile=profile,
        tolerance=profile.tolerance(),
        datum=Point2D(50.0, 40.0),
        parent_feature_id="plate" if parent else None,
        parent_box=BoundingBox(0.0, 0.0, 100.0, 80.0) if parent else None,
        parent_outline=outline if parent else None,
    )


def test_modifier_id_and_measurement_expectation_are_stable() -> None:
    context = _context(parent=True)
    assert context.parent_outline is not None
    spec = ModifierSpec(type="corner_fillet", parameters={"radius_mm": 5.0, "vertex_indices": [1]})
    first = CornerFilletModifier().apply(spec, context.parent_outline, context, modifier_index=3)
    second = CornerFilletModifier().apply(spec, context.parent_outline, context, modifier_index=3)
    assert first.feature_id == "feature:plate:mod:corner_fillet:3"
    assert first == second
    assert first.expectations[0].expected["actual_radius_mm"] == pytest.approx(5.0)


def test_modifier_contract_rejects_coordinates_but_accepts_vertex_indices() -> None:
    ModifierSpec(
        type="corner_chamfer",
        parameters={"distance_1_mm": 2.0, "distance_2_mm": 2.0, "vertex_indices": [0]},
    )
    with pytest.raises(ValidationError):
        ModifierSpec(type="corner_fillet", parameters={"points": [[0.0, 0.0]]})


@pytest.mark.parametrize(
    ("compiler", "feature"),
    [
        (
            CornerNotchCompiler(),
            FeatureSpec(
                feature_id="notch",
                type="corner_notch",
                parameters={"corner_index": 1, "width_mm": 10.0, "height_mm": 8.0},
            ),
        ),
        (
            EdgeCutoutCompiler(),
            FeatureSpec(
                feature_id="cutout",
                type="edge_cutout",
                parameters={"edge_index": 0, "offset_mm": 20.0, "width_mm": 15.0, "depth_mm": 5.0},
            ),
        ),
        (
            KeywayCompiler(),
            FeatureSpec(
                feature_id="keyway",
                type="keyway",
                parameters={
                    "bore_diameter_mm": 20.0,
                    "key_width_mm": 6.0,
                    "key_depth_mm": 3.0,
                    "center_mm": [50.0, 40.0],
                },
            ),
        ),
        (
            LinearHolePatternCompiler(),
            FeatureSpec(
                feature_id="linear",
                type="linear_hole_pattern",
                parameters={
                    "start_point": [20.0, 20.0],
                    "direction": [1.0, 0.0],
                    "pitch_mm": 20.0,
                    "count": 3,
                    "hole_diameter_mm": 5.0,
                },
            ),
        ),
    ],
)
def test_new_feature_compilers_emit_closed_or_pattern_geometry(compiler, feature) -> None:
    compiled = compiler.compile(feature, _context(parent=True))
    assert compiled.operations
    assert compiled.expectations


def test_catalog_publishes_current_versioned_schemas() -> None:
    descriptions = {item["type"]: item for item in describe_all()}
    for feature_type in ("corner_notch", "edge_cutout", "keyway", "linear_hole_pattern"):
        assert descriptions[feature_type]["schema_version"] == SCHEMA_VERSION
        assert descriptions[feature_type]["required_parameters"]
