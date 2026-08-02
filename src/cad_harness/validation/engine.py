"""Validation engine.

Rules are small, independent and stage-scoped. The engine only collects findings; the
decision to block belongs to :meth:`ValidationReport.gate_allows_commit`, so policy
stays in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.result import CommitResult
from cad_harness.domain.models.validation import (
    Finding,
    ValidationReport,
    ValidationStage,
)
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.geometry.tolerance import ToleranceProfile


@dataclass(slots=True)
class RuleContext:
    """Everything a rule may read. Rules never perform I/O."""

    plan: OperationPlan
    profile: CompanyProfile
    tolerance: ToleranceProfile
    #: Present only at the post-commit stage.
    commit_result: CommitResult | None = None
    extras: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class ValidationRule(Protocol):
    """A rule is read-only state plus one pure evaluation.

    Members are declared as properties so frozen dataclasses satisfy the protocol; a
    settable attribute would also force exact tuple types on implementations.
    """

    @property
    def rule_id(self) -> str: ...

    @property
    def stages(self) -> tuple[ValidationStage, ...]: ...

    def evaluate(self, context: RuleContext) -> list[Finding]: ...


class ValidationEngine:
    """Runs the rules registered for a stage."""

    def __init__(self, rules: list[ValidationRule] | None = None) -> None:
        self._rules: list[ValidationRule] = list(rules or [])

    def register(self, rule: ValidationRule) -> None:
        if any(existing.rule_id == rule.rule_id for existing in self._rules):
            raise ValueError(f"Duplicate validation rule id: {rule.rule_id}")
        self._rules.append(rule)

    def rules_for(self, stage: ValidationStage) -> list[ValidationRule]:
        return [rule for rule in self._rules if stage in rule.stages]

    def rule_ids(self) -> list[str]:
        return sorted(rule.rule_id for rule in self._rules)

    def run(self, stage: ValidationStage, context: RuleContext, *, job_id: str) -> ValidationReport:
        findings: list[Finding] = []
        for rule in self.rules_for(stage):
            findings.extend(rule.evaluate(context))
        return ValidationReport(
            validation_id=new_id(IdPrefix.VALIDATION),
            job_id=job_id,
            stage=stage,
            plan_hash=context.plan.plan_hash,
            findings=tuple(findings),
        )


def default_engine() -> ValidationEngine:
    """Engine with every shipped rule registered."""
    from cad_harness.validation.rules import all_rules

    return ValidationEngine(all_rules())
