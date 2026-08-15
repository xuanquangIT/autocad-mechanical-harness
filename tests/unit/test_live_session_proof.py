from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cad_harness.application.live_session_proof import (
    issue_live_session_proof,
    verify_live_session_proof,
)
from cad_harness.application.manual_gate import LIVE_SETUP_STEPS
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("adapter_type", "com"),
        ("process_id", 9261),
        ("document_id", "doc-other"),
        ("revision", "sha256:other"),
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
    with pytest.raises(ValueError):
        _token(ttl=timedelta(minutes=16))


def test_naive_timestamp_and_future_token_fail_closed() -> None:
    with pytest.raises(ValueError):
        _token(now=datetime(2026, 8, 15, 12, 0))

    token = _token(now=NOW + timedelta(seconds=1))
    with pytest.raises(ApprovalScopeMismatchError):
        verify_live_session_proof(token, SECRET, **SCOPE, now=NOW)
