"""Mechanical geometry kernel: pure, deterministic, tolerance-aware.

No AutoCAD, no I/O, no randomness. Everything here is unit-testable without a CAD
installation and is the only place allowed to compute coordinates.
"""

from cad_harness.geometry.patterns import bolt_circle, rectangular_grid, slot_outline
from cad_harness.geometry.primitives import BoundingBox, Circle2D, Point2D, Polyline2D, Vector2D
from cad_harness.geometry.tolerance import DEMO_TOLERANCE, ToleranceProfile

__all__ = [
    "DEMO_TOLERANCE",
    "BoundingBox",
    "Circle2D",
    "Point2D",
    "Polyline2D",
    "ToleranceProfile",
    "Vector2D",
    "bolt_circle",
    "rectangular_grid",
    "slot_outline",
]
