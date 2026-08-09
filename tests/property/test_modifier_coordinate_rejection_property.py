"""Property 22: modifier specs reject intermediate coordinates."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from cad_harness.domain.models.drawing_spec import ModifierSpec

forbidden_keys = st.sampled_from(
    ["points", "vertices", "coordinates", "tangent_points", "arc_coordinates"]
)


# Feature: cad-ai-production-roadmap, Property 22: Modifier không nhận toạ độ trung gian từ spec
@given(key=forbidden_keys, values=st.lists(st.floats(allow_nan=False), min_size=1, max_size=6))
@settings(max_examples=100, deadline=None)
def test_modifier_schema_rejects_intermediate_coordinate_keys(
    key: str, values: list[float]
) -> None:
    """**Validates: Requirements 8.6**"""
    with pytest.raises(ValidationError):
        ModifierSpec(type="corner_fillet", parameters={"radius_mm": 2.0, key: values})
