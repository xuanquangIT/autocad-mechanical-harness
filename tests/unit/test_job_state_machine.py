"""Job state machine (architecture section 8.2)."""

from __future__ import annotations

import pytest

from cad_harness.domain.errors import InvalidJobTransitionError
from cad_harness.domain.models.job import ALLOWED_TRANSITIONS, CadJob, JobState, assert_transition


@pytest.fixture
def job() -> CadJob:
    return CadJob(job_id="job_1", document_id="doc_1", expected_revision="sha256:r1")


class TestTransitions:
    def test_happy_path(self, job: CadJob) -> None:
        for state in (
            JobState.SPEC_ACCEPTED,
            JobState.PLANNED,
            JobState.PREVIEWED,
            JobState.VALIDATED,
            JobState.APPROVED,
            JobState.COMMITTING,
            JobState.COMMITTED,
        ):
            job = job.transition_to(state)
        assert job.state is JobState.COMMITTED

    def test_cannot_skip_to_commit(self, job: CadJob) -> None:
        with pytest.raises(InvalidJobTransitionError) as info:
            job.transition_to(JobState.COMMITTING)
        assert info.value.details["current_state"] == "CREATED"

    def test_cannot_commit_without_approval_state(self, job: CadJob) -> None:
        validated = (
            job.transition_to(JobState.SPEC_ACCEPTED)
            .transition_to(JobState.PLANNED)
            .transition_to(JobState.PREVIEWED)
            .transition_to(JobState.VALIDATED)
        )
        with pytest.raises(InvalidJobTransitionError):
            validated.transition_to(JobState.COMMITTING)

    def test_terminal_states_have_no_exits(self) -> None:
        assert ALLOWED_TRANSITIONS[JobState.ROLLED_BACK] == frozenset()
        assert ALLOWED_TRANSITIONS[JobState.CANCELLED] == frozenset()

    def test_failed_can_be_replanned(self) -> None:
        assert JobState.PLANNED in ALLOWED_TRANSITIONS[JobState.FAILED]

    def test_every_state_has_a_transition_entry(self) -> None:
        assert set(ALLOWED_TRANSITIONS) == set(JobState)

    def test_transitions_are_immutable(self, job: CadJob) -> None:
        moved = job.transition_to(JobState.SPEC_ACCEPTED)
        assert job.state is JobState.CREATED
        assert moved is not job

    def test_assert_transition_accepts_valid_move(self) -> None:
        assert_transition(JobState.APPROVED, JobState.COMMITTING)


class TestApprovalInvalidation:
    def test_spec_change_clears_approval(self, job: CadJob) -> None:
        approved = job.model_copy(update={"approval_id": "approval_1"})
        assert approved.invalidate_approval().approval_id is None

    def test_approved_job_can_return_to_spec_accepted(self, job: CadJob) -> None:
        """A revised spec must be able to reopen an approved job."""
        assert JobState.SPEC_ACCEPTED in ALLOWED_TRANSITIONS[JobState.APPROVED]
