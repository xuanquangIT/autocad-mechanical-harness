"""Pure deterministic audit of an extracted drawing model."""

from __future__ import annotations

from collections.abc import Mapping

from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.models.drawing_model import DrawingModel
from cad_harness.domain.models.validation import ValidationReport, ValidationStage
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.validation.engine import RuleContext, default_engine


def audit_drawing(
    model: DrawingModel,
    *,
    profile: CompanyProfile,
    tolerance: ToleranceProfile,
    job_id: str = "drawing-audit",
    expected_layers_by_ref: Mapping[str, str] | None = None,
) -> ValidationReport:
    """Run geometry and standard rules in stable rule-id order without any adapter I/O."""

    context = RuleContext(
        profile=profile,
        tolerance=tolerance,
        drawing_model=model,
        extras={"expected_layers_by_ref": dict(expected_layers_by_ref or {})},
    )
    engine = default_engine()
    rules = {
        rule.rule_id: rule
        for stage in (ValidationStage.DRAWING_AUDIT, ValidationStage.DRAWING_STANDARD)
        for rule in engine.rules_for(stage)
    }
    findings = tuple(
        finding for rule_id in sorted(rules) for finding in rules[rule_id].evaluate(context)
    )
    return ValidationReport(
        validation_id=new_id(IdPrefix.VALIDATION),
        job_id=job_id,
        stage=ValidationStage.DRAWING_AUDIT,
        findings=findings,
        entities_examined=len(model.entities),
        company_approved=profile.company_approved,
        profile_ref=profile.as_ref(),
    )


__all__ = ["audit_drawing"]
