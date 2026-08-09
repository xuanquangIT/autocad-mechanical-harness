"""Property 20: linear patterns preserve count and pitch."""

from __future__ import annotations

from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.geometry.patterns import linear_pattern
from cad_harness.geometry.primitives import Point2D
from cad_harness.geometry.tolerance import DEMO_TOLERANCE


# Feature: cad-ai-production-roadmap, Property 20: `linear_hole_pattern` sinh đúng số lỗ với đúng bước
@given(
    start_x=st.floats(min_value=-1e4, max_value=1e4, allow_nan=False),
    start_y=st.floats(min_value=-1e4, max_value=1e4, allow_nan=False),
    direction_x=st.floats(min_value=0.1, max_value=100.0, allow_nan=False),
    direction_y=st.floats(min_value=-100.0, max_value=100.0, allow_nan=False),
    pitch=st.floats(min_value=0.01, max_value=1000.0, allow_nan=False),
    count=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=100, deadline=None)
def test_linear_hole_pattern_has_requested_count_and_pitch(
    start_x: float,
    start_y: float,
    direction_x: float,
    direction_y: float,
    pitch: float,
    count: int,
) -> None:
    """**Validates: Requirements 8.5**"""
    centers = linear_pattern(Point2D(start_x, start_y), (direction_x, direction_y), pitch, count)
    assert len(centers) == count
    assert all(
        DEMO_TOLERANCE.length_close(first.distance_to(second), pitch)
        for first, second in pairwise(centers)
    )
