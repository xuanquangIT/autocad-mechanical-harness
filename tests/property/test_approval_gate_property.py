"""Property 60: engineer approval is enabled exactly for eligible UI state."""

from __future__ import annotations

from apps.engineer_desktop.approval_gate import ApprovalReason, can_approve
from hypothesis import given
from hypothesis import strategies as st

from cad_harness.domain.models.validation import (
    Finding,
    Severity,
    ValidationReport,
    ValidationStage,
)


def _report(*, blocking: bool, warning_rule_ids: tuple[str, ...]) -> ValidationReport:
    findings = tuple(
        [
            Finding(rule_id="BLOCK", severity=Severity.BLOCKING, message="must fix")
            for _ in range(blocking)
        ]
        + [
            Finding(rule_id=rule_id, severity=Severity.WARNING, message="acknowledge")
            for rule_id in warning_rule_ids
        ]
    )
    return ValidationReport(
        validation_id="validation",
        job_id="job",
        stage=ValidationStage.PRE_COMMIT,
        findings=findings,
    )


# Feature: cad-ai-production-roadmap, Property 60: approval gate iff all conditions hold
@given(
    plan_matches=st.booleans(),
    revision_matches=st.booleans(),
    blocking=st.booleans(),
    warning_rule_ids=st.lists(st.sampled_from(("WARN-A", "WARN-B", "WARN-C")), unique=True).map(
        tuple
    ),
    acknowledged_warning_rule_ids=st.lists(
        st.sampled_from(("WARN-A", "WARN-B", "WARN-C")), unique=True
    ).map(tuple),
)
def test_approval_gate_iff_all_required_conditions(
    plan_matches: bool,
    revision_matches: bool,
    blocking: bool,
    warning_rule_ids: tuple[str, ...],
    acknowledged_warning_rule_ids: tuple[str, ...],
) -> None:
    """**Validates: Requirements 25.1, Property 60**"""
    decision = can_approve(
        displayed_plan_hash="displayed-plan",
        current_plan_hash="displayed-plan" if plan_matches else "current-plan",
        displayed_revision="displayed-revision",
        current_revision="displayed-revision" if revision_matches else "current-revision",
        report=_report(blocking=blocking, warning_rule_ids=warning_rule_ids),
        acknowledged_warning_rule_ids=acknowledged_warning_rule_ids,
    )

    expected = (
        plan_matches
        and revision_matches
        and not blocking
        and set(warning_rule_ids).issubset(acknowledged_warning_rule_ids)
    )
    assert decision.can_approve is expected
    assert bool(decision.reasons) is not expected
    assert (ApprovalReason.PLAN_HASH_CHANGED in decision.reasons) is not plan_matches
    assert (ApprovalReason.REVISION_CHANGED in decision.reasons) is not revision_matches
    assert (ApprovalReason.BLOCKING_FINDINGS in decision.reasons) is blocking
    assert (ApprovalReason.WARNINGS_UNACKNOWLEDGED in decision.reasons) is not set(
        warning_rule_ids
    ).issubset(acknowledged_warning_rule_ids)
