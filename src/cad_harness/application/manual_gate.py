"""Explicit confirmation gates for every real-AutoCAD manual step."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from cad_harness.domain.errors import ApprovalRequiredError


class ManualStepId(StrEnum):
    OPEN_TARGET_DRAWING = "open_target_drawing"
    LOAD_COMPANY_STANDARDS = "load_company_standards"
    INSTALL_BRIDGE_BUNDLE = "install_bridge_bundle"
    GRANT_NAMED_PIPE_ACL = "grant_named_pipe_acl"
    CONFIRM_AUTOCAD_VERSION = "confirm_autocad_version"
    APPROVE_COMMIT = "approve_commit"


MANUAL_STEP_INSTRUCTIONS: dict[ManualStepId, str] = {
    ManualStepId.OPEN_TARGET_DRAWING: (
        "Open AutoCAD with the disposable target drawing and verify it is the active document."
    ),
    ManualStepId.LOAD_COMPANY_STANDARDS: (
        "Load the controlled company DWT and DWS files into the target drawing."
    ),
    ManualStepId.INSTALL_BRIDGE_BUNDLE: (
        "Install the signed C# Bridge .bundle for production, or an explicitly marked "
        "development-unsigned bundle only in a PID-owned disposable acceptance session."
    ),
    ManualStepId.GRANT_NAMED_PIPE_ACL: (
        "Grant the current Windows user access to the bridge Named Pipe ACL."
    ),
    ManualStepId.CONFIRM_AUTOCAD_VERSION: (
        "Confirm the running AutoCAD version matches the published compatibility matrix."
    ),
    ManualStepId.APPROVE_COMMIT: (
        "Review the exact preview, validation findings, plan hash and revision, "
        "then approve commit."
    ),
}

# COM status/inspection prove the exact PID, document, revision and compatible version
# before this evidence is signed. The remaining standards assertion cannot yet be
# derived from a controlled DWT/DWS fingerprint, so it stays human-confirmed.
COM_LIVE_SETUP_STEPS: tuple[ManualStepId, ...] = (ManualStepId.LOAD_COMPANY_STANDARDS,)

# Backward-compatible name for the full bridge setup sequence. Bridge-only
# installation and pipe ACL evidence must never be requested from a COM session.
LIVE_SETUP_STEPS: tuple[ManualStepId, ...] = (
    ManualStepId.OPEN_TARGET_DRAWING,
    ManualStepId.LOAD_COMPANY_STANDARDS,
    ManualStepId.INSTALL_BRIDGE_BUNDLE,
    ManualStepId.GRANT_NAMED_PIPE_ACL,
    ManualStepId.CONFIRM_AUTOCAD_VERSION,
)

_MANUAL_CONFIRMATIONS_ENV = "CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS"


def required_live_setup_steps(adapter_type: str) -> tuple[ManualStepId, ...]:
    """Return the exact ordered setup evidence required by one live adapter."""
    normalized = adapter_type.strip().lower()
    if normalized == "com":
        return COM_LIVE_SETUP_STEPS
    if normalized == "dotnet_bridge":
        return LIVE_SETUP_STEPS
    return ()


@dataclass(frozen=True, slots=True)
class ManualStep:
    step_id: ManualStepId
    instruction: str


class ManualGate:
    """Advance exactly one callback only after the matching human confirmation."""

    def __init__(self, steps: Sequence[ManualStep]) -> None:
        if not steps:
            raise ValueError("ManualGate requires at least one step")
        if len({step.step_id for step in steps}) != len(steps):
            raise ValueError("ManualGate step identifiers must be unique")
        self._steps = tuple(steps)
        self._index = 0
        self._confirmed = False

    @classmethod
    def live_autocad(cls, adapter_type: str = "dotnet_bridge") -> ManualGate:
        setup_steps = required_live_setup_steps(adapter_type)
        if not setup_steps:
            raise ValueError("Live AutoCAD manual gate requires a live adapter")
        return cls(
            tuple(
                ManualStep(step_id, MANUAL_STEP_INSTRUCTIONS[step_id])
                for step_id in (*setup_steps, ManualStepId.APPROVE_COMMIT)
            )
        )

    @property
    def complete(self) -> bool:
        return self._index == len(self._steps)

    @property
    def current_step(self) -> ManualStep | None:
        return None if self.complete else self._steps[self._index]

    def notification(self) -> str:
        step = self.current_step
        if step is None:
            return "All required manual steps are complete."
        return f"Manual step [{step.step_id.value}]: {step.instruction}"

    def confirm(self, step_id: ManualStepId) -> None:
        step = self.current_step
        if step is None:
            raise ValueError("All manual steps are already complete")
        if step_id is not step.step_id:
            raise ApprovalRequiredError(
                "Confirmation does not match the current manual step",
                required_action=self.notification(),
                details={
                    "expected_step_id": step.step_id.value,
                    "received_step_id": step_id.value,
                },
            )
        self._confirmed = True

    def run_next[T](self, action: Callable[[], T]) -> T:
        step = self.current_step
        if step is None:
            raise ValueError("All manual steps are already complete")
        if not self._confirmed:
            raise ApprovalRequiredError(
                "Manual confirmation is required before the next action",
                required_action=self.notification(),
                details={"step_id": step.step_id.value},
            )
        result = action()
        self._index += 1
        self._confirmed = False
        return result


def load_live_setup_confirmations_from_environment() -> tuple[ManualStepId, ...]:
    """Read explicit startup evidence for a non-interactive MCP host.

    The value is a comma-separated sequence of the adapter-specific setup step ids.
    The commit-approval step is deliberately excluded because it belongs to the
    Engineer Desktop preview approval flow.
    """
    raw = os.environ.get(_MANUAL_CONFIRMATIONS_ENV, "")
    if not raw.strip():
        return ()
    try:
        return tuple(ManualStepId(item.strip()) for item in raw.split(","))
    except ValueError as exc:
        raise ApprovalRequiredError(
            "Manual setup confirmation evidence contains an unknown step id",
            required_action=(
                f"Set {_MANUAL_CONFIRMATIONS_ENV} to the exact ordered setup step ids "
                "after completing each instruction"
            ),
        ) from exc


def require_live_setup_confirmations(
    adapter_type: str,
    confirmations: Sequence[ManualStepId],
) -> tuple[ManualStepId, ...]:
    """Fail before live adapter construction unless all setup steps are confirmed."""
    expected_steps = required_live_setup_steps(adapter_type)
    if not expected_steps:
        return ()
    provided = tuple(confirmations)
    if provided != expected_steps:
        mismatch_index = next(
            (
                index
                for index, expected in enumerate(expected_steps)
                if index >= len(provided) or provided[index] is not expected
            ),
            len(expected_steps),
        )
        expected = (
            expected_steps[mismatch_index]
            if mismatch_index < len(expected_steps)
            else ManualStepId.APPROVE_COMMIT
        )
        raise ApprovalRequiredError(
            "Live AutoCAD setup confirmations are incomplete or out of order",
            required_action=(
                f"Manual step [{expected.value}]: {MANUAL_STEP_INSTRUCTIONS[expected]}"
            ),
            details={
                "expected_step_id": expected.value,
                "confirmed_count": min(mismatch_index, len(expected_steps)),
            },
        )
    return provided


__all__ = [
    "COM_LIVE_SETUP_STEPS",
    "LIVE_SETUP_STEPS",
    "MANUAL_STEP_INSTRUCTIONS",
    "ManualGate",
    "ManualStep",
    "ManualStepId",
    "load_live_setup_confirmations_from_environment",
    "require_live_setup_confirmations",
    "required_live_setup_steps",
]
