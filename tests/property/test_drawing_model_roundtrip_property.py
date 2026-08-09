"""Property 36: DrawingModel serialization preserves semantic content and order."""

from hypothesis import given, settings
from tests.property.strategies import drawing_models

from cad_harness.domain.models.drawing_model import DrawingModel


# Feature: cad-ai-production-roadmap, Property 36: DrawingModel serialization round-trip preserves semantics
@given(model=drawing_models())
@settings(max_examples=100, deadline=None)
def test_drawing_model_json_round_trip_preserves_semantics(model: DrawingModel) -> None:
    """**Validates: Requirements 13.11**"""
    restored = DrawingModel.model_validate_json(model.model_dump_json())

    assert restored == model
    assert tuple(entity.entity_ref for entity in restored.entities) == tuple(
        entity.entity_ref for entity in model.entities
    )
    assert tuple(entity.entity_type for entity in restored.entities) == tuple(
        entity.entity_type for entity in model.entities
    )
