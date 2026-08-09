"""Pure approval eligibility for the engineer desktop surface."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

from cad_harness.domain.models.validation import Severity, ValidationReport


class ApprovalReason(StrEnum):
    """Why the desktop must keep the approval action disabled."""

    PLAN_HASH_CHANGED = "plan_hash_changed"
    REVISION_CHANGED = "revision_changed"
    BLOCKING_FINDINGS = "blocking_findings"
    WARNINGS_UNACKNOWLEDGED = "warnings_unacknowledged"


@dataclass(frozen=True, slots=True)
class ApproveDecision:
    """The complete, displayable result of evaluating approval eligibility."""

    can_approve: bool
    reasons: tuple[ApprovalReason, ...] = ()


def can_approve(
    *,
    displayed_plan_hash: str,
    current_plan_hash: str,
    displayed_revision: str,
    current_revision: str,
    report: ValidationReport,
    acknowledged_warning_rule_ids: Collection[str] = (),
) -> ApproveDecision:
    """Decide approval from immutable UI state without reading or changing CAD.

    Every distinct warning rule must have an explicit acknowledgement.  The order of
    reasons is fixed so identical inputs always yield the same UI state.
    """
    reasons: list[ApprovalReason] = []
    if displayed_plan_hash != current_plan_hash:
        reasons.append(ApprovalReason.PLAN_HASH_CHANGED)
    if displayed_revision != current_revision:
        reasons.append(ApprovalReason.REVISION_CHANGED)
    if report.has_blocking:
        reasons.append(ApprovalReason.BLOCKING_FINDINGS)

    warning_rule_ids = {
        finding.rule_id for finding in report.findings if finding.severity is Severity.WARNING
    }
    if not warning_rule_ids.issubset(acknowledged_warning_rule_ids):
        reasons.append(ApprovalReason.WARNINGS_UNACKNOWLEDGED)

    return ApproveDecision(can_approve=not reasons, reasons=tuple(reasons))
