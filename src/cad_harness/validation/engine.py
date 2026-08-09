"""Validation engine.

Rules are small, independent and stage-scoped. The engine only collects findings; the
decision to block belongs to :meth:`ValidationReport.gate_allows_commit`, so policy
stays in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.errors import HarnessError
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.result import CommitResult
from cad_harness.domain.models.validation import (
    Finding,
    ValidationReport,
    ValidationStage,
)
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.geometry.tolerance import ToleranceProfile


@runtime_checkable
class DrawingModelLike(Protocol):
    """Structural stand-in for the ``DrawingModel`` read contract.

    ``DrawingModel`` lands with the drawing reader; declaring the shape the rules
    need lets ``RuleContext`` carry a read model today without importing a module
    that does not exist yet. The real contract satisfies this protocol
    structurally, so adopting it later is an annotation change, not a refactor.
    """

    @property
    def document_id(self) -> str: ...

    @property
    def revision(self) -> str: ...


@dataclass(slots=True, kw_only=True)
class RuleContext:
    """Everything a rule may read. Rules never perform I/O.

    A rule reads either a plan (write direction) or a drawing model (audit
    direction), so both are optional and exactly one is populated per run. Fields
    are keyword-only: positional construction would silently change meaning as the
    context grows.
    """

    profile: CompanyProfile
    tolerance: ToleranceProfile
    #: Present for the plan-facing stages. ``None`` when auditing an existing drawing.
    plan: OperationPlan | None = None
    #: Present for the ``DRAWING_AUDIT`` and ``DRAWING_STANDARD`` stages.
    drawing_model: DrawingModelLike | None = None
    #: Present only at the post-commit stage.
    commit_result: CommitResult | None = None
    extras: dict[str, object] = field(default_factory=dict)

    def require_plan(self) -> OperationPlan:
        """Return the plan, or fail loudly.

        A plan-facing rule reached without a plan is a wiring bug in the caller.
        Returning no findings instead would report a clean drawing that was never
        actually checked.
        """
        if self.plan is None:
            raise HarnessError(
                "Validation rule requires an operation plan but the context has none",
                required_action=(
                    "Run this rule at a plan-facing stage, or register it for a drawing stage"
                ),
            )
        return self.plan

    def require_drawing_model(self) -> DrawingModelLike:
        """Return the drawing model, or fail loudly. See :meth:`require_plan`."""
        if self.drawing_model is None:
            raise HarnessError(
                "Validation rule requires a drawing model but the context has none",
                required_action="Read the drawing before running drawing-stage rules",
            )
        return self.drawing_model


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

    def run(
        self,
        stage: ValidationStage,
        context: RuleContext,
        *,
        job_id: str,
        entities_examined: int | None = None,
    ) -> ValidationReport:
        """Run every rule registered for ``stage`` and collect their findings.

        ``entities_examined`` lets a drawing-facing caller report the real scope it
        read; plan-facing stages fall back to the operation count.
        """
        findings: list[Finding] = []
        for rule in self.rules_for(stage):
            findings.extend(rule.evaluate(context))
        examined = entities_examined
        if examined is None:
            examined = len(context.plan.operations) if context.plan is not None else 0
        return ValidationReport(
            validation_id=new_id(IdPrefix.VALIDATION),
            job_id=job_id,
            stage=stage,
            plan_hash=context.plan.plan_hash if context.plan is not None else None,
            findings=tuple(findings),
            entities_examined=examined,
            # Requirement 10.6: every report states whether it was judged against a
            # company-approved profile. A demo profile must never look approved.
            company_approved=context.profile.company_approved,
            profile_ref=context.profile.as_ref(),
        )


def default_engine() -> ValidationEngine:
    """Engine with every shipped rule registered."""
    from cad_harness.validation.rules import all_rules

    return ValidationEngine(all_rules())
