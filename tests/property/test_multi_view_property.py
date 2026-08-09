"""Property 29: multi-view placement preserves projection and feature identity."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.errors import UnsupportedFeatureError
from cad_harness.domain.models.drawing_spec import ViewSpec
from cad_harness.domain.models.operation_plan import Operation, OperationType
from cad_harness.feature_catalog.views import SUPPORTED_VIEW_TYPES, place_views


# Feature: cad-ai-production-roadmap, Property 29: Multi-view giữ quan hệ chiếu, khoảng cách và định danh feature
@given(
    extra_views=st.lists(st.sampled_from(("front", "side", "section")), unique=True, max_size=3),
    spacing=st.floats(min_value=10, max_value=1000, allow_nan=False),
    unsupported=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12).filter(
        lambda value: value not in SUPPORTED_VIEW_TYPES
    ),
)
@settings(max_examples=100, deadline=None)
def test_multi_view_projection_spacing_and_ids(
    extra_views: list[str], spacing: float, unsupported: str
) -> None:
    """**Validates: Requirements 11.1, 11.2, 11.3, 11.4**"""
    profile = load_profile("demo-profile")
    profile = profile.model_copy(
        update={
            "layout_rules": profile.layout_rules.model_copy(update={"view_spacing_mm": spacing})
        }
    )
    views = tuple(ViewSpec(type=name) for name in ("top", *extra_views))
    source = Operation(
        operation_id="op:plate:outline",
        feature_id="plate",
        type=OperationType.CREATE_CLOSED_POLYLINE,
        layer="OBJECT",
        geometry={"vertices_mm": [[0, 0], [5, 0], [5, 5], [0, 5]]},
    )
    result = place_views((source,), views, profile)
    assert len(result.operations) == len(views)
    assert {operation.feature_id for operation in result.operations} == {
        f"feature:plate@{view.type}" for view in views
    }
    top = result.origins["top"]
    if "front" in result.origins:
        front = result.origins["front"]
        assert profile.tolerance().length_close(top[0], front[0])
        assert profile.tolerance().length_close(abs(top[1] - front[1]), spacing)
    if "side" in result.origins:
        side = result.origins["side"]
        assert profile.tolerance().length_close(top[1], side[1])
        assert profile.tolerance().length_close(abs(top[0] - side[0]), spacing)
    with pytest.raises(UnsupportedFeatureError) as caught:
        place_views((source,), (ViewSpec(type=unsupported),), profile)
    assert unsupported in caught.value.details["unsupported_view_types"]
    assert caught.value.details["supported_view_types"] == list(SUPPORTED_VIEW_TYPES)
