"""Approval tokens: short-lived, HMAC-signed, scoped to one plan and revision.

An approval is the engineer's signature on a specific preview. Signing the scope means
a client cannot replay an approval against a different plan, and cannot forge one.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
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


def _scope_claims(approval: ApprovalRecord) -> dict[str, str]:
    """Exactly the signed fields that define the approval's authority."""
    return {
        "approval_id": approval.approval_id,
        "job_id": approval.job_id,
        "document_id": approval.document_id,
        "expected_revision": approval.expected_revision,
        "plan_hash": approval.plan_hash,
        "approved_by": approval.approved_by,
        "expires_at": approval.expires_at.astimezone(UTC).isoformat(),
    }


def _encode_claims(approval: ApprovalRecord) -> str:
    payload = canonical_json(_scope_claims(approval)).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_claims(payload: str) -> dict[str, str]:
    if not payload or any(character not in _BASE64URL_CHARS for character in payload):
        raise ValueError("invalid approval claim encoding")
    padding = "=" * (-len(payload) % 4)
    decoded = base64.b64decode(payload + padding, altchars=b"-_", validate=True)
    value = json.loads(decoded)
    if (
        not isinstance(value, dict)
        or set(value) != _CLAIM_FIELDS
        or not all(isinstance(item, str) for item in value.values())
    ):
        raise ValueError("invalid approval claims")
    return value


_CLAIM_FIELDS = frozenset(
    {
        "approval_id",
        "job_id",
        "document_id",
        "expected_revision",
        "plan_hash",
        "approved_by",
        "expires_at",
    }
)
_BASE64URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def make_approval_token(approval: ApprovalRecord, secret: str) -> str:
    """Sign an approval record. An empty secret is a configuration error."""
    if not secret:
        raise ApprovalRequiredError(
            "No approval signing secret is configured",
            required_action="Set CAD_HARNESS_APPROVAL_SECRET on this workstation",
        )
    payload = _encode_claims(approval)
    digest = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), sha256).hexdigest()
    return f"v2.{payload}.{digest}"


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
    try:
        version, payload, received_digest = token.split(".", 2)
        if version != "v2" or len(received_digest) != 64:
            raise ValueError("invalid approval token shape")
        expected_digest = hmac.new(
            secret.encode("utf-8"), payload.encode("ascii"), sha256
        ).hexdigest()
    except (UnicodeEncodeError, ValueError):
        expected_digest = ""
        payload = ""
        received_digest = ""
    if not hmac.compare_digest(received_digest, expected_digest):
        raise ApprovalScopeMismatchError(
            "Approval token signature is invalid",
            required_action="Request a fresh approval for the current preview",
        )
    try:
        claims = _decode_claims(payload)
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ApprovalScopeMismatchError(
            "Approval token claims are invalid",
            required_action="Request a fresh approval for the current preview",
        ) from exc
    if not hmac.compare_digest(
        canonical_json(claims).encode("utf-8"),
        canonical_json(_scope_claims(approval)).encode("utf-8"),
    ):
        raise ApprovalScopeMismatchError(
            "Approval token does not match the stored approval",
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
