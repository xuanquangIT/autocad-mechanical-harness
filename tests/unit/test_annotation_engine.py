"""Focused examples for annotation, title-block, layout and GD&T behavior."""

import pytest

from cad_harness.annotation.engine import AnnotationEngine
from cad_harness.annotation.placement import TextBox, place_text
from cad_harness.annotation.title_block import resolve_title_block
from cad_harness.company_rules.loader import TitleBlockField, load_profile
from cad_harness.domain.errors import StandardProfileNotFoundError
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.operation_plan import Operation, OperationType
from cad_harness.geometry.primitives import Point2D


def _geometry() -> tuple[Operation, ...]:
    return (
        Operation(
            operation_id="op:plate:outline",
            feature_id="plate",
            type=OperationType.CREATE_CLOSED_POLYLINE,
            layer="OBJECT",
            geometry={"vertices_mm": [[0, 0], [100, 0], [100, 60], [0, 60]]},
        ),
        Operation(
            operation_id="op:holes:holes",
            feature_id="holes",
            type=OperationType.CREATE_CIRCLES,
            layer="OBJECT",
            geometry={"centers_mm": [[20, 20], [80, 20], [20, 40], [80, 40]], "diameter_mm": 10},
        ),
    )


def _spec(annotations: dict[str, object]) -> DrawingSpec:
    return DrawingSpec.model_validate(
        {
            "spec_id": "s",
            "document_id": "d",
            "standard_profile": {"profile_id": "demo-profile", "version": "1.0"},
            "annotations": annotations,
        }
    )


def test_four_holes_use_one_callout_and_one_hole_table_row() -> None:
    profile = load_profile("demo-profile")
    result = AnnotationEngine(profile, profile.tolerance()).annotate(
        geometry_operations=_geometry(),
        spec=_spec({"dimensions": "auto_required"}),
        datum=Point2D(0, 0),
    )
    kinds = [operation.geometry.get("annotation_kind") for operation in result.operations]
    assert kinds.count("hole_callout") == 1
    assert kinds.count("hole_table_row") == 1
    assert (
        sum(operation.type is OperationType.CREATE_CENTERMARK for operation in result.operations)
        == 4
    )


def test_missing_annotation_style_reports_the_exact_profile_key() -> None:
    profile = load_profile("demo-profile").model_copy(update={"dimension_style": None})
    with pytest.raises(StandardProfileNotFoundError) as caught:
        AnnotationEngine(profile, profile.tolerance()).annotate(
            geometry_operations=_geometry(),
            spec=_spec({"dimensions": "auto_required"}),
            datum=Point2D(0, 0),
        )
    assert caught.value.details == {"missing_config_key": "dimension_style"}


def test_title_block_uses_profile_value_with_profile_provenance() -> None:
    profile = load_profile("demo-profile").model_copy(
        update={"title_block_fields": (TitleBlockField(name="company", value="ACME"),)}
    )
    result = resolve_title_block(_spec({"dimensions": "none", "title_block": "demo"}), profile)
    assert result.missing_inputs == []
    assert result.values["company"].value == "ACME"
    assert result.values["company"].source == profile.profile_id
    assert result.values["company"].source_version == profile.version


def test_exhausted_placement_candidates_return_warning_not_silent_overlap() -> None:
    occupied = [TextBox(0, 0, 100, 100)]
    _, _, warning = place_text(
        text="note",
        anchor=(0, 0),
        text_height_mm=2.5,
        occupied=occupied,
        offsets=((0, 0),),
    )
    assert warning is not None
    assert warning.rule_id == "ANNOTATION_OVERLAP"
    assert warning.severity.value == "warning"
