"""AutoCAD Mechanical Harness - deterministic 2D mechanical drawing automation.

Layering rule (see docs/AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.en.md section 5.1):

    interface (apps/) -> application -> domain -> geometry/validation pure core

``cad_harness.domain`` must never import MCP, COM, AutoCAD, SQLAlchemy or UI code.
"""

__all__ = ["__version__"]

__version__ = "0.2.0"
