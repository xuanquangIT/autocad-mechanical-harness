"""Approval records bound to an exact plan and revision (architecture section 17.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel

#: Approvals are deliberately short lived. A stale approval must never reach commit.
DEFAULT_APPROVAL_TTL = timedelta(minutes=15)


class ApprovalRecord(ContractModel):
    """Who approved what, under which conditions.

    Scope is the triple (job, plan_hash, expected_revision). Changing any of them
    invalidates the approval; there is no partial re-use.
    """

    schema_version: str = SCHEMA_VERSION
    approval_id: str
    job_id: str
    document_id: str
    expected_revision: str
    plan_hash: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    warnings_acknowledged: tuple[str, ...] = ()

    def is_expired(self, now: datetime | None = None) -> bool:
        # The signed interval is closed at its configured TTL boundary; it expires
        # only once elapsed time has exceeded that duration (Property 59).
        return (now or datetime.now(UTC)) > self.expires_at

    def matches(self, *, job_id: str, plan_hash: str, revision: str) -> bool:
        return (
            self.job_id == job_id
            and self.plan_hash == plan_hash
            and self.expected_revision == revision
        )


class RollbackApprovalRecord(ContractModel):
    """One human authorization for one exact destructive restore scope.

    This record is intentionally independent from :class:`ApprovalRecord`: approval
    to create entities never authorizes discarding later drawing work.
    """

    schema_version: str = SCHEMA_VERSION
    approval_id: str
    job_id: str
    document_id: str
    checkpoint_id: str
    current_revision: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) > self.expires_at

    def matches(
        self,
        *,
        job_id: str,
        document_id: str,
        checkpoint_id: str,
        current_revision: str,
    ) -> bool:
        return (
            self.job_id == job_id
            and self.document_id == document_id
            and self.checkpoint_id == checkpoint_id
            and self.current_revision == current_revision
        )
