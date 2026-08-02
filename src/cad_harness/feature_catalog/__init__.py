"""Feature catalog: the only place that turns engineering intent into operations.

Importing this package registers every implemented compiler. Features listed in
``PLANNED_FEATURES`` are visible to clients as not-yet-available, so an AI client can
report "not supported yet" instead of improvising geometry.
"""

from cad_harness.feature_catalog.base import (
    CompileContext,
    CompiledFeature,
    FeatureCompiler,
    InputReport,
)
from cad_harness.feature_catalog.bracket import LBracketCompiler
from cad_harness.feature_catalog.flange import FlangeCompiler
from cad_harness.feature_catalog.hole_pattern import (
    BoltCirclePatternCompiler,
    RectangularHolePatternCompiler,
)
from cad_harness.feature_catalog.plate import RectangularPlateCompiler
from cad_harness.feature_catalog.registry import (
    describe_all,
    get_compiler,
    register,
    search,
    supported_types,
)
from cad_harness.feature_catalog.slot import SlotCompiler

# Implemented compilers. Registration order does not matter; the registry sorts.
register(RectangularPlateCompiler())
register(RectangularHolePatternCompiler())
register(BoltCirclePatternCompiler())

#: Declared but not registered. Compiling one raises UNSUPPORTED_FEATURE.
PLANNED_FEATURES: tuple[type, ...] = (FlangeCompiler, SlotCompiler, LBracketCompiler)

__all__ = [
    "PLANNED_FEATURES",
    "BoltCirclePatternCompiler",
    "CompileContext",
    "CompiledFeature",
    "FeatureCompiler",
    "FlangeCompiler",
    "InputReport",
    "LBracketCompiler",
    "RectangularHolePatternCompiler",
    "RectangularPlateCompiler",
    "SlotCompiler",
    "describe_all",
    "get_compiler",
    "register",
    "search",
    "supported_types",
]
