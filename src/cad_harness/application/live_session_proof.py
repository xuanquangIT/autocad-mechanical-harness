"""Short-lived proof binding manual live setup to one AutoCAD session.

The proof is deliberately separate from commit approval.  It authorizes exposing a
live adapter's write capability for one exact process, document, revision and company
profile; every individual plan still requires its ordinary engineer approval token.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from cad_harness.application.manual_gate import ManualStepId, required_live_setup_steps
from cad_harness.domain.canonical import canonical_json
from cad_harness.domain.errors import (
    ApprovalExpiredError,
    ApprovalRequiredError,
    ApprovalScopeMismatchError,
)

_TOKEN_VERSION = "lsp2"
_SIGNING_DOMAIN = b"cad-harness-live-session-proof-v2\0"
_MAX_TOKEN_LENGTH = 4096
_MAX_TTL = timedelta(minutes=15)
_BASE64URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_CLAIM_FIELDS = frozenset(
    {
        "schema_version",
        "adapter_type",
        "process_id",
        "document_id",
        "revision",
        "company_profile",
        "setup_steps",
        "issued_at",
        "expires_at",
        "nonce",
    }
)
LIVE_SESSION_PROOF_ENV = "CAD_HARNESS_LIVE_SESSION_PROOF"


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("live session proof timestamps must be timezone-aware")
    return int(value.astimezone(UTC).timestamp())


def _claims(
    *,
    adapter_type: str,
    process_id: int,
    document_id: str,
    revision: str,
    company_profile: str,
    setup_steps: Sequence[ManualStepId],
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "adapter_type": adapter_type,
        "process_id": process_id,
        "document_id": document_id,
        "revision": revision,
        "company_profile": company_profile,
        "setup_steps": [step.value for step in setup_steps],
        "issued_at": _timestamp(issued_at),
        "expires_at": _timestamp(expires_at),
        "nonce": nonce,
    }


def _encode(claims: dict[str, Any]) -> str:
    return (
        base64.urlsafe_b64encode(canonical_json(claims).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )


def _decode(payload: str) -> dict[str, Any]:
    if not payload or any(character not in _BASE64URL_CHARS for character in payload):
        raise ValueError("invalid live session proof encoding")
    padding = "=" * (-len(payload) % 4)
    decoded = base64.b64decode(payload + padding, altchars=b"-_", validate=True)
    value = json.loads(decoded)
    if not isinstance(value, dict) or set(value) != _CLAIM_FIELDS:
        raise ValueError("invalid live session proof claims")
    if (
        value.get("schema_version") != "2.0"
        or value.get("adapter_type") not in {"com", "dotnet_bridge"}
        or not isinstance(value.get("process_id"), int)
        or isinstance(value.get("process_id"), bool)
        or value["process_id"] <= 0
        or not isinstance(value.get("issued_at"), int)
        or isinstance(value.get("issued_at"), bool)
        or not isinstance(value.get("expires_at"), int)
        or isinstance(value.get("expires_at"), bool)
    ):
        raise ValueError("invalid live session proof typed claims")
    for field in ("document_id", "revision", "company_profile", "nonce"):
        item = value.get(field)
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 512
            or any(character in item for character in "\r\n\0")
        ):
            raise ValueError("invalid live session proof string claim")
    steps = value.get("setup_steps")
    expected_steps = [step.value for step in required_live_setup_steps(value["adapter_type"])]
    if not expected_steps or not isinstance(steps, list) or steps != expected_steps:
        raise ValueError("invalid live session proof setup steps")
    if value["expires_at"] <= value["issued_at"]:
        raise ValueError("invalid live session proof lifetime")
    if value["expires_at"] - value["issued_at"] > int(_MAX_TTL.total_seconds()):
        raise ValueError("live session proof lifetime exceeds the maximum")
    return value


def issue_live_session_proof(
    *,
    adapter_type: str,
    process_id: int,
    document_id: str,
    revision: str,
    company_profile: str,
    setup_steps: Sequence[ManualStepId],
    secret: str,
    ttl: timedelta = _MAX_TTL,
    now: datetime | None = None,
) -> str:
    """Issue one process/document/revision-bound setup proof."""
    if not secret:
        raise ApprovalRequiredError(
            "No live session proof signing secret is configured",
            required_action="Set CAD_HARNESS_APPROVAL_SECRET outside client configuration",
        )
    expected_steps = required_live_setup_steps(adapter_type)
    if not expected_steps or tuple(setup_steps) != expected_steps:
        raise ApprovalRequiredError(
            "Every adapter-specific live setup step must be confirmed in order",
            required_action="Complete the Engineer Desktop live setup preflight",
        )
    if ttl <= timedelta(0) or ttl > _MAX_TTL:
        raise ValueError("live session proof ttl must be in (0, 15 minutes]")
    issued_at = now or datetime.now(UTC)
    claims = _claims(
        adapter_type=adapter_type,
        process_id=process_id,
        document_id=document_id,
        revision=revision,
        company_profile=company_profile,
        setup_steps=setup_steps,
        issued_at=issued_at,
        expires_at=issued_at + ttl,
        nonce=secrets.token_urlsafe(24),
    )
    payload = _encode(claims)
    digest = hmac.new(
        secret.encode("utf-8"), _SIGNING_DOMAIN + payload.encode("ascii"), sha256
    ).hexdigest()
    return f"{_TOKEN_VERSION}.{payload}.{digest}"


def verify_live_session_proof(
    token: str,
    secret: str,
    *,
    adapter_type: str,
    process_id: int,
    document_id: str,
    revision: str,
    company_profile: str,
    now: datetime | None = None,
) -> None:
    """Verify signature first, then exact live scope and expiry."""
    if not secret:
        raise ApprovalRequiredError(
            "No live session proof signing secret is configured",
            required_action="Set CAD_HARNESS_APPROVAL_SECRET outside client configuration",
        )
    try:
        if len(token) > _MAX_TOKEN_LENGTH:
            raise ValueError("live session proof is too large")
        version, payload, received_digest = token.split(".", 2)
        if version != _TOKEN_VERSION or len(received_digest) != 64:
            raise ValueError("invalid live session proof shape")
        expected_digest = hmac.new(
            secret.encode("utf-8"), _SIGNING_DOMAIN + payload.encode("ascii"), sha256
        ).hexdigest()
    except (UnicodeEncodeError, ValueError):
        payload = ""
        received_digest = ""
        expected_digest = ""
    if not hmac.compare_digest(received_digest, expected_digest):
        raise ApprovalScopeMismatchError(
            "Live session proof signature is invalid",
            required_action="Repeat the live setup preflight for this AutoCAD session",
        )
    try:
        claims = _decode(payload)
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ApprovalScopeMismatchError(
            "Live session proof claims are invalid",
            required_action="Repeat the live setup preflight for this AutoCAD session",
        ) from exc
    expected_steps = [step.value for step in required_live_setup_steps(adapter_type)]
    if not expected_steps or claims["setup_steps"] != expected_steps:
        raise ApprovalScopeMismatchError(
            "Live session proof setup does not match the selected adapter",
            required_action="Repeat the adapter-specific live setup preflight",
        )
    expected_scope = {
        "adapter_type": adapter_type,
        "process_id": process_id,
        "document_id": document_id,
        "revision": revision,
        "company_profile": company_profile,
    }
    if any(
        not hmac.compare_digest(str(claims[field]), str(expected))
        for field, expected in expected_scope.items()
    ):
        raise ApprovalScopeMismatchError(
            "Live session proof does not match the active AutoCAD session",
            required_action="Repeat setup confirmation for the current document revision",
        )
    checked_at = _timestamp(now or datetime.now(UTC))
    if checked_at > claims["expires_at"]:
        raise ApprovalExpiredError(
            "Live session proof has expired",
            required_action="Repeat the live setup preflight for this AutoCAD session",
        )
    if checked_at < claims["issued_at"]:
        raise ApprovalScopeMismatchError(
            "Live session proof is not valid yet",
            required_action="Check the workstation clock and repeat live setup",
        )


__all__ = [
    "LIVE_SESSION_PROOF_ENV",
    "issue_live_session_proof",
    "verify_live_session_proof",
]
