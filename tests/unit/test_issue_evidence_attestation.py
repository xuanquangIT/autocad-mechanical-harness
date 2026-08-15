from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from scripts import issue_evidence_attestation as issuer
from scripts.issue_evidence_attestation import issue_from_files, main

from cad_harness.security.evidence_attestation import (
    EvidenceAttestationError,
    EvidenceRole,
    evidence_attestation_from_mapping,
    trust_policy_from_mapping,
    trust_policy_sha256,
    verify_attestation,
)

_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
_PRIVATE_ENV = "CAD_EVIDENCE_REVIEWER_PRIVATE_KEY"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _keypair(marker: int = 7) -> tuple[str, str]:
    key = Ed25519PrivateKey.from_private_bytes(bytes([marker]) * 32)
    return (
        _b64(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ),
        _b64(
            key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ),
    )


_PRIVATE, _PUBLIC = _keypair()


def _inputs(root: Path) -> tuple[Path, Path, dict[str, object], str]:
    policy_payload = {
        "schema_version": "2.0",
        "policy_kind": "production_evidence_trust_policy",
        "identities": [
            {
                "identity_id": "reviewer-opaque-01",
                "allowed_roles": [EvidenceRole.GOLDEN_REVIEWER.value],
                "public_key": _PUBLIC,
            }
        ],
    }
    claims: dict[str, object] = {
        "case_id": "case-001",
        "artifact_sha256": "sha256:" + "a" * 64,
    }
    policy_path = root / "policy.json"
    claims_path = root / "claims.json"
    policy_path.write_text(json.dumps(policy_payload), encoding="utf-8")
    claims_path.write_text(json.dumps(claims), encoding="utf-8")
    policy = trust_policy_from_mapping(policy_payload)
    return policy_path, claims_path, claims, trust_policy_sha256(policy)


def test_issue_from_files_creates_verifiable_ed25519_attestation_without_private_key(
    tmp_path: Path,
) -> None:
    policy_path, claims_path, claims, pin = _inputs(tmp_path)
    output = tmp_path / "attestation.json"

    result = issue_from_files(
        policy_path=policy_path,
        claims_path=claims_path,
        output_path=output,
        identity_id="reviewer-opaque-01",
        role=EvidenceRole.GOLDEN_REVIEWER,
        private_key_env_var=_PRIVATE_ENV,
        expected_policy_sha256=pin,
        expires_in_hours=24,
        env={_PRIVATE_ENV: _PRIVATE},
        now=_NOW,
    )

    raw = output.read_text(encoding="utf-8")
    attestation = evidence_attestation_from_mapping(json.loads(raw))
    policy = trust_policy_from_mapping(json.loads(policy_path.read_text(encoding="utf-8")))
    assert (
        verify_attestation(
            policy,
            attestation,
            claims,  # type: ignore[arg-type]
            expected_policy_sha256=pin,
            now=_NOW,
        ).identity_id
        == "reviewer-opaque-01"
    )
    assert result == {
        "ok": True,
        "role": EvidenceRole.GOLDEN_REVIEWER.value,
        "claims_sha256": attestation.claims_sha256,
        "policy_sha256": pin,
        "output_written": True,
    }
    assert _PRIVATE not in raw
    assert _PRIVATE_ENV not in raw
    assert "case-001" not in raw
    assert _PRIVATE not in policy_path.read_text(encoding="utf-8")


def test_unpinned_or_substituted_policy_and_wrong_private_key_fail(tmp_path: Path) -> None:
    policy_path, claims_path, _, pin = _inputs(tmp_path)
    common = {
        "policy_path": policy_path,
        "claims_path": claims_path,
        "output_path": tmp_path / "attestation.json",
        "identity_id": "reviewer-opaque-01",
        "role": EvidenceRole.GOLDEN_REVIEWER,
        "private_key_env_var": _PRIVATE_ENV,
        "expires_in_hours": 24,
        "now": _NOW,
    }
    with pytest.raises(EvidenceAttestationError):
        issue_from_files(
            **common,
            expected_policy_sha256="",
            env={_PRIVATE_ENV: _PRIVATE},
        )
    with pytest.raises(EvidenceAttestationError):
        issue_from_files(
            **common,
            expected_policy_sha256=f"sha256:{'0' * 64}",
            env={_PRIVATE_ENV: _PRIVATE},
        )
    with pytest.raises(EvidenceAttestationError):
        issue_from_files(
            **common,
            expected_policy_sha256=pin,
            env={_PRIVATE_ENV: _keypair(8)[0]},
        )


