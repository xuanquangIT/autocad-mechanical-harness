"""Pure, deterministic and tolerance-aware mechanical geometry kernel."""

from cad_harness.geometry.areas import (
    ContourForest,
    CurveContour,
    LineEdge,
    contour_area,
    contour_perimeter,
)
from cad_harness.geometry.curves import (
    CurveKind,
    CurveParams,
    chord_segment_count,
    linearize_curve,
    normalize_arc,
    normalize_bulge,
    normalize_circle,
    normalize_ellipse,
)
from cad_harness.geometry.fillet_chamfer import (
    ChamferResult,
    FilletResult,
    chamfer_vertex,
    fillet_vertex,
)
from cad_harness.geometry.patterns import (
    bolt_circle,
    linear_pattern,
    rectangular_grid,
    slot_outline,
)
from cad_harness.geometry.primitives import BoundingBox, Circle2D, Point2D, Polyline2D, Vector2D
from cad_harness.geometry.tolerance import DEMO_TOLERANCE, ToleranceProfile
from cad_harness.geometry.transforms import rotate_about, translate

__all__ = [
    "DEMO_TOLERANCE",
    "BoundingBox",
    "ChamferResult",
    "Circle2D",
    "ContourForest",
    "CurveContour",
    "CurveKind",
    "CurveParams",
    "FilletResult",
    "LineEdge",
    "Point2D",
    "Polyline2D",
    "ToleranceProfile",
    "Vector2D",
    "bolt_circle",
    "chamfer_vertex",
    "chord_segment_count",
    "contour_area",
    "contour_perimeter",
    "fillet_vertex",
    "linear_pattern",
    "linearize_curve",
    "normalize_arc",
    "normalize_bulge",
    "normalize_circle",
    "normalize_ellipse",
    "rectangular_grid",
    "rotate_about",
    "slot_outline",
    "translate",
]
