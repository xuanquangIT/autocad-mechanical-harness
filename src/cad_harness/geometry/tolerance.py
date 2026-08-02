"""Tolerance policy. Floats are never compared with ``==`` anywhere in the kernel."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToleranceProfile:
    """Versioned numeric comparison policy (architecture section 15.4).

    These are *computational* tolerances for deciding whether two computed values
    agree. They are not manufacturing tolerances and must not be presented as such.
    """

    id: str
    version: str
    canonical_unit: str = "mm"
    absolute_length_mm: float = 1.0e-3
    relative_length: float = 1.0e-9
    angular_deg: float = 1.0e-4
    coincidence_mm: float = 1.0e-3
    area_mm2: float = 1.0e-2

    def as_ref(self) -> str:
        return f"{self.id}@{self.version}"

    def length_close(self, a: float, b: float) -> bool:
        return math.isclose(a, b, rel_tol=self.relative_length, abs_tol=self.absolute_length_mm)

    def area_close(self, a: float, b: float) -> bool:
        return math.isclose(a, b, rel_tol=self.relative_length, abs_tol=self.area_mm2)

    def angle_close_deg(self, a: float, b: float) -> bool:
        """Compare angles in degrees, wrapping the difference into [-180, 180]."""
        delta = (a - b + 180.0) % 360.0 - 180.0
        return abs(delta) <= self.angular_deg

    def is_coincident(self, distance_mm: float) -> bool:
        return distance_mm <= self.coincidence_mm

    def is_zero_length(self, length_mm: float) -> bool:
        return length_mm <= self.absolute_length_mm


#: Demo configuration only. Not a company-approved profile.
DEMO_TOLERANCE = ToleranceProfile(id="demo-mechanical-mm", version="1.0")
