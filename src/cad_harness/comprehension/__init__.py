"""Pure drawing-comprehension engines; no I/O or AutoCAD dependencies."""

from cad_harness.comprehension.contours import (
    AssembledContour,
    ContourAnalysis,
    EdgeRecord,
    analyze_contours,
)
from cad_harness.comprehension.recognizer import recognize

__all__ = ["AssembledContour", "ContourAnalysis", "EdgeRecord", "analyze_contours", "recognize"]
