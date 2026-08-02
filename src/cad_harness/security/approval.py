"""Approval tokens: short-lived, HMAC-signed, scoped to one plan and revision.

An approval is the engineer's signature on a specific preview. Signing the scope means
a client cannot replay an approval against a different plan, and cannot forge one.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from cad_harness.domain.canonical import canonical_json
from cad_harness.domain.errors import (
    ApprovalExpiredError,
    ApprovalRequiredError,
    ApprovalScopeMismatchError,
)
from cad_harness.domain.models.approval import ApprovalRecord
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id


def _scope_payload(approval: ApprovalRecord) -> str:
    """Exactly the fields that define the approval's authority."""
    return canonical_json(
        {
            "approval_id": approval.approval_id,
            "job_id": approval.job_id,
            "document_id": approval.document_id,
            "expected_revision": approval.expected_revision,
            "plan_hash": approval.plan_hash,
            "approved_by": approval.approved_by,
            "expires_at": approval.expires_at.astimezone(UTC).isoformat(),
        }
    )


def make_approval_token(approval: ApprovalRecord, secret: str) -> str:
    """Sign an approval record. An empty secret is a configuration error."""
    if not secret:
        raise ApprovalRequiredError(
            "No approval signing secret is configured",
            required_action="Set CAD_HARNESS_APPROVAL_SECRET on this workstation",
        )
    digest = hmac.new(
        secret.encode("utf-8"), _scope_payload(approval).encode("utf-8"), sha256
    ).hexdigest()
    return f"{approval.approval_id}.{digest}"


def issue_approval(
    *,
    job_id: str,
    document_id: str,
    plan_hash: str,
    expected_revision: str,
    approved_by: str,
    secret: str,
    ttl: timedelta,
    warnings_acknowledged: tuple[str, ...] = (),
) -> tuple[ApprovalRecord, str]:
    """Create and sign an approval. Returns the record and its token."""
    now = datetime.now(UTC)
    approval = ApprovalRecord(
        approval_id=new_id(IdPrefix.APPROVAL),
        job_id=job_id,
        document_id=document_id,
        expected_revision=expected_revision,
        plan_hash=plan_hash,
        approved_by=approved_by,
        approved_at=now,
        expires_at=now + ttl,
        warnings_acknowledged=warnings_acknowledged,
    )
    return approval, make_approval_token(approval, secret)


def verify_approval_token(
    token: str,
    approval: ApprovalRecord,
    secret: str,
    *,
    job_id: str,
    plan_hash: str,
    expected_revision: str,
    now: datetime | None = None,
) -> None:
    """Validate signature, scope and expiry. Raises on any mismatch.

    Order matters: the signature is checked first so scope details are not revealed
    to a caller holding a forged token.
    """
    expected_token = make_approval_token(approval, secret)
    if not hmac.compare_digest(token, expected_token):
        raise ApprovalScopeMismatchError(
            "Approval token signature is invalid",
            required_action="Request a fresh approval for the current preview",
        )
    if approval.is_expired(now):
        raise ApprovalExpiredError(
            "Approval has expired",
            required_action="Regenerate the preview and approve again",
            details={"expires_at": approval.expires_at.isoformat()},
        )
    if not approval.matches(job_id=job_id, plan_hash=plan_hash, revision=expected_revision):
        raise ApprovalScopeMismatchError(
            "Approval does not cover this job, plan or revision",
            required_action="Regenerate the preview and approve the new plan",
            details={
                "approved_plan_hash": approval.plan_hash,
                "submitted_plan_hash": plan_hash,
                "approved_revision": approval.expected_revision,
                "submitted_revision": expected_revision,
            },
        )
