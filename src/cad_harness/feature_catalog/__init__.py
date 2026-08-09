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
from cad_harness.feature_catalog.corner_notch import CornerNotchCompiler
from cad_harness.feature_catalog.edge_cutout import EdgeCutoutCompiler
from cad_harness.feature_catalog.flange import FlangeCompiler
from cad_harness.feature_catalog.hole_pattern import (
    BoltCirclePatternCompiler,
    RectangularHolePatternCompiler,
)
from cad_harness.feature_catalog.keyway import KeywayCompiler
from cad_harness.feature_catalog.linear_hole_pattern import LinearHolePatternCompiler
from cad_harness.feature_catalog.plate import RectangularPlateCompiler
from cad_harness.feature_catalog.recognized import (
    RecognizedChamferCornerCompiler,
    RecognizedCircularHoleCompiler,
    RecognizedFilletCornerCompiler,
    RecognizedPartOutlineCompiler,
)
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
register(FlangeCompiler())
register(SlotCompiler())
register(LBracketCompiler())
register(CornerNotchCompiler())
register(EdgeCutoutCompiler())
register(KeywayCompiler())
register(LinearHolePatternCompiler())
register(RecognizedPartOutlineCompiler())
register(RecognizedCircularHoleCompiler())
register(RecognizedFilletCornerCompiler())
register(RecognizedChamferCornerCompiler())

#: Feature classes declared for future delivery. An empty tuple means every currently
#: declared feature meets the catalog Definition of Done.
PLANNED_FEATURES: tuple[type, ...] = ()

__all__ = [
    "PLANNED_FEATURES",
    "BoltCirclePatternCompiler",
    "CompileContext",
    "CompiledFeature",
    "CornerNotchCompiler",
    "EdgeCutoutCompiler",
    "FeatureCompiler",
    "FlangeCompiler",
    "InputReport",
    "KeywayCompiler",
    "LBracketCompiler",
    "LinearHolePatternCompiler",
    "RectangularHolePatternCompiler",
    "RectangularPlateCompiler",
    "SlotCompiler",
    "describe_all",
    "get_compiler",
    "register",
    "search",
    "supported_types",
]