def test_output_is_append_only_and_wrong_role_or_private_mapping_fails(tmp_path: Path) -> None:
    policy_path, claims_path, _, pin = _inputs(tmp_path)
    output = tmp_path / "attestation.json"
    common = {
        "policy_path": policy_path,
        "claims_path": claims_path,
        "output_path": output,
        "identity_id": "reviewer-opaque-01",
        "expected_policy_sha256": pin,
        "expires_in_hours": 24,
        "now": _NOW,
    }
    with pytest.raises((EvidenceAttestationError, ValueError)):
        issue_from_files(
            **common,
            role=EvidenceRole.GOLDEN_REVIEWER,
            private_key_env_var=_PRIVATE_ENV,
            env={},
        )
    with pytest.raises((EvidenceAttestationError, ValueError)):
        issue_from_files(
            **common,
            role=EvidenceRole.ENGINEER_SELECTOR,
            private_key_env_var=_PRIVATE_ENV,
            env={_PRIVATE_ENV: _PRIVATE},
        )
    with pytest.raises(ValueError):
        issue_from_files(
            **common,
            role=EvidenceRole.GOLDEN_REVIEWER,
            private_key_env_var="Path",
            env={_PRIVATE_ENV: _PRIVATE},
        )
    issue_from_files(
        **common,
        role=EvidenceRole.GOLDEN_REVIEWER,
        private_key_env_var=_PRIVATE_ENV,
        env={_PRIVATE_ENV: _PRIVATE},
    )
    original = output.read_bytes()
    with pytest.raises(ValueError):
        issue_from_files(
            **common,
            role=EvidenceRole.GOLDEN_REVIEWER,
            private_key_env_var=_PRIVATE_ENV,
            env={_PRIVATE_ENV: _PRIVATE},
        )
    assert output.read_bytes() == original


def test_duplicate_json_keys_and_symlink_inputs_fail_closed(tmp_path: Path) -> None:
    policy_path, claims_path, _, pin = _inputs(tmp_path)
    claims_path.write_text('{"case_id":"one","case_id":"two"}', encoding="utf-8")
    with pytest.raises(ValueError):
        issue_from_files(
            policy_path=policy_path,
            claims_path=claims_path,
            output_path=tmp_path / "out.json",
            identity_id="reviewer-opaque-01",
            role=EvidenceRole.GOLDEN_REVIEWER,
            private_key_env_var=_PRIVATE_ENV,
            expected_policy_sha256=pin,
            expires_in_hours=24,
            env={_PRIVATE_ENV: _PRIVATE},
            now=_NOW,
        )

    target = tmp_path / "target.json"
    target.write_text('{"claim":"safe"}', encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError):
        issuer._read_json(link, maximum_bytes=1024)


def test_single_descriptor_reader_rejects_swap_between_lstat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "claims.json"
    replacement = tmp_path / "replacement.json"
    target.write_bytes(b'{"a":1}')
    replacement.write_bytes(b'{"b":2}')
    real_open = issuer.os.open
    swapped = False

    def swap_then_open(path: Path, flags: int) -> int:
        nonlocal swapped
        if Path(path) == target and not swapped:
            swapped = True
            os.replace(replacement, target)
        return real_open(path, flags)

    monkeypatch.setattr(issuer.os, "open", swap_then_open)
    with pytest.raises(ValueError, match="changed"):
        issuer._read_json(target, maximum_bytes=1024)
    assert swapped is True


def test_cli_uses_external_policy_pin_and_redacts_success_and_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path, claims_path, _, pin = _inputs(tmp_path)
    output = tmp_path / "output.json"
    monkeypatch.setenv(issuer.TRUST_POLICY_SHA256_ENV, pin)
    monkeypatch.setenv(_PRIVATE_ENV, _PRIVATE)
    assert (
        main(
            [
                "--trust-policy",
                str(policy_path),
                "--claims",
                str(claims_path),
                "--output",
                str(output),
                "--identity-id",
                "reviewer-opaque-01",
                "--role",
                EvidenceRole.GOLDEN_REVIEWER.value,
                "--private-key-env",
                _PRIVATE_ENV,
                "--expires-hours",
                "24",
            ]
        )
        == 0
    )
    rendered = capsys.readouterr().out
    assert _PRIVATE not in rendered
    assert _PRIVATE_ENV not in rendered
    assert "reviewer-opaque-01" not in rendered
    assert json.loads(rendered)["policy_sha256"] == pin

    sensitive = tmp_path / "customer-name" / "missing.json"
    assert (
        main(
            [
                "--trust-policy",
                str(sensitive),
                "--claims",
                str(sensitive),
                "--output",
                str(tmp_path / "failure.json"),
                "--identity-id",
                "sensitive-identity",
                "--role",
                EvidenceRole.GOLDEN_REVIEWER.value,
                "--private-key-env",
                _PRIVATE_ENV,
            ]
        )
        == 1
    )
    failure = capsys.readouterr().out
    assert str(sensitive) not in failure
    assert "sensitive-identity" not in failure
    assert json.loads(failure) == {
        "code": "EVIDENCE_ATTESTATION_ISSUE_FAILED",
        "ok": False,
    }
