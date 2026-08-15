from __future__ import annotations

import base64
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cad_harness.security.evidence_attestation import (
    EvidenceAttestationError,
    EvidenceAttestationErrorCode,
    EvidenceRole,
    EvidenceTrustPolicy,
    TrustPolicyIdentity,
    evidence_attestation_from_mapping,
    issue_attestation,
    trust_policy_from_mapping,
    trust_policy_sha256,
    verify_attestation,
    verify_trust_policy_digest,
)

_ISSUED = datetime(2026, 8, 15, 9, 30, tzinfo=UTC)
_CLAIMS = {
    "artifact_sha256": "sha256:" + "a" * 64,
    "case_id": "case-opaque-001",
    "result": {"accepted": True, "measurements": [1.25, 7, None]},
}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _keypair(marker: int = 1) -> tuple[str, str]:
    key = Ed25519PrivateKey.from_private_bytes(bytes([marker]) * 32)
    private = _b64(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    public = _b64(
        key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    return private, public


_PRIVATE, _PUBLIC = _keypair()


def _identity(
    *,
    identity_id: str = "identity-opaque-01",
    roles: tuple[EvidenceRole, ...] = tuple(EvidenceRole),
    public_key: str = _PUBLIC,
) -> TrustPolicyIdentity:
    return TrustPolicyIdentity(
        identity_id=identity_id,
        allowed_roles=roles,
        public_key=public_key,
    )


def _policy(identity: TrustPolicyIdentity | None = None) -> EvidenceTrustPolicy:
    return EvidenceTrustPolicy(identities=(identity or _identity(),))


def _pin(policy: EvidenceTrustPolicy) -> str:
    return trust_policy_sha256(policy)


def _verify(
    policy: EvidenceTrustPolicy,
    attestation: object,
    claims: object = _CLAIMS,
    *,
    pin: str | None = None,
    now: datetime = _ISSUED,
) -> TrustPolicyIdentity:
    return verify_attestation(
        policy,
        attestation,  # type: ignore[arg-type]
        claims,  # type: ignore[arg-type]
        expected_policy_sha256=pin if pin is not None else _pin(policy),
        now=now,
    )


def _assert_code(expected: EvidenceAttestationErrorCode, error: pytest.ExceptionInfo) -> None:
    assert error.value.code is expected
    assert error.value.details == {"reason": expected.value}


@pytest.mark.parametrize("role", list(EvidenceRole))
def test_each_closed_role_uses_ed25519_and_verifies_without_private_key_or_env(
    role: EvidenceRole,
) -> None:
    identity = _identity(roles=(role,))
    policy = _policy(identity)
    attestation = issue_attestation(
        _CLAIMS,
        identity,
        role,
        _PRIVATE,
        issued_at=_ISSUED,
        expires_at=_ISSUED + timedelta(days=30),
    )

    assert _verify(policy, attestation) is identity
    assert attestation.role is role
    assert attestation.claims_sha256.startswith("sha256:")
    assert attestation.signature.startswith("ed25519:")
    assert _PRIVATE not in repr(attestation)
    assert _PRIVATE not in str(policy.to_canonical_dict())


def test_canonical_claim_and_policy_digests_are_deterministic() -> None:
    identity = _identity(roles=(EvidenceRole.GOLDEN_REVIEWER,))
    first = issue_attestation(
        {"b": [2, 3], "a": 1},
        identity,
        EvidenceRole.GOLDEN_REVIEWER,
        _PRIVATE,
        issued_at=_ISSUED,
    )
    second = issue_attestation(
        {"a": 1, "b": [2, 3]},
        identity,
        EvidenceRole.GOLDEN_REVIEWER,
        _PRIVATE,
        issued_at=_ISSUED,
    )
    parsed = trust_policy_from_mapping(_policy(identity).to_canonical_dict())

    assert first == second
    assert trust_policy_sha256(parsed) == trust_policy_sha256(_policy(identity))


def test_opaque_unicode_identity_is_compared_as_utf8_bytes() -> None:
    identity = _identity(identity_id="reviewer-độc-lập-01")
    policy = _policy(identity)
    attestation = issue_attestation(
        _CLAIMS,
        identity,
        EvidenceRole.ENGINEER_SELECTOR,
        _PRIVATE,
        issued_at=_ISSUED,
    )

    assert _verify(policy, attestation) is identity


def test_missing_invalid_and_mismatched_private_keys_fail_closed() -> None:
    identity = _identity(roles=(EvidenceRole.GOLDEN_REVIEWER,))
    for value, expected in [
        ("", EvidenceAttestationErrorCode.PRIVATE_KEY_MISSING),
        ("not-base64url!", EvidenceAttestationErrorCode.PRIVATE_KEY_INVALID),
        (_keypair(2)[0], EvidenceAttestationErrorCode.PRIVATE_KEY_MISMATCH),
    ]:
        with pytest.raises(EvidenceAttestationError) as error:
            issue_attestation(
                _CLAIMS,
                identity,
                EvidenceRole.GOLDEN_REVIEWER,
                value,
                issued_at=_ISSUED,
            )
        _assert_code(expected, error)


def test_identity_role_scope_and_signature_tampering_fail_closed() -> None:
    identity = _identity(roles=(EvidenceRole.ENGINEER_SELECTOR,))
    policy = _policy(identity)
    attestation = issue_attestation(
        _CLAIMS,
        identity,
        EvidenceRole.ENGINEER_SELECTOR,
        _PRIVATE,
        issued_at=_ISSUED,
    )

    untrusted = replace(attestation, identity_id="identity-opaque-02")
    with pytest.raises(EvidenceAttestationError) as identity_error:
        _verify(policy, untrusted)
    _assert_code(EvidenceAttestationErrorCode.IDENTITY_NOT_TRUSTED, identity_error)

    unauthorized = replace(attestation, role=EvidenceRole.GOLDEN_REVIEWER)
    with pytest.raises(EvidenceAttestationError) as role_error:
        _verify(policy, unauthorized)
    _assert_code(EvidenceAttestationErrorCode.ROLE_NOT_ALLOWED, role_error)

    with pytest.raises(EvidenceAttestationError) as scope_error:
        _verify(policy, attestation, {**_CLAIMS, "case_id": "case-opaque-002"})
    _assert_code(EvidenceAttestationErrorCode.CLAIMS_MISMATCH, scope_error)

    encoded = attestation.signature.removeprefix("ed25519:")
    replacement = ("A" if encoded[0] != "A" else "B") + encoded[1:]
    tampered = replace(attestation, signature=f"ed25519:{replacement}")
    with pytest.raises(EvidenceAttestationError) as signature_error:
        _verify(policy, tampered)
    _assert_code(EvidenceAttestationErrorCode.SIGNATURE_INVALID, signature_error)


def test_policy_rejects_duplicate_identity_role_and_public_key() -> None:
    identity = _identity(roles=(EvidenceRole.GOLDEN_REVIEWER,))
    with pytest.raises(EvidenceAttestationError) as duplicate_identity:
        EvidenceTrustPolicy(identities=(identity, identity))
    _assert_code(EvidenceAttestationErrorCode.INVALID_POLICY, duplicate_identity)

    with pytest.raises(EvidenceAttestationError) as duplicate_role:
        _identity(roles=(EvidenceRole.GOLDEN_REVIEWER, EvidenceRole.GOLDEN_REVIEWER))
    _assert_code(EvidenceAttestationErrorCode.INVALID_ROLE, duplicate_role)

    same_key_alias = _identity(identity_id="identity-opaque-02")
    with pytest.raises(EvidenceAttestationError) as duplicate_key:
        EvidenceTrustPolicy(identities=(identity, same_key_alias))
    _assert_code(EvidenceAttestationErrorCode.INVALID_POLICY, duplicate_key)


@pytest.mark.parametrize(
    ("identity_id", "public_key", "expected"),
    [
        ("identity\n01", _PUBLIC, EvidenceAttestationErrorCode.INVALID_IDENTITY),
        (" identity-01", _PUBLIC, EvidenceAttestationErrorCode.INVALID_IDENTITY),
        ("identity-01", "not-base64url!", EvidenceAttestationErrorCode.PUBLIC_KEY_INVALID),
        ("identity-01", _b64(b"x" * 31), EvidenceAttestationErrorCode.PUBLIC_KEY_INVALID),
    ],
)
def test_identity_and_public_key_validation_is_strict(
    identity_id: str,
    public_key: str,
    expected: EvidenceAttestationErrorCode,
) -> None:
    with pytest.raises(EvidenceAttestationError) as error:
        _identity(identity_id=identity_id, public_key=public_key)
    _assert_code(expected, error)


def test_policy_pin_is_mandatory_and_rejects_arbitrary_self_generated_policy() -> None:
    identity = _identity(roles=(EvidenceRole.GOLDEN_REVIEWER,))
    policy = _policy(identity)
    attestation = issue_attestation(
        _CLAIMS,
        identity,
        EvidenceRole.GOLDEN_REVIEWER,
        _PRIVATE,
        issued_at=_ISSUED,
    )
    with pytest.raises(EvidenceAttestationError) as missing:
        verify_attestation(
            policy,
            attestation,
            _CLAIMS,
            expected_policy_sha256=None,  # type: ignore[arg-type]
            now=_ISSUED,
        )
    _assert_code(EvidenceAttestationErrorCode.POLICY_DIGEST_MISSING, missing)
    with pytest.raises(EvidenceAttestationError) as malformed:
        _verify(policy, attestation, pin="0" * 64)
    _assert_code(EvidenceAttestationErrorCode.POLICY_DIGEST_INVALID, malformed)

    attacker_private, attacker_public = _keypair(9)
    attacker = _identity(
        identity_id="attacker-controlled",
        roles=(EvidenceRole.GOLDEN_REVIEWER,),
        public_key=attacker_public,
    )
    attacker_policy = _policy(attacker)
    attacker_attestation = issue_attestation(
        _CLAIMS,
        attacker,
        EvidenceRole.GOLDEN_REVIEWER,
        attacker_private,
        issued_at=_ISSUED,
    )
    with pytest.raises(EvidenceAttestationError) as substituted:
        _verify(attacker_policy, attacker_attestation, pin=_pin(policy))
    _assert_code(EvidenceAttestationErrorCode.POLICY_DIGEST_MISMATCH, substituted)


@pytest.mark.parametrize(
    "claims",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": b"not-json"},
        {1: "non-string-key"},
        {"value": (1, 2)},
        {"value": 1 << 64},
        {"value": "x" * 65_537},
        {"value": list(range(2_049))},
    ],
)
def test_malformed_or_unbounded_claims_are_rejected(claims: object) -> None:
    with pytest.raises(EvidenceAttestationError) as error:
        issue_attestation(
            claims,  # type: ignore[arg-type]
            _identity(),
            EvidenceRole.ENGINEER_SELECTOR,
            _PRIVATE,
            issued_at=_ISSUED,
        )
    _assert_code(EvidenceAttestationErrorCode.INVALID_CLAIMS, error)


def test_claim_depth_limit_and_cycles_are_rejected() -> None:
    too_deep: dict[str, object] = {}
    cursor = too_deep
    for _ in range(33):
        nested: dict[str, object] = {}
        cursor["next"] = nested
        cursor = nested
    with pytest.raises(EvidenceAttestationError) as depth_error:
        issue_attestation(
            too_deep,  # type: ignore[arg-type]
            _identity(),
            EvidenceRole.ENGINEER_SELECTOR,
            _PRIVATE,
            issued_at=_ISSUED,
        )
    _assert_code(EvidenceAttestationErrorCode.INVALID_CLAIMS, depth_error)

    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(EvidenceAttestationError) as cycle_error:
        issue_attestation(
            cyclic,  # type: ignore[arg-type]
            _identity(),
            EvidenceRole.ENGINEER_SELECTOR,
            _PRIVATE,
            issued_at=_ISSUED,
        )
    _assert_code(EvidenceAttestationErrorCode.INVALID_CLAIMS, cycle_error)


def test_expiry_is_exclusive_and_time_window_is_bounded() -> None:
    identity = _identity(roles=(EvidenceRole.TAKEOFF_REVIEWER,))
    policy = _policy(identity)
    expires_at = _ISSUED + timedelta(hours=1)
    attestation = issue_attestation(
        _CLAIMS,
        identity,
        EvidenceRole.TAKEOFF_REVIEWER,
        _PRIVATE,
        issued_at=_ISSUED,
        expires_at=expires_at,
    )
    with pytest.raises(EvidenceAttestationError) as expired:
        _verify(policy, attestation, now=expires_at)
    _assert_code(EvidenceAttestationErrorCode.EXPIRED, expired)

    for invalid_expiry in [
        _ISSUED,
        _ISSUED + timedelta(days=30, microseconds=1),
    ]:
        with pytest.raises(EvidenceAttestationError) as invalid:
            issue_attestation(
                _CLAIMS,
                identity,
                EvidenceRole.TAKEOFF_REVIEWER,
                _PRIVATE,
                issued_at=_ISSUED,
                expires_at=invalid_expiry,
            )
        _assert_code(EvidenceAttestationErrorCode.INVALID_ATTESTATION, invalid)


def test_time_requires_utc_and_attestation_is_not_valid_before_issuance() -> None:
    identity = _identity(roles=(EvidenceRole.TAKEOFF_REVIEWER,))
    non_utc = datetime(2026, 8, 15, 10, 30, tzinfo=timezone(timedelta(hours=1)))
    with pytest.raises(EvidenceAttestationError) as timezone_error:
        issue_attestation(
            _CLAIMS,
            identity,
            EvidenceRole.TAKEOFF_REVIEWER,
            _PRIVATE,
            issued_at=non_utc,
        )
    _assert_code(EvidenceAttestationErrorCode.INVALID_ATTESTATION, timezone_error)

    attestation = issue_attestation(
        _CLAIMS,
        identity,
        EvidenceRole.TAKEOFF_REVIEWER,
        _PRIVATE,
        issued_at=_ISSUED,
    )
    with pytest.raises(EvidenceAttestationError) as early:
        _verify(_policy(identity), attestation, now=_ISSUED - timedelta(microseconds=1))
    _assert_code(EvidenceAttestationErrorCode.NOT_YET_VALID, early)


def test_models_are_frozen_strict_and_reject_unknown_fields() -> None:
    identity = _identity()
    with pytest.raises(FrozenInstanceError):
        identity.identity_id = "replacement"  # type: ignore[misc]
    with pytest.raises(TypeError):
        TrustPolicyIdentity(  # type: ignore[call-arg]
            identity_id="identity-opaque-01",
            allowed_roles=(EvidenceRole.ENGINEER_SELECTOR,),
            public_key=_PUBLIC,
            unknown=True,
        )
    with pytest.raises(EvidenceAttestationError) as coerced_role:
        TrustPolicyIdentity(  # type: ignore[arg-type]
            identity_id="identity-opaque-01",
            allowed_roles=("engineer_selector",),
            public_key=_PUBLIC,
        )
    _assert_code(EvidenceAttestationErrorCode.INVALID_ROLE, coerced_role)


def test_errors_never_leak_private_keys_claims_or_identity_values() -> None:
    identity = _identity(identity_id="sensitive-identity-reference")
    policy = _policy(identity)
    attestation = issue_attestation(
        {"sensitive": "customer-artifact-reference"},
        identity,
        EvidenceRole.ENGINEER_SELECTOR,
        _PRIVATE,
        issued_at=_ISSUED,
    )
    with pytest.raises(EvidenceAttestationError) as caught:
        verify_attestation(
            policy,
            attestation,
            {"sensitive": "different-customer-reference"},
            expected_policy_sha256=_pin(policy),
            now=_ISSUED,
        )

    rendered = f"{caught.value!s} {caught.value!r} {caught.value.details!r}"
    assert _PRIVATE not in rendered
    assert identity.identity_id not in rendered
    assert "customer-artifact-reference" not in rendered
    assert "different-customer-reference" not in rendered


def test_strict_external_policy_and_attestation_round_trip() -> None:
    identity = _identity(roles=(EvidenceRole.RASTER_ENGINEER_REVIEWER,))
    policy = trust_policy_from_mapping(
        {
            "schema_version": "2.0",
            "policy_kind": "production_evidence_trust_policy",
            "identities": [
                {
                    "identity_id": identity.identity_id,
                    "allowed_roles": [EvidenceRole.RASTER_ENGINEER_REVIEWER.value],
                    "public_key": identity.public_key,
                }
            ],
        }
    )
    issued = issue_attestation(
        _CLAIMS,
        identity,
        EvidenceRole.RASTER_ENGINEER_REVIEWER,
        _PRIVATE,
        issued_at=_ISSUED,
        expires_at=_ISSUED + timedelta(days=1),
    )
    parsed = evidence_attestation_from_mapping(issued.to_external_dict())

    assert _verify(policy, parsed) == identity
    verify_trust_policy_digest(policy, trust_policy_sha256(policy))


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": True},
        {"schema_version": "1.0"},
        {"policy_kind": "different"},
        {"identities": "not-a-list"},
        {
            "identities": [
                {
                    "identity_id": "identity-opaque-01",
                    "allowed_roles": ["not-a-role"],
                    "public_key": _PUBLIC,
                }
            ]
        },
    ],
)
def test_external_policy_parser_rejects_unknown_or_invalid_values(
    mutation: dict[str, object],
) -> None:
    value: dict[str, object] = {
        "schema_version": "2.0",
        "policy_kind": "production_evidence_trust_policy",
        "identities": [
            {
                "identity_id": "identity-opaque-01",
                "allowed_roles": [EvidenceRole.GOLDEN_REVIEWER.value],
                "public_key": _PUBLIC,
            }
        ],
    }
    value.update(mutation)
    with pytest.raises(EvidenceAttestationError):
        trust_policy_from_mapping(value)


def test_external_attestation_parser_rejects_noncanonical_time_and_unknown_fields() -> None:
    issued = issue_attestation(
        _CLAIMS,
        _identity(),
        EvidenceRole.ENGINEER_SELECTOR,
        _PRIVATE,
        issued_at=_ISSUED,
    )
    noncanonical = issued.to_external_dict()
    noncanonical["issued_at"] = "2026-08-15T09:30:00Z"
    with pytest.raises(EvidenceAttestationError) as time_error:
        evidence_attestation_from_mapping(noncanonical)
    _assert_code(EvidenceAttestationErrorCode.INVALID_ATTESTATION, time_error)

    unknown = {**issued.to_external_dict(), "unknown": "value"}
    with pytest.raises(EvidenceAttestationError) as unknown_error:
        evidence_attestation_from_mapping(unknown)
    _assert_code(EvidenceAttestationErrorCode.INVALID_ATTESTATION, unknown_error)
