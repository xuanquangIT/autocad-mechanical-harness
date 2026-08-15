"""Asymmetric, trust-policy-pinned attestations for production evidence.

The verifier receives only Ed25519 public keys. Private signing keys belong to
independent evidence owners and never appear in the trust policy or verifier
environment. A caller must also pin the exact canonical trust-policy digest;
accepting an arbitrary caller-supplied policy would make self-attestation trivial.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Never

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from cad_harness.domain.canonical import canonical_json

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

_DOMAIN: Final = b"cad-harness-production-evidence-attestation/v2\0"
_POLICY_SCHEMA_VERSION: Final = "2.0"
_POLICY_KIND: Final = "production_evidence_trust_policy"
_MAX_IDENTITIES: Final = 256
_MAX_IDENTITY_BYTES: Final = 256
_MAX_CLAIMS_DEPTH: Final = 32
_MAX_CLAIMS_NODES: Final = 10_000
_MAX_CONTAINER_ITEMS: Final = 2_048
_MAX_STRING_BYTES: Final = 65_536
_MAX_CLAIMS_BYTES: Final = 262_144
_MAX_INTEGER: Final = (1 << 63) - 1
_MAX_EXPIRY: Final = timedelta(days=30)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)
_ED25519_PUBLIC_KEY_BYTES: Final = 32
_ED25519_PRIVATE_KEY_BYTES: Final = 32
_ED25519_SIGNATURE_BYTES: Final = 64


class EvidenceRole(StrEnum):
    """Closed set of independent production-evidence authorities."""

    ENGINEER_SELECTOR = "engineer_selector"
    GOLDEN_REVIEWER = "golden_reviewer"
    TAKEOFF_CALCULATOR = "takeoff_calculator"
    TAKEOFF_REVIEWER = "takeoff_reviewer"
    COMPANY_PROFILE_APPROVER = "company_profile_approver"
    MATERIAL_TABLE_APPROVER = "material_table_approver"
    PILOT_ENGINEER = "pilot_engineer"
    PILOT_REVIEWER = "pilot_reviewer"
    RASTER_ENGINEER_REVIEWER = "raster_engineer_reviewer"
    RASTER_ACCURACY_REVIEWER = "raster_accuracy_reviewer"


AttestationRole = EvidenceRole


class EvidenceAttestationErrorCode(StrEnum):
    """Stable codes whose messages and details disclose no evidence or key material."""

    INVALID_IDENTITY = "EVIDENCE_ATTESTATION_INVALID_IDENTITY"
    INVALID_POLICY = "EVIDENCE_ATTESTATION_INVALID_POLICY"
    INVALID_ROLE = "EVIDENCE_ATTESTATION_INVALID_ROLE"
    ROLE_NOT_ALLOWED = "EVIDENCE_ATTESTATION_ROLE_NOT_ALLOWED"
    INVALID_CLAIMS = "EVIDENCE_ATTESTATION_INVALID_CLAIMS"
    INVALID_ATTESTATION = "EVIDENCE_ATTESTATION_INVALID_ATTESTATION"
    IDENTITY_NOT_TRUSTED = "EVIDENCE_ATTESTATION_IDENTITY_NOT_TRUSTED"
    PUBLIC_KEY_INVALID = "EVIDENCE_ATTESTATION_PUBLIC_KEY_INVALID"
    PRIVATE_KEY_MISSING = "EVIDENCE_ATTESTATION_PRIVATE_KEY_MISSING"
    PRIVATE_KEY_INVALID = "EVIDENCE_ATTESTATION_PRIVATE_KEY_INVALID"
    PRIVATE_KEY_MISMATCH = "EVIDENCE_ATTESTATION_PRIVATE_KEY_MISMATCH"
    POLICY_DIGEST_MISSING = "EVIDENCE_ATTESTATION_POLICY_DIGEST_MISSING"
    POLICY_DIGEST_INVALID = "EVIDENCE_ATTESTATION_POLICY_DIGEST_INVALID"
    POLICY_DIGEST_MISMATCH = "EVIDENCE_ATTESTATION_POLICY_DIGEST_MISMATCH"
    CLAIMS_MISMATCH = "EVIDENCE_ATTESTATION_CLAIMS_MISMATCH"
    SIGNATURE_INVALID = "EVIDENCE_ATTESTATION_SIGNATURE_INVALID"
    NOT_YET_VALID = "EVIDENCE_ATTESTATION_NOT_YET_VALID"
    EXPIRED = "EVIDENCE_ATTESTATION_EXPIRED"


_ERROR_MESSAGES: Final[dict[EvidenceAttestationErrorCode, str]] = {
    EvidenceAttestationErrorCode.INVALID_IDENTITY: "Evidence identity is invalid",
    EvidenceAttestationErrorCode.INVALID_POLICY: "Evidence trust policy is invalid",
    EvidenceAttestationErrorCode.INVALID_ROLE: "Evidence role is invalid",
    EvidenceAttestationErrorCode.ROLE_NOT_ALLOWED: "Evidence role is not authorized",
    EvidenceAttestationErrorCode.INVALID_CLAIMS: "Evidence claims are invalid",
    EvidenceAttestationErrorCode.INVALID_ATTESTATION: "Evidence attestation is invalid",
    EvidenceAttestationErrorCode.IDENTITY_NOT_TRUSTED: "Evidence identity is not trusted",
    EvidenceAttestationErrorCode.PUBLIC_KEY_INVALID: "Evidence public key is invalid",
    EvidenceAttestationErrorCode.PRIVATE_KEY_MISSING: "Evidence private key is unavailable",
    EvidenceAttestationErrorCode.PRIVATE_KEY_INVALID: "Evidence private key is invalid",
    EvidenceAttestationErrorCode.PRIVATE_KEY_MISMATCH: (
        "Evidence private key does not match identity"
    ),
    EvidenceAttestationErrorCode.POLICY_DIGEST_MISSING: "Evidence trust policy digest is required",
    EvidenceAttestationErrorCode.POLICY_DIGEST_INVALID: "Evidence trust policy digest is invalid",
    EvidenceAttestationErrorCode.POLICY_DIGEST_MISMATCH: (
        "Evidence trust policy digest does not match"
    ),
    EvidenceAttestationErrorCode.CLAIMS_MISMATCH: "Evidence claims do not match",
    EvidenceAttestationErrorCode.SIGNATURE_INVALID: "Evidence signature is invalid",
    EvidenceAttestationErrorCode.NOT_YET_VALID: "Evidence attestation is not yet valid",
    EvidenceAttestationErrorCode.EXPIRED: "Evidence attestation has expired",
}


class EvidenceAttestationError(ValueError):
    """Fail-closed attestation error with stable, privacy-safe metadata."""

    def __init__(self, code: EvidenceAttestationErrorCode) -> None:
        self.code = code
        self.details = MappingProxyType({"reason": code.value})
        super().__init__(_ERROR_MESSAGES[code])


def _fail(code: EvidenceAttestationErrorCode) -> Never:
    raise EvidenceAttestationError(code)


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _validate_identity_id(value: object) -> None:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or _has_control_character(value)
    ):
        _fail(EvidenceAttestationErrorCode.INVALID_IDENTITY)
    assert isinstance(value, str)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(EvidenceAttestationErrorCode.INVALID_IDENTITY)
    if len(encoded) > _MAX_IDENTITY_BYTES:
        _fail(EvidenceAttestationErrorCode.INVALID_IDENTITY)


def _require_utc(value: object, *, code: EvidenceAttestationErrorCode) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        _fail(code)
    assert isinstance(value, datetime)
    return value.astimezone(UTC)


def _encode_base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_base64url(value: object, *, size: int, code: EvidenceAttestationErrorCode) -> bytes:
    if type(value) is not str or _BASE64URL.fullmatch(value) is None:
        _fail(code)
    assert isinstance(value, str)
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        _fail(code)
    if len(raw) != size or not hmac.compare_digest(_encode_base64url(raw), value):
        _fail(code)
    return raw


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustPolicyIdentity:
    """One opaque identity, its roles, and its exact raw Ed25519 public key."""

    identity_id: str
    allowed_roles: tuple[EvidenceRole, ...]
    public_key: str

    def __post_init__(self) -> None:
        _validate_identity_id(self.identity_id)
        if (
            type(self.allowed_roles) is not tuple
            or not self.allowed_roles
            or len(self.allowed_roles) > len(EvidenceRole)
            or any(type(role) is not EvidenceRole for role in self.allowed_roles)
            or len(set(self.allowed_roles)) != len(self.allowed_roles)
        ):
            _fail(EvidenceAttestationErrorCode.INVALID_ROLE)
        _decode_base64url(
            self.public_key,
            size=_ED25519_PUBLIC_KEY_BYTES,
            code=EvidenceAttestationErrorCode.PUBLIC_KEY_INVALID,
        )

    def to_canonical_dict(self) -> dict[str, str | list[str]]:
        return {
            "identity_id": self.identity_id,
            "allowed_roles": [role.value for role in self.allowed_roles],
            "public_key": self.public_key,
        }


TrustedIdentity = TrustPolicyIdentity


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceTrustPolicy:
    """Pinned local trust policy containing public verification material only."""

    identities: tuple[TrustPolicyIdentity, ...]

    def __post_init__(self) -> None:
        if (
            type(self.identities) is not tuple
            or not self.identities
            or len(self.identities) > _MAX_IDENTITIES
            or any(type(identity) is not TrustPolicyIdentity for identity in self.identities)
        ):
            _fail(EvidenceAttestationErrorCode.INVALID_POLICY)
        identity_ids = [identity.identity_id for identity in self.identities]
        public_keys = [identity.public_key for identity in self.identities]
        if len(set(identity_ids)) != len(identity_ids) or len(set(public_keys)) != len(public_keys):
            _fail(EvidenceAttestationErrorCode.INVALID_POLICY)

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "schema_version": _POLICY_SCHEMA_VERSION,
            "policy_kind": _POLICY_KIND,
            "identities": [identity.to_canonical_dict() for identity in self.identities],
        }


TrustPolicy = EvidenceTrustPolicy


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceAttestation:
    """Portable Ed25519 signature over exact claims, identity, role and validity."""

    identity_id: str
    role: EvidenceRole
    claims_sha256: str
    issued_at: datetime
    expires_at: datetime | None
    signature: str

    def __post_init__(self) -> None:
        _validate_identity_id(self.identity_id)
        if type(self.role) is not EvidenceRole:
            _fail(EvidenceAttestationErrorCode.INVALID_ROLE)
        if type(self.claims_sha256) is not str or _SHA256.fullmatch(self.claims_sha256) is None:
            _fail(EvidenceAttestationErrorCode.INVALID_ATTESTATION)
        if type(self.signature) is not str or not self.signature.startswith("ed25519:"):
            _fail(EvidenceAttestationErrorCode.INVALID_ATTESTATION)
        _decode_base64url(
            self.signature.removeprefix("ed25519:"),
            size=_ED25519_SIGNATURE_BYTES,
            code=EvidenceAttestationErrorCode.INVALID_ATTESTATION,
        )
        issued_at = _require_utc(
            self.issued_at, code=EvidenceAttestationErrorCode.INVALID_ATTESTATION
        )
        object.__setattr__(self, "issued_at", issued_at)
        if self.expires_at is not None:
            expires_at = _require_utc(
                self.expires_at, code=EvidenceAttestationErrorCode.INVALID_ATTESTATION
            )
            if expires_at <= issued_at or expires_at - issued_at > _MAX_EXPIRY:
                _fail(EvidenceAttestationErrorCode.INVALID_ATTESTATION)
            object.__setattr__(self, "expires_at", expires_at)

    def to_canonical_dict(self) -> dict[str, str | None]:
        """Return the exact signed envelope without signature material."""
        return {
            "claims_sha256": self.claims_sha256,
            "expires_at": _timestamp(self.expires_at),
            "identity_id": self.identity_id,
            "issued_at": _timestamp(self.issued_at),
            "role": self.role.value,
        }

    def to_external_dict(self) -> dict[str, str | None]:
        return {**self.to_canonical_dict(), "signature": self.signature}


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object, *, optional: bool) -> datetime | None:
    if optional and value is None:
        return None
    if type(value) is not str or not value.endswith("Z"):
        _fail(EvidenceAttestationErrorCode.INVALID_ATTESTATION)
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail(EvidenceAttestationErrorCode.INVALID_ATTESTATION)
    parsed = _require_utc(parsed, code=EvidenceAttestationErrorCode.INVALID_ATTESTATION)
    if _timestamp(parsed) != value:
        _fail(EvidenceAttestationErrorCode.INVALID_ATTESTATION)
    return parsed


def trust_policy_from_mapping(value: object) -> EvidenceTrustPolicy:
    """Parse the strict v2 external trust-policy representation."""
    if type(value) is not dict:
        _fail(EvidenceAttestationErrorCode.INVALID_POLICY)
    assert isinstance(value, dict)
    if set(value) != {"schema_version", "policy_kind", "identities"}:
        _fail(EvidenceAttestationErrorCode.INVALID_POLICY)
    if (
        value.get("schema_version") != _POLICY_SCHEMA_VERSION
        or value.get("policy_kind") != _POLICY_KIND
        or type(value.get("identities")) is not list
    ):
        _fail(EvidenceAttestationErrorCode.INVALID_POLICY)
    raw_identities = value["identities"]
    assert isinstance(raw_identities, list)
    identities: list[TrustPolicyIdentity] = []
    for raw_identity in raw_identities:
        if type(raw_identity) is not dict:
            _fail(EvidenceAttestationErrorCode.INVALID_POLICY)
        assert isinstance(raw_identity, dict)
        if set(raw_identity) != {"identity_id", "allowed_roles", "public_key"}:
            _fail(EvidenceAttestationErrorCode.INVALID_POLICY)
        raw_roles = raw_identity.get("allowed_roles")
        if type(raw_roles) is not list:
            _fail(EvidenceAttestationErrorCode.INVALID_ROLE)
        assert isinstance(raw_roles, list)
        try:
            roles = tuple(EvidenceRole(role) for role in raw_roles)
        except (TypeError, ValueError):
            _fail(EvidenceAttestationErrorCode.INVALID_ROLE)
        try:
            identities.append(
                TrustPolicyIdentity(
                    identity_id=raw_identity["identity_id"],
                    allowed_roles=roles,
                    public_key=raw_identity["public_key"],
                )
            )
        except KeyError:
            _fail(EvidenceAttestationErrorCode.INVALID_POLICY)
    return EvidenceTrustPolicy(identities=tuple(identities))


def trust_policy_sha256(policy: EvidenceTrustPolicy) -> str:
    """Return the canonical digest that deployment configuration must pin."""
    if type(policy) is not EvidenceTrustPolicy:
        _fail(EvidenceAttestationErrorCode.INVALID_POLICY)
    digest = hashlib.sha256(canonical_json(policy.to_canonical_dict()).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def verify_trust_policy_digest(policy: EvidenceTrustPolicy, expected_policy_sha256: object) -> None:
    """Reject missing, malformed, or substituted trust policies before identity lookup."""
    if expected_policy_sha256 is None or expected_policy_sha256 == "":
        _fail(EvidenceAttestationErrorCode.POLICY_DIGEST_MISSING)
    if type(expected_policy_sha256) is not str or _SHA256.fullmatch(expected_policy_sha256) is None:
        _fail(EvidenceAttestationErrorCode.POLICY_DIGEST_INVALID)
    actual = trust_policy_sha256(policy)
    if not hmac.compare_digest(actual, expected_policy_sha256):
        _fail(EvidenceAttestationErrorCode.POLICY_DIGEST_MISMATCH)


def evidence_attestation_from_mapping(value: object) -> EvidenceAttestation:
    """Parse the strict portable attestation representation."""
    if type(value) is not dict:
        _fail(EvidenceAttestationErrorCode.INVALID_ATTESTATION)
    assert isinstance(value, dict)
    expected = {
        "claims_sha256",
        "expires_at",
        "identity_id",
        "issued_at",
        "role",
        "signature",
    }
    if set(value) != expected:
        _fail(EvidenceAttestationErrorCode.INVALID_ATTESTATION)
    try:
        role = EvidenceRole(value["role"])
    except (TypeError, ValueError):
        _fail(EvidenceAttestationErrorCode.INVALID_ROLE)
    issued_at = _parse_timestamp(value["issued_at"], optional=False)
    expires_at = _parse_timestamp(value["expires_at"], optional=True)
    assert issued_at is not None
    try:
        return EvidenceAttestation(
            identity_id=value["identity_id"],
            role=role,
            claims_sha256=value["claims_sha256"],
            issued_at=issued_at,
            expires_at=expires_at,
            signature=value["signature"],
        )
    except KeyError:
        _fail(EvidenceAttestationErrorCode.INVALID_ATTESTATION)


def _validate_claims(claims: object) -> JsonValue:
    nodes = 0
    active_containers: set[int] = set()

    def visit(value: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_CLAIMS_NODES or depth > _MAX_CLAIMS_DEPTH:
            _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
        if value is None or type(value) is bool:
            return
        if type(value) is int:
            if not -_MAX_INTEGER <= value <= _MAX_INTEGER:
                _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
            return
        if type(value) is float:
            if not math.isfinite(value):
                _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
            return
        if type(value) is str:
            try:
                size = len(value.encode("utf-8"))
            except UnicodeEncodeError:
                _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
            if size > _MAX_STRING_BYTES:
                _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
            return
        if type(value) not in {list, dict}:
            _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
        assert isinstance(value, list | dict)
        if len(value) > _MAX_CONTAINER_ITEMS:
            _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
        marker = id(value)
        if marker in active_containers:
            _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
        active_containers.add(marker)
        try:
            if isinstance(value, list):
                for item in value:
                    visit(item, depth + 1)
            else:
                for key, item in value.items():
                    if type(key) is not str:
                        _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
                    try:
                        key_size = len(key.encode("utf-8"))
                    except UnicodeEncodeError:
                        _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
                    if key_size > _MAX_STRING_BYTES:
                        _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
                    visit(item, depth + 1)
        finally:
            active_containers.remove(marker)

    visit(claims, 0)
    try:
        serialized = canonical_json(claims)
        serialized_size = len(serialized.encode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError):
        _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
    if serialized_size > _MAX_CLAIMS_BYTES:
        _fail(EvidenceAttestationErrorCode.INVALID_CLAIMS)
    return claims  # type: ignore[return-value]


def _claims_sha256(claims: object) -> str:
    validated = _validate_claims(claims)
    digest = hashlib.sha256(canonical_json(validated).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _private_key(value: object) -> Ed25519PrivateKey:
    if value is None or value == "":
        _fail(EvidenceAttestationErrorCode.PRIVATE_KEY_MISSING)
    raw = _decode_base64url(
        value,
        size=_ED25519_PRIVATE_KEY_BYTES,
        code=EvidenceAttestationErrorCode.PRIVATE_KEY_INVALID,
    )
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError:
        _fail(EvidenceAttestationErrorCode.PRIVATE_KEY_INVALID)


def _signature_payload(
    *,
    identity_id: str,
    role: EvidenceRole,
    claims_sha256: str,
    issued_at: datetime,
    expires_at: datetime | None,
) -> bytes:
    payload = canonical_json(
        {
            "claims_sha256": claims_sha256,
            "expires_at": _timestamp(expires_at),
            "identity_id": identity_id,
            "issued_at": _timestamp(issued_at),
            "role": role.value,
        }
    ).encode("utf-8")
    return _DOMAIN + payload


def issue_attestation(
    claims: JsonValue,
    identity: TrustPolicyIdentity,
    role: EvidenceRole,
    private_key: str,
    *,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> EvidenceAttestation:
    """Issue with a private key whose public half exactly matches the trusted identity."""
    if type(identity) is not TrustPolicyIdentity:
        _fail(EvidenceAttestationErrorCode.INVALID_IDENTITY)
    if type(role) is not EvidenceRole:
        _fail(EvidenceAttestationErrorCode.INVALID_ROLE)
    if role not in identity.allowed_roles:
        _fail(EvidenceAttestationErrorCode.ROLE_NOT_ALLOWED)
    key = _private_key(private_key)
    derived_public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trusted_public = _decode_base64url(
        identity.public_key,
        size=_ED25519_PUBLIC_KEY_BYTES,
        code=EvidenceAttestationErrorCode.PUBLIC_KEY_INVALID,
    )
    if not hmac.compare_digest(derived_public, trusted_public):
        _fail(EvidenceAttestationErrorCode.PRIVATE_KEY_MISMATCH)
    issued = _require_utc(
        issued_at or datetime.now(UTC), code=EvidenceAttestationErrorCode.INVALID_ATTESTATION
    )
    expiry = (
        None
        if expires_at is None
        else _require_utc(expires_at, code=EvidenceAttestationErrorCode.INVALID_ATTESTATION)
    )
    claims_digest = _claims_sha256(claims)
    payload = _signature_payload(
        identity_id=identity.identity_id,
        role=role,
        claims_sha256=claims_digest,
        issued_at=issued,
        expires_at=expiry,
    )
    signature = f"ed25519:{_encode_base64url(key.sign(payload))}"
    return EvidenceAttestation(
        identity_id=identity.identity_id,
        role=role,
        claims_sha256=claims_digest,
        issued_at=issued,
        expires_at=expiry,
        signature=signature,
    )


def verify_attestation(
    policy: EvidenceTrustPolicy,
    attestation: EvidenceAttestation,
    exact_claims: JsonValue,
    *,
    expected_policy_sha256: str,
    now: datetime | None = None,
) -> TrustPolicyIdentity:
    """Verify a pinned policy, exact claims, Ed25519 signature and validity window."""
    if type(policy) is not EvidenceTrustPolicy:
        _fail(EvidenceAttestationErrorCode.INVALID_POLICY)
    if type(attestation) is not EvidenceAttestation:
        _fail(EvidenceAttestationErrorCode.INVALID_ATTESTATION)
    verify_trust_policy_digest(policy, expected_policy_sha256)
    identity = next(
        (
            candidate
            for candidate in policy.identities
            if hmac.compare_digest(
                candidate.identity_id.encode("utf-8"), attestation.identity_id.encode("utf-8")
            )
        ),
        None,
    )
    if identity is None:
        _fail(EvidenceAttestationErrorCode.IDENTITY_NOT_TRUSTED)
    if attestation.role not in identity.allowed_roles:
        _fail(EvidenceAttestationErrorCode.ROLE_NOT_ALLOWED)
    submitted_digest = _claims_sha256(exact_claims)
    if not hmac.compare_digest(attestation.claims_sha256, submitted_digest):
        _fail(EvidenceAttestationErrorCode.CLAIMS_MISMATCH)
    signature = _decode_base64url(
        attestation.signature.removeprefix("ed25519:"),
        size=_ED25519_SIGNATURE_BYTES,
        code=EvidenceAttestationErrorCode.INVALID_ATTESTATION,
    )
    public_key = Ed25519PublicKey.from_public_bytes(
        _decode_base64url(
            identity.public_key,
            size=_ED25519_PUBLIC_KEY_BYTES,
            code=EvidenceAttestationErrorCode.PUBLIC_KEY_INVALID,
        )
    )
    try:
        public_key.verify(
            signature,
            _signature_payload(
                identity_id=attestation.identity_id,
                role=attestation.role,
                claims_sha256=attestation.claims_sha256,
                issued_at=attestation.issued_at,
                expires_at=attestation.expires_at,
            ),
        )
    except InvalidSignature:
        _fail(EvidenceAttestationErrorCode.SIGNATURE_INVALID)
    current = _require_utc(
        now or datetime.now(UTC), code=EvidenceAttestationErrorCode.INVALID_ATTESTATION
    )
    if current < attestation.issued_at:
        _fail(EvidenceAttestationErrorCode.NOT_YET_VALID)
    if attestation.expires_at is not None and current >= attestation.expires_at:
        _fail(EvidenceAttestationErrorCode.EXPIRED)
    return identity


__all__ = [
    "AttestationRole",
    "EvidenceAttestation",
    "EvidenceAttestationError",
    "EvidenceAttestationErrorCode",
    "EvidenceRole",
    "EvidenceTrustPolicy",
    "JsonValue",
    "TrustPolicy",
    "TrustPolicyIdentity",
    "TrustedIdentity",
    "evidence_attestation_from_mapping",
    "issue_attestation",
    "trust_policy_from_mapping",
    "trust_policy_sha256",
    "verify_attestation",
    "verify_trust_policy_digest",
]
