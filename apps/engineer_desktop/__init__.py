"""Human-only PySide6 approval surface over :class:`HarnessService`."""

from apps.engineer_desktop.approval_gate import ApproveDecision, can_approve
from apps.engineer_desktop.controller import (
    ApprovalEligibility,
    ApprovalOutcome,
    EngineerDesktopController,
)
from apps.engineer_desktop.effort_session import EngineerEffortSession
from apps.engineer_desktop.view_model import ApprovalViewModel

__all__ = [
    "ApprovalEligibility",
    "ApprovalOutcome",
    "ApprovalViewModel",
    "ApproveDecision",
    "EngineerDesktopController",
    "EngineerEffortSession",
    "can_approve",
]
