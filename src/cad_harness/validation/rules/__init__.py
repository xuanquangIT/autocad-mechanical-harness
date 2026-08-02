"""Shipped validation rules. Add a rule here and it is picked up by the default engine."""

from cad_harness.validation.engine import ValidationRule
from cad_harness.validation.rules.geometry_rules import (
    ClosedOutlineRule,
    FiniteCoordinatesRule,
    HolePlacementRule,
    HoleSpacingRule,
    PatternIntegrityRule,
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
        CanonicalUnitsRule(),
        LayerDeclaredRule(),
        ProfileProvenanceRule(),
        GeneralToleranceDeclaredRule(),
        EveryOperationProducedEntityRule(),
        MeasurementMatchesExpectationRule(),
    ]


__all__ = [
    "CanonicalUnitsRule",
    "ClosedOutlineRule",
    "EveryOperationProducedEntityRule",
    "FiniteCoordinatesRule",
    "GeneralToleranceDeclaredRule",
    "HolePlacementRule",
    "HoleSpacingRule",
    "LayerDeclaredRule",
    "MeasurementMatchesExpectationRule",
    "PatternIntegrityRule",
    "ProfileProvenanceRule",
    "all_rules",
]
