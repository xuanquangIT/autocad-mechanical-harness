"""Independent Ed25519 receipts for live CAD execution evidence.

The issuer receives typed claims derived from a completed adapter call.  The
verifier needs only the public key and an independently pinned key digest.
This surface is deliberately separate from human evidence attestations.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from datetime import UTC, datetime
from typing import Final, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import Field, field_validator

from cad_harness.domain.canonical import canonical_json
from cad_harness.domain.models.base import ContractModel

_DOMAIN: Final = b"cad-harness/raster-execution-receipt/v1\x00"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$", re.ASCII)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", re.ASCII)
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$", re.ASCII)


class ExecutionReceiptError(ValueError):
    """Privacy-safe failure at the execution-signing boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _decode_key(value: str, *, size: int, code: str) -> bytes:
    if not isinstance(value, str) or _BASE64URL.fullmatch(value) is None or "=" in value:
        raise ExecutionReceiptError(code)
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ExecutionReceiptError(code) from exc
    canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(raw) != size or not hmac.compare_digest(canonical, value):
        raise ExecutionReceiptError(code)
    return raw


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class ExecutionReceiptClaims(ContractModel):
    adapter_type: Literal["com", "dotnet_bridge"]
    process_id: int = Field(gt=0)
    document_id: str
    pre_revision: str
    post_revision: str
    plan_hash: str
    job_id: str
    validation_report_sha256: str
    result_sha256: str

    @field_validator("document_id", "job_id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("execution identifier is invalid")
        return value

    @field_validator(
        "pre_revision",
        "post_revision",
        "plan_hash",
        "validation_report_sha256",
        "result_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("execution digest is invalid")
        return value


class ExecutionReceipt(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    signer_id: str
    issued_at: str
    claims: ExecutionReceiptClaims
    signature: str

    @field_validator("signer_id")
    @classmethod
    def _signer(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("execution signer id is invalid")
        return value

    def to_external_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def execution_public_key(private_key: str) -> str:
    """Return the raw public key for an issuer-side private key."""

    private = Ed25519PrivateKey.from_private_bytes(
        _decode_key(private_key, size=32, code="EXECUTION_PRIVATE_KEY_INVALID")
    )
    return _encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )


def execution_public_key_sha256(public_key: str) -> str:
    raw = _decode_key(public_key, size=32, code="EXECUTION_PUBLIC_KEY_INVALID")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _unsigned(
    *, signer_id: str, issued_at: str, claims: ExecutionReceiptClaims
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "signer_id": signer_id,
        "issued_at": issued_at,
        "claims": claims.model_dump(mode="json"),
    }


def issue_execution_receipt(
    claims: ExecutionReceiptClaims,
    *,
    signer_id: str,
    private_key: str,
    issued_at: datetime | None = None,
) -> ExecutionReceipt:
    """Sign claims derived from one completed live CAD execution."""

    if _SAFE_ID.fullmatch(signer_id) is None:
        raise ExecutionReceiptError("EXECUTION_SIGNER_INVALID")
    timestamp = issued_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ExecutionReceiptError("EXECUTION_TIMESTAMP_INVALID")
    timestamp_text = timestamp.astimezone(UTC).isoformat()
    private = Ed25519PrivateKey.from_private_bytes(
        _decode_key(private_key, size=32, code="EXECUTION_PRIVATE_KEY_INVALID")
    )
    unsigned = _unsigned(signer_id=signer_id, issued_at=timestamp_text, claims=claims)
    signature = private.sign(_DOMAIN + canonical_json(unsigned).encode("utf-8"))
    return ExecutionReceipt(
        signer_id=signer_id,
        issued_at=timestamp_text,
        claims=claims,
        signature=f"ed25519:{_encode(signature)}",
    )


def verify_execution_receipt(
    receipt: ExecutionReceipt,
    expected_claims: ExecutionReceiptClaims,
    *,
    public_key: str,
    expected_public_key_sha256: str,
    now: datetime | None = None,
) -> None:
    """Verify exact claims, pinned key, chronology, and signature."""

    actual_key_digest = execution_public_key_sha256(public_key)
    if _SHA256.fullmatch(expected_public_key_sha256) is None or not hmac.compare_digest(
        actual_key_digest, expected_public_key_sha256
    ):
        raise ExecutionReceiptError("EXECUTION_PUBLIC_KEY_MISMATCH")
    if receipt.claims != expected_claims:
        raise ExecutionReceiptError("EXECUTION_CLAIMS_MISMATCH")
    try:
        issued = datetime.fromisoformat(receipt.issued_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionReceiptError("EXECUTION_TIMESTAMP_INVALID") from exc
    if (
        issued.tzinfo is None
        or issued.utcoffset() is None
        or issued.astimezone(UTC) > (now or datetime.now(UTC)).astimezone(UTC)
    ):
        raise ExecutionReceiptError("EXECUTION_TIMESTAMP_INVALID")
    if not receipt.signature.startswith("ed25519:"):
        raise ExecutionReceiptError("EXECUTION_SIGNATURE_INVALID")
    signature = _decode_key(
        receipt.signature.removeprefix("ed25519:"),
        size=64,
        code="EXECUTION_SIGNATURE_INVALID",
    )
    unsigned = _unsigned(
        signer_id=receipt.signer_id,
        issued_at=receipt.issued_at,
        claims=receipt.claims,
    )
    try:
        Ed25519PublicKey.from_public_bytes(
            _decode_key(public_key, size=32, code="EXECUTION_PUBLIC_KEY_INVALID")
        ).verify(signature, _DOMAIN + canonical_json(unsigned).encode("utf-8"))
    except InvalidSignature as exc:
        raise ExecutionReceiptError("EXECUTION_SIGNATURE_INVALID") from exc


__all__ = [
    "ExecutionReceipt",
    "ExecutionReceiptClaims",
    "ExecutionReceiptError",
    "execution_public_key",
    "execution_public_key_sha256",
    "issue_execution_receipt",
    "verify_execution_receipt",
]
