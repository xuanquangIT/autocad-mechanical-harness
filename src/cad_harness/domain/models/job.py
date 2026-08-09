"""CadJob aggregate and its state machine (architecture section 8.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field

from cad_harness.domain.errors import InvalidJobTransitionError
from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel


class JobState(StrEnum):
    CREATED = "CREATED"
    SPEC_ACCEPTED = "SPEC_ACCEPTED"
    PLANNED = "PLANNED"
    PREVIEWED = "PREVIEWED"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    UNKNOWN_COMMIT_STATE = "UNKNOWN_COMMIT_STATE"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    CANCELLED = "CANCELLED"


#: Allowed transitions. Anything absent here is a programming error, not a user error.
ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.CREATED: frozenset({JobState.SPEC_ACCEPTED, JobState.CANCELLED}),
    JobState.SPEC_ACCEPTED: frozenset({JobState.PLANNED, JobState.CANCELLED}),
    # Re-submitting a changed spec returns the job to SPEC_ACCEPTED and voids approval.
    JobState.PLANNED: frozenset({JobState.PREVIEWED, JobState.SPEC_ACCEPTED, JobState.CANCELLED}),
    JobState.PREVIEWED: frozenset({JobState.VALIDATED, JobState.SPEC_ACCEPTED, JobState.CANCELLED}),
    JobState.VALIDATED: frozenset({JobState.APPROVED, JobState.SPEC_ACCEPTED, JobState.CANCELLED}),
    JobState.APPROVED: frozenset({JobState.COMMITTING, JobState.SPEC_ACCEPTED}),
    JobState.COMMITTING: frozenset(
        {JobState.COMMITTED, JobState.FAILED, JobState.UNKNOWN_COMMIT_STATE}
    ),
    JobState.COMMITTED: frozenset({JobState.ROLLED_BACK}),
    JobState.UNKNOWN_COMMIT_STATE: frozenset(),
    # FAILED may be pre-write (no checkpoint) or a proven post-commit validation
    # failure. HarnessService permits rollback only for the latter.
    JobState.FAILED: frozenset({JobState.PLANNED, JobState.ROLLED_BACK, JobState.CANCELLED}),
    JobState.ROLLED_BACK: frozenset(),
    JobState.CANCELLED: frozenset(),
}


def assert_transition(current: JobState, target: JobState) -> None:
    """Raise if ``current -> target`` is not part of the state machine."""
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidJobTransitionError(
            f"Job cannot move from {current.value} to {target.value}",
            required_action="Restart the workflow from the last valid stage",
            details={
                "current_state": current.value,
                "requested_state": target.value,
                "allowed": sorted(s.value for s in ALLOWED_TRANSITIONS[current]),
            },
        )


class CadJob(ContractModel):
    """Aggregate tracking one change to one document.

    Immutable by design: transitions return a new instance so history can be
    reconstructed from the audit log.
    """

    schema_version: str = SCHEMA_VERSION
    job_id: str
    document_id: str
    #: Pinned when the job is created; re-verified immediately before commit.
    expected_revision: str
    state: JobState = JobState.CREATED
    spec_id: str | None = None
    spec_version: int = 0
    plan_id: str | None = None
    plan_hash: str | None = None
    approval_id: str | None = None
    checkpoint_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def transition_to(self, target: JobState, **updates: object) -> CadJob:
        """Validate and apply a state transition."""
        assert_transition(self.state, target)
        return self.model_copy(update={"state": target, "updated_at": datetime.now(UTC), **updates})

    def invalidate_approval(self) -> CadJob:
        """Any spec or plan change voids a prior approval (section 8.2)."""
        return self.model_copy(update={"approval_id": None, "updated_at": datetime.now(UTC)})
