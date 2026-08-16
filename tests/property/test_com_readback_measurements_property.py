# Feature: cad-ai-production-roadmap, Property 10: Số đo post-commit là giá trị đọc lại thay vì expected

import math

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from cad_harness.adapters.autocad_com import ComAutoCADAdapter
from cad_harness.domain.models.operation_plan import Operation, OperationType


class _CircleReadback:
    Handle = "2AF"
    ObjectName = "AcDbCircle"
    Layer = "OBJECT"
    Center = (12.0, 34.0, 0.0)

    def __init__(self, diameter: float) -> None:
        self.Diameter = diameter
        self.Radius = diameter / 2.0
        self.Area = math.pi * self.Radius**2
        self.Circumference = math.pi * diameter


@given(
    actual=st.floats(min_value=0.001, max_value=1_000_000, allow_nan=False, allow_infinity=False),
    expected=st.floats(min_value=0.001, max_value=1_000_000, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, deadline=None)
def test_com_measurements_are_read_from_entity(actual: float, expected: float) -> None:
    """**Validates: Requirements 4.2, 5.6, 12.7**"""
    assume(not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9))
    operation = Operation(
        operation_id="op-circle",
        feature_id="feature-circle",
        type=OperationType.CREATE_CIRCLE,
        layer="OBJECT",
        geometry={"center_mm": [0.0, 0.0], "diameter_mm": actual},
        expected={"diameter_mm": expected},
    )

    result = ComAutoCADAdapter()._result_from_entity(operation, _CircleReadback(actual), count=1)

    assert result.measurements["diameter_mm"] == actual
    assert result.measurements["diameter_mm"] != expected
    assert result.measurements["center_mm"] == [12.0, 34.0]
