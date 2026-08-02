"""Unit normalization. The canonical internal unit for the MVP is the millimetre."""

from __future__ import annotations

import math
from enum import StrEnum

from cad_harness.domain.errors import InvalidGeometryError


class Unit(StrEnum):
    """Length units accepted at the boundary. Everything is normalized to mm."""

    MM = "mm"
    CM = "cm"
    M = "m"
    INCH = "in"


CANONICAL_UNIT: Unit = Unit.MM

_FACTOR_TO_MM: dict[Unit, float] = {
    Unit.MM: 1.0,
    Unit.CM: 10.0,
    Unit.M: 1000.0,
    Unit.INCH: 25.4,
}


def to_mm(value: float, unit: Unit = Unit.MM) -> float:
    """Convert ``value`` expressed in ``unit`` to millimetres.

    Raises:
        InvalidGeometryError: if the value is not finite. NaN/Infinity must never
            reach the geometry kernel (architecture section 15.1).
    """
    if not math.isfinite(value):
        raise InvalidGeometryError(
            "Non-finite length value rejected",
            required_action="Supply a finite numeric value",
            details={"value": repr(value), "unit": unit.value},
        )
    return value * _FACTOR_TO_MM[unit]


def from_mm(value_mm: float, unit: Unit = Unit.MM) -> float:
    """Convert a millimetre value into ``unit`` for presentation only."""
    return value_mm / _FACTOR_TO_MM[unit]
