"""Stateless, short-lived authorization for destructive checkpoint restore."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from pydantic import ValidationError

from cad_harness.domain.canonical import canonical_json
from cad_harness.domain.errors import (
    ApprovalExpiredError,
    ApprovalRequiredError,
    ApprovalScopeMismatchError,
)
from cad_harness.domain.models.approval import RollbackApprovalRecord
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id

_TOKEN_VERSION = "rb1"
_BASE64URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_CLAIM_FIELDS = frozenset(
    {
        "schema_version",
        "approval_id",
        "job_id",
        "document_id",
        "checkpoint_id",
        "current_revision",
        "approved_by",
        "approved_at",
        "expires_at",
    }
)


def _claims(approval: RollbackApprovalRecord) -> dict[str, str]:
    return {
        "schema_version": approval.schema_version,
        "approval_id": approval.approval_id,
        "job_id": approval.job_id,
        "document_id": approval.document_id,
        "checkpoint_id": approval.checkpoint_id,
        "current_revision": approval.current_revision,
        "approved_by": approval.approved_by,
        "approved_at": approval.approved_at.astimezone(UTC).isoformat(),
        "expires_at": approval.expires_at.astimezone(UTC).isoformat(),
    }


def _encode(claims: dict[str, str]) -> str:
    payload = canonical_json(claims).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode(payload: str) -> dict[str, str]:
    if not payload or any(character not in _BASE64URL_CHARS for character in payload):
        raise ValueError("invalid rollback approval claim encoding")
    padding = "=" * (-len(payload) % 4)
    decoded = base64.b64decode(payload + padding, altchars=b"-_", validate=True)
    value = json.loads(decoded)
    if (
        not isinstance(value, dict)
        or set(value) != _CLAIM_FIELDS
        or not all(isinstance(item, str) for item in value.values())
    ):
        raise ValueError("invalid rollback approval claims")
    return value


def make_rollback_approval_token(approval: RollbackApprovalRecord, secret: str) -> str:
    """Sign a rollback scope with a token namespace distinct from commit approval."""
    if not secret:
        raise ApprovalRequiredError(
            "No rollback approval signing secret is configured",
            required_action="Set CAD_HARNESS_APPROVAL_SECRET on this workstation",
        )
    payload = _encode(_claims(approval))
    digest = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), sha256).hexdigest()
    return f"{_TOKEN_VERSION}.{payload}.{digest}"


def rollback_approval_token_digest(token: str) -> str:
    """Return the domain-separated digest used to bind one durable restore attempt."""
    return sha256(("cad-harness-rb1-token-v1\0" + token).encode("utf-8")).hexdigest()


def issue_rollback_approval(
    *,
    job_id: str,
    document_id: str,
    checkpoint_id: str,
    current_revision: str,
    approved_by: str,
    secret: str,
    ttl: timedelta,
    now: datetime | None = None,
) -> tuple[RollbackApprovalRecord, str]:
    """Issue authority for one checkpoint at the revision reviewed by a human."""
    if not approved_by.strip():
        raise ApprovalRequiredError(
            "Rollback approval requires an identified engineer",
            required_action="Sign in or provide the engineer identity in the desktop UI",
        )
    issued_at = now or datetime.now(UTC)
    approval = RollbackApprovalRecord(
        approval_id=new_id(IdPrefix.APPROVAL),
        job_id=job_id,
        document_id=document_id,
        checkpoint_id=checkpoint_id,
        current_revision=current_revision,
        approved_by=approved_by,
        approved_at=issued_at,
        expires_at=issued_at + ttl,
    )
    return approval, make_rollback_approval_token(approval, secret)


def verify_rollback_approval_token(
    token: str,
    secret: str,
    *,
    job_id: str,
    document_id: str,
    checkpoint_id: str,
    current_revision: str,
    now: datetime | None = None,
) -> RollbackApprovalRecord:
    """Verify signature, exact restore scope and expiry, returning signed identity."""
    approval = _verify_rollback_approval_claims(
        token,
        secret,
        job_id=job_id,
        document_id=document_id,
        checkpoint_id=checkpoint_id,
        current_revision=current_revision,
    )
    if approval.is_expired(now):
        raise ApprovalExpiredError(
            "Rollback approval has expired",
            required_action="Review the current revision and approve rollback again",
            details={"expires_at": approval.expires_at.isoformat()},
        )
    return approval


def verify_rollback_recovery_token(
    token: str,
    secret: str,
    *,
    job_id: str,
    document_id: str,
    checkpoint_id: str,
    current_revision: str,
    now: datetime | None = None,
) -> RollbackApprovalRecord:
    """Authenticate an expired rb1 token for an already-journaled restore only.

    The caller must restrict this path to the real .NET bridge checkpoint-restore
    adapter.  The C# authenticated journal proves the exact token digest and scope,
    including after a Python restart, and cannot start a new replacement from this
    expired token.
    """
    approval = _verify_rollback_approval_claims(
        token,
        secret,
        job_id=job_id,
        document_id=document_id,
        checkpoint_id=checkpoint_id,
        current_revision=current_revision,
    )
    if not approval.is_expired(now):
        raise ApprovalScopeMismatchError(
            "Rollback recovery verification requires the expired original approval",
            required_action="Use normal rollback verification for an unexpired approval",
        )
    return approval


def _verify_rollback_approval_claims(
    token: str,
    secret: str,
    *,
    job_id: str,
    document_id: str,
    checkpoint_id: str,
    current_revision: str,
) -> RollbackApprovalRecord:
    """Verify rb1 namespace, HMAC, schema and exact scope without expiry policy."""
    if not secret:
        raise ApprovalRequiredError(
            "No rollback approval signing secret is configured",
            required_action="Set CAD_HARNESS_APPROVAL_SECRET on this workstation",
        )
    try:
        version, payload, received_digest = token.split(".", 2)
        if version != _TOKEN_VERSION or len(received_digest) != 64:
            raise ValueError("invalid rollback approval token shape")
        expected_digest = hmac.new(
            secret.encode("utf-8"), payload.encode("ascii"), sha256
        ).hexdigest()
    except (UnicodeEncodeError, ValueError):
        payload = ""
        received_digest = ""
        expected_digest = ""
    if not hmac.compare_digest(received_digest, expected_digest):
        raise ApprovalScopeMismatchError(
            "Rollback approval token signature is invalid",
            required_action="Request a fresh rollback approval in the engineer desktop",
        )
    try:
        claims = _decode(payload)
        approval = RollbackApprovalRecord.model_validate(claims)
    except (
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise ApprovalScopeMismatchError(
            "Rollback approval token claims are invalid",
            required_action="Request a fresh rollback approval in the engineer desktop",
        ) from exc
    if approval.schema_version != SCHEMA_VERSION:
        raise ApprovalScopeMismatchError(
            "Rollback approval contract version is not current",
            required_action="Request a fresh rollback approval in the engineer desktop",
            details={"approved_schema_version": approval.schema_version},
        )
    if not approval.matches(
        job_id=job_id,
        document_id=document_id,
        checkpoint_id=checkpoint_id,
        current_revision=current_revision,
    ):
        raise ApprovalScopeMismatchError(
            "Rollback approval does not cover this checkpoint and current revision",
            required_action=(
                "Review the exact current drawing and request a fresh rollback approval"
            ),
            details={
                "approved_job_id": approval.job_id,
                "approved_document_id": approval.document_id,
                "approved_checkpoint_id": approval.checkpoint_id,
                "approved_revision": approval.current_revision,
            },
        )
    return approval
