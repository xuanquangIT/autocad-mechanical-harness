"""Shipped validation rules. Add a rule here and it is picked up by the default engine."""

from cad_harness.validation.engine import ValidationRule
from cad_harness.validation.rules.annotation_rules import (
    AnnotationOverlapRule,
    AnnotationProfileRule,
    DimensionTextMatchesGeometryRule,
    GdtDatumExistsRule,
)
from cad_harness.validation.rules.drawing_audit_rules import (
    DimstyleInProfileRule,
    DocumentUnitsMatchProfileRule,
    EntityOnExpectedLayerRule,
    LayerSetMatchesProfileRule,
    TextstyleInProfileRule,
)
from cad_harness.validation.rules.drawing_geometry_audit_rules import (
    DuplicateEntityRule,
    FilletNotTangentRule,
    HoleEdgeDistanceRule,
    HoleLigamentRule,
    HoleOutsidePartRule,
    InvalidArcRadiusRule,
    OpenContourRule,
    OverlappingEntityRule,
    SelfIntersectingContourRule,
    ZeroLengthEntityRule,
)
from cad_harness.validation.rules.feature_rules import (
    FlangeHolesOnPcdRule,
    FlangeOuterDiameterClearanceRule,
    LBracketLegPerpendicularityRule,
    NoUndeclaredContourIntersectionRule,
    ReferenceCircleGeometryRule,
    SlotArcTangencyRule,
)
from cad_harness.validation.rules.geometry_rules import (
    ClosedOutlineRule,
    FiniteCoordinatesRule,
    HolePlacementRule,
    HoleSpacingRule,
    PatternIntegrityRule,
)
from cad_harness.validation.rules.layout_rules import (
    DwsLayerRule,
    LayoutProfileRule,
    NoUndeclaredLayerRule,
    ViewProjectionAlignmentRule,
)
from cad_harness.validation.rules.post_commit_rules import (
    EveryOperationProducedEntityRule,
    MeasurementMatchesExpectationRule,
)
from cad_harness.validation.rules.standard_rules import (
    CanonicalUnitsRule,
    GeneralToleranceDeclaredRule,
    LayerDeclaredRule,
    ProfileProvenanceRule,
)


def all_rules() -> list[ValidationRule]:
    return [
        FiniteCoordinatesRule(),
        ClosedOutlineRule(),
        HolePlacementRule(),
        HoleSpacingRule(),
        PatternIntegrityRule(),
        FlangeOuterDiameterClearanceRule(),
        FlangeHolesOnPcdRule(),
        SlotArcTangencyRule(),
        LBracketLegPerpendicularityRule(),
        NoUndeclaredContourIntersectionRule(),
        ReferenceCircleGeometryRule(),
        CanonicalUnitsRule(),
        LayerDeclaredRule(),
        ProfileProvenanceRule(),
        GeneralToleranceDeclaredRule(),
        DimensionTextMatchesGeometryRule(),
        AnnotationOverlapRule(),
        AnnotationProfileRule(),
        GdtDatumExistsRule(),
        NoUndeclaredLayerRule(),
        LayoutProfileRule(),
        DwsLayerRule(),
        ViewProjectionAlignmentRule(),
        EveryOperationProducedEntityRule(),
        MeasurementMatchesExpectationRule(),
        LayerSetMatchesProfileRule(),
        EntityOnExpectedLayerRule(),
        DimstyleInProfileRule(),
        TextstyleInProfileRule(),
        DocumentUnitsMatchProfileRule(),
        ZeroLengthEntityRule(),
        OpenContourRule(),
        SelfIntersectingContourRule(),
        DuplicateEntityRule(),
        OverlappingEntityRule(),
        HoleOutsidePartRule(),
        HoleEdgeDistanceRule(),
        HoleLigamentRule(),
        InvalidArcRadiusRule(),
        FilletNotTangentRule(),
    ]


__all__ = [
    "CanonicalUnitsRule",
    "ClosedOutlineRule",
    "DimstyleInProfileRule",
    "DocumentUnitsMatchProfileRule",
    "DuplicateEntityRule",
    "EntityOnExpectedLayerRule",
    "EveryOperationProducedEntityRule",
    "FilletNotTangentRule",
    "FiniteCoordinatesRule",
    "FlangeHolesOnPcdRule",
    "FlangeOuterDiameterClearanceRule",
    "GeneralToleranceDeclaredRule",
    "HoleEdgeDistanceRule",
    "HoleLigamentRule",
    "HoleOutsidePartRule",
    "HolePlacementRule",
    "HoleSpacingRule",
    "InvalidArcRadiusRule",
    "LBracketLegPerpendicularityRule",
    "LayerDeclaredRule",
    "LayerSetMatchesProfileRule",
    "MeasurementMatchesExpectationRule",
    "NoUndeclaredContourIntersectionRule",
    "OpenContourRule",
    "OverlappingEntityRule",
    "PatternIntegrityRule",
    "ProfileProvenanceRule",
    "ReferenceCircleGeometryRule",
    "SelfIntersectingContourRule",
    "SlotArcTangencyRule",
    "TextstyleInProfileRule",
    "ZeroLengthEntityRule",
    "all_rules",
]
