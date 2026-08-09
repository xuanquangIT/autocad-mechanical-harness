"""Property 19: cutouts preserve closure and remove their analytic area."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.geometry.areas import ContourForest
from cad_harness.geometry.cutouts import corner_notch, edge_cutout, keyway_contour
from cad_harness.geometry.primitives import Point2D, Polyline2D
from cad_harness.geometry.tolerance import DEMO_TOLERANCE


# Feature: cad-ai-production-roadmap, Property 19: Feature khoét giữ contour kín và giảm diện tích đúng bằng phần bị bỏ
@given(
    kind=st.sampled_from(("corner_notch", "edge_cutout", "keyway")),
    side=st.floats(min_value=30.0, max_value=300.0, allow_nan=False),
    width_fraction=st.floats(min_value=0.05, max_value=0.20, allow_nan=False),
    depth_fraction=st.floats(min_value=0.05, max_value=0.25, allow_nan=False),
)
@settings(max_examples=100, deadline=None)
def test_cutout_contours_are_closed_and_reduce_area_exactly(
    kind: str, side: float, width_fraction: float, depth_fraction: float
) -> None:
    """**Validates: Requirements 8.3, 8.4**"""
    parent = Polyline2D(
        (Point2D(0.0, 0.0), Point2D(side, 0.0), Point2D(side, side), Point2D(0.0, side)),
        closed=True,
    )
    width = side * width_fraction
    depth = side * depth_fraction
    if kind == "corner_notch":
        result = corner_notch(parent, 1, width, depth, DEMO_TOLERANCE)
        assert result.outline.closed
        assert DEMO_TOLERANCE.area_close(
            result.outline.area(), parent.area() - result.removed_area_mm2
        )
        assert DEMO_TOLERANCE.area_close(result.removed_area_mm2, width * depth)
    elif kind == "edge_cutout":
        result = edge_cutout(parent, 0, width, width, depth, DEMO_TOLERANCE)
        assert result.outline.closed
        assert DEMO_TOLERANCE.area_close(
            result.outline.area(), parent.area() - result.removed_area_mm2
        )
        assert DEMO_TOLERANCE.area_close(result.removed_area_mm2, width * depth)
    else:
        keyway = keyway_contour(
            Point2D(side / 2.0, side / 2.0),
            side / 4.0,
            width,
            depth,
            DEMO_TOLERANCE,
        )
        forest = ContourForest.build((parent, keyway.contour), DEMO_TOLERANCE)
        assert keyway.preview_outline.closed
        assert DEMO_TOLERANCE.area_close(
            forest.net_area_mm2, parent.area() - keyway.removed_area_mm2
        )
        box = keyway.preview_outline.bounding_box()
        assert DEMO_TOLERANCE.length_close(box.max_y - (side / 2.0 + keyway.bore_radius_mm), depth)
