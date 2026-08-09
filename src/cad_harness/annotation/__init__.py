"""Geometry-derived drawing annotations."""

from cad_harness.annotation.engine import AnnotationEngine, AnnotationResult
from cad_harness.annotation.title_block import TitleBlockResult, resolve_title_block

__all__ = ["AnnotationEngine", "AnnotationResult", "TitleBlockResult", "resolve_title_block"]
