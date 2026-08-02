"""Approval tokens, path allowlisting and redaction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cad_harness.domain.errors import (
    ApprovalExpiredError,
    ApprovalScopeMismatchError,
    ExportPathNotAllowedError,
)
from cad_harness.security.approval import issue_approval, verify_approval_token
from cad_harness.security.paths import ensure_path_allowed, is_path_allowed
from cad_harness.security.redaction import redact_path, redact_payload

SECRET = "unit-test-secret"
SCOPE = {
    "job_id": "job_1",
    "document_id": "doc_1",
    "plan_hash": "sha256:plan-a",
    "expected_revision": "sha256:rev-1",
}


def _issue(**overrides: object):
    payload = {
        **SCOPE,
        "approved_by": "engineer-1",
        "secret": SECRET,
        "ttl": timedelta(minutes=15),
        **overrides,
    }
    return issue_approval(**payload)  # type: ignore[arg-type]


class TestApprovalToken:
    def test_valid_token_passes(self) -> None:
        approval, token = _issue()
        verify_approval_token(
            token,
            approval,
            SECRET,
            job_id="job_1",
            plan_hash="sha256:plan-a",
            expected_revision="sha256:rev-1",
        )

    def test_different_plan_hash_is_rejected(self) -> None:
        """The core anti-replay property: an approval covers one plan only."""
        approval, token = _issue()
        with pytest.raises(ApprovalScopeMismatchError) as info:
            verify_approval_token(
                token,
                approval,
                SECRET,
                job_id="job_1",
                plan_hash="sha256:plan-b",
                expected_revision="sha256:rev-1",
            )
        assert info.value.details["submitted_plan_hash"] == "sha256:plan-b"

    def test_different_revision_is_rejected(self) -> None:
        approval, token = _issue()
        with pytest.raises(ApprovalScopeMismatchError):
            verify_approval_token(
                token,
                approval,
                SECRET,
                job_id="job_1",
                plan_hash="sha256:plan-a",
                expected_revision="sha256:rev-2",
            )

    def test_forged_token_is_rejected(self) -> None:
        approval, _ = _issue()
        with pytest.raises(ApprovalScopeMismatchError):
            verify_approval_token(
                f"{approval.approval_id}.deadbeef",
                approval,
                SECRET,
                job_id="job_1",
                plan_hash="sha256:plan-a",
                expected_revision="sha256:rev-1",
            )

    def test_token_signed_with_another_secret_is_rejected(self) -> None:
        approval, token = _issue()
        with pytest.raises(ApprovalScopeMismatchError):
            verify_approval_token(
                token,
                approval,
                "other-secret",
                job_id="job_1",
                plan_hash="sha256:plan-a",
                expected_revision="sha256:rev-1",
            )

    def test_expired_token_is_rejected(self) -> None:
        approval, token = _issue(ttl=timedelta(minutes=1))
        with pytest.raises(ApprovalExpiredError):
            verify_approval_token(
                token,
                approval,
                SECRET,
                job_id="job_1",
                plan_hash="sha256:plan-a",
                expected_revision="sha256:rev-1",
                now=datetime.now(UTC) + timedelta(minutes=2),
            )


class TestPathAllowlist:
    def test_path_inside_allowlist_is_accepted(self, tmp_path: Path) -> None:
        allowed = tmp_path / "exports"
        allowed.mkdir()
        result = ensure_path_allowed((allowed / "part.dxf"), (allowed,))
        assert result.name == "part.dxf"

    def test_path_outside_allowlist_is_rejected(self, tmp_path: Path) -> None:
        allowed = tmp_path / "exports"
        allowed.mkdir()
        with pytest.raises(ExportPathNotAllowedError):
            ensure_path_allowed(tmp_path / "elsewhere" / "part.dxf", (allowed,))

    def test_traversal_cannot_escape(self, tmp_path: Path) -> None:
        allowed = tmp_path / "exports"
        allowed.mkdir()
        assert not is_path_allowed(allowed / ".." / "secret.dwg", (allowed,))

    def test_existing_file_is_not_overwritten_silently(self, tmp_path: Path) -> None:
        allowed = tmp_path / "exports"
        allowed.mkdir()
        target = allowed / "part.dxf"
        target.write_text("existing", encoding="utf-8")

        with pytest.raises(ExportPathNotAllowedError):
            ensure_path_allowed(target, (allowed,))
        assert ensure_path_allowed(target, (allowed,), overwrite=True) == target.resolve()


class TestRedaction:
    def test_secrets_are_removed(self) -> None:
        redacted = redact_payload({"approval_token": "abc", "prompt": "draw a plate"})
        assert redacted == {"approval_token": "[redacted]", "prompt": "[redacted]"}

    def test_paths_become_stable_pseudonyms(self) -> None:
        first = redact_path("C:/Customers/AcmeCorp/part.dwg")
        assert "Acme" not in first
        assert first.endswith(".dwg")
        assert first == redact_path("C:/Customers/AcmeCorp/part.dwg")

    def test_redaction_is_recursive(self) -> None:
        redacted = redact_payload({"jobs": [{"target_path": "C:/x/y.dxf", "count": 2}]})
        assert redacted["jobs"][0]["count"] == 2
        assert redacted["jobs"][0]["target_path"].startswith("path:")
