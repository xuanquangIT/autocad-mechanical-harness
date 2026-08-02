"""Company drawing standard rules (architecture section 15.2)."""

from __future__ import annotations

from dataclasses import dataclass

from cad_harness.domain.models.validation import Finding, Severity, ValidationStage
from cad_harness.domain.value_objects.units import CANONICAL_UNIT
from cad_harness.validation.engine import RuleContext
from cad_harness.validation.findings import finding

STANDARD_STAGES = (
    ValidationStage.PLAN,
    ValidationStage.COMPANY_STANDARD,
    ValidationStage.PRE_COMMIT,
)


@dataclass(frozen=True, slots=True)
class CanonicalUnitsRule:
    """A plan in non-canonical units means normalization was skipped somewhere."""

    rule_id: str = "STD-UNITS-CANONICAL"
    stages: tuple[ValidationStage, ...] = STANDARD_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        if context.plan.canonical_units is CANONICAL_UNIT:
            return []
        return [
            finding(
                self.rule_id,
                Severity.BLOCKING,
                "Operation plan is not in canonical units",
                expected=CANONICAL_UNIT.value,
                actual=context.plan.canonical_units.value,
                suggested_fix="Normalize the spec to millimetres before compiling",
            )
        ]


@dataclass(frozen=True, slots=True)
class LayerDeclaredRule:
    """Every operation must target a layer the profile declares."""

    rule_id: str = "STD-LAYER-DECLARED"
    stages: tuple[ValidationStage, ...] = STANDARD_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        declared = context.profile.layer_names()
        for operation in context.plan.operations:
            if operation.layer == "0":
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Operation falls back to layer '0' because the purpose is unmapped",
                        feature_id=operation.feature_id,
                        operation_id=operation.operation_id,
                        expected=sorted(declared),
                        actual="0",
                        suggested_fix="Add the missing purpose to layer_map in the profile",
                    )
                )
            elif operation.layer not in declared:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        f"Layer '{operation.layer}' is not declared in the standard profile",
                        feature_id=operation.feature_id,
                        operation_id=operation.operation_id,
                        expected=sorted(declared),
                        actual=operation.layer,
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class ProfileProvenanceRule:
    """A demo profile may produce drawings, but never claim company compliance."""

    rule_id: str = "STD-PROFILE-PROVENANCE"
    stages: tuple[ValidationStage, ...] = STANDARD_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        if not context.profile.company_approved:
            findings.append(
                finding(
                    self.rule_id,
                    Severity.WARNING,
                    "Drawing produced with a profile that is not company approved",
                    expected="company_approved: true",
                    actual=context.profile.as_ref(),
                    suggested_fix="Install and select the reviewed company profile before release",
                )
            )
        if context.plan.profile_ref != context.profile.as_ref():
            findings.append(
                finding(
                    self.rule_id,
                    Severity.BLOCKING,
                    "Plan was compiled against a different profile version than the one loaded",
                    expected=context.plan.profile_ref,
                    actual=context.profile.as_ref(),
                    suggested_fix="Recompile the plan; a profile change invalidates the plan hash",
                )
            )
        return findings


@dataclass(frozen=True, slots=True)
class GeneralToleranceDeclaredRule:
    """A fabrication drawing without a general tolerance class is incomplete."""

    rule_id: str = "STD-GENERAL-TOLERANCE"
    stages: tuple[ValidationStage, ...] = STANDARD_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        if context.profile.general_tolerance:
            return []
        return [
            finding(
                self.rule_id,
                Severity.WARNING,
                "No general tolerance class declared by the standard profile",
                expected="e.g. ISO 2768-m",
                actual=None,
                suggested_fix="Set general_tolerance in the company profile",
            )
        ]
