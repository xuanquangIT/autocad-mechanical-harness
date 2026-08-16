from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from cad_harness.application.live_session_proof import (
    _decode,
    issue_live_session_proof,
    verify_live_session_proof,
)
from cad_harness.application.manual_gate import (
    LIVE_SETUP_STEPS,
    ManualStepId,
    required_live_setup_steps,
)
from cad_harness.domain.errors import (
    ApprovalExpiredError,
    ApprovalRequiredError,
    ApprovalScopeMismatchError,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
SECRET = "development-test-secret"
SCOPE = {
    "adapter_type": "dotnet_bridge",
    "process_id": 9260,
    "document_id": "doc-live",
    "revision": "sha256:revision",
    "company_profile": "demo-profile@1.0",
}


def _token(**overrides: object) -> str:
    values: dict[str, object] = {
        **SCOPE,
        "setup_steps": LIVE_SETUP_STEPS,
        "secret": SECRET,
        "now": NOW,
    }
    values.update(overrides)
    return issue_live_session_proof(**values)  # type: ignore[arg-type]


def test_exact_live_session_scope_verifies_through_expiry_boundary() -> None:
    token = _token()

    verify_live_session_proof(token, SECRET, **SCOPE, now=NOW + timedelta(minutes=15))


def test_com_proof_contains_only_com_setup_evidence() -> None:
    scope = {**SCOPE, "adapter_type": "com"}
    setup_steps = required_live_setup_steps("com")
    token = issue_live_session_proof(
        **scope,
        setup_steps=setup_steps,
        secret=SECRET,
        now=NOW,
    )

    verify_live_session_proof(token, SECRET, **scope, now=NOW)
    _, payload, _ = token.split(".")
    padding = "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload + padding))
    assert claims["setup_steps"] == [step.value for step in setup_steps]
    assert ManualStepId.INSTALL_BRIDGE_BUNDLE.value not in claims["setup_steps"]
    assert ManualStepId.GRANT_NAMED_PIPE_ACL.value not in claims["setup_steps"]


def test_decode_rejects_bridge_setup_claimed_for_com() -> None:
    token = issue_live_session_proof(
        **{**SCOPE, "adapter_type": "com"},
        setup_steps=required_live_setup_steps("com"),
        secret=SECRET,
        now=NOW,
    )
    _, payload, _ = token.split(".")
    padding = "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload + padding))
    claims["setup_steps"] = [step.value for step in LIVE_SETUP_STEPS]
    invalid_payload = (
        base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )

    with pytest.raises(ValueError, match="setup steps"):
        _decode(invalid_payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("adapter_type", "com"),
        ("process_id", 9261),
        ("document_id", "doc-other"),
        ("revision", "sha256:other"),
        ("company_profile", "other-profile@2.0"),
    ],
)
def test_scope_change_is_rejected(field: str, value: object) -> None:
    expected = dict(SCOPE)
    expected[field] = value

    with pytest.raises(ApprovalScopeMismatchError):
        verify_live_session_proof(_token(), SECRET, **expected, now=NOW)  # type: ignore[arg-type]


def test_tamper_wrong_secret_and_expiry_fail_closed() -> None:
    token = _token()
    version, payload, digest = token.split(".")

    with pytest.raises(ApprovalScopeMismatchError):
        verify_live_session_proof(f"{version}.{payload}A.{digest}", SECRET, **SCOPE, now=NOW)
    with pytest.raises(ApprovalScopeMismatchError):
        verify_live_session_proof(token, "wrong-secret", **SCOPE, now=NOW)
    with pytest.raises(ApprovalExpiredError):
        verify_live_session_proof(
            token, SECRET, **SCOPE, now=NOW + timedelta(minutes=15, seconds=1)
        )


def test_missing_or_incomplete_manual_authority_cannot_issue() -> None:
    with pytest.raises(ApprovalRequiredError):
        _token(secret="")
    with pytest.raises(ApprovalRequiredError):
        _token(setup_steps=LIVE_SETUP_STEPS[:-1])
    with pytest.raises(ApprovalRequiredError):
        issue_live_session_proof(
            **{**SCOPE, "adapter_type": "com"},
            setup_steps=LIVE_SETUP_STEPS,
            secret=SECRET,
            now=NOW,
        )
    with pytest.raises(ValueError):
        _token(ttl=timedelta(minutes=16))


def test_naive_timestamp_and_future_token_fail_closed() -> None:
    with pytest.raises(ValueError):
        _token(now=datetime(2026, 8, 15, 12, 0))

    token = _token(now=NOW + timedelta(seconds=1))
    with pytest.raises(ApprovalScopeMismatchError):
        verify_live_session_proof(token, SECRET, **SCOPE, now=NOW)
