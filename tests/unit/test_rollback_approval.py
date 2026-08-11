"""Requirement 22.10: rollback requires separate exact human authorization."""

from __future__ import annotations

from datetime import timedelta

import pytest

from cad_harness.domain.errors import (
    ApprovalExpiredError,
    ApprovalRequiredError,
    ApprovalScopeMismatchError,
)
from cad_harness.security.approval import issue_approval
from cad_harness.security.rollback_approval import (
    issue_rollback_approval,
    make_rollback_approval_token,
    verify_rollback_approval_token,
)

SECRET = "rollback-test-secret"


def _issued():
    return issue_rollback_approval(
        job_id="job_rollback",
        document_id="doc_rollback",
        checkpoint_id="checkpoint_rollback",
        current_revision="sha256:current",
        approved_by="engineer-1",
        secret=SECRET,
        ttl=timedelta(minutes=5),
    )


def test_token_is_bound_to_every_destructive_scope_field() -> None:
    approval, token = _issued()
    verified = verify_rollback_approval_token(
        token,
        SECRET,
        job_id=approval.job_id,
        document_id=approval.document_id,
        checkpoint_id=approval.checkpoint_id,
        current_revision=approval.current_revision,
        now=approval.approved_at,
    )
    assert verified == approval

    scope = {
        "job_id": approval.job_id,
        "document_id": approval.document_id,
        "checkpoint_id": approval.checkpoint_id,
        "current_revision": approval.current_revision,
    }
    for field in scope:
        changed = {**scope, field: f"{scope[field]}-changed"}
        with pytest.raises(ApprovalScopeMismatchError):
            verify_rollback_approval_token(
                token,
                SECRET,
                **changed,
                now=approval.approved_at,
            )


def test_signature_is_checked_before_tampered_claims_are_used() -> None:
    approval, token = _issued()
    version, payload, digest = token.split(".")
    replacement = "A" if payload[0] != "A" else "B"
    tampered = f"{version}.{replacement}{payload[1:]}.{digest}"
    with pytest.raises(ApprovalScopeMismatchError, match="signature"):
        verify_rollback_approval_token(
            tampered,
            SECRET,
            job_id=approval.job_id,
            document_id=approval.document_id,
            checkpoint_id=approval.checkpoint_id,
            current_revision=approval.current_revision,
        )


def test_token_expires_only_after_the_signed_boundary() -> None:
    approval, token = _issued()
    verify_rollback_approval_token(
        token,
        SECRET,
        job_id=approval.job_id,
        document_id=approval.document_id,
        checkpoint_id=approval.checkpoint_id,
        current_revision=approval.current_revision,
        now=approval.expires_at,
    )
    with pytest.raises(ApprovalExpiredError):
        verify_rollback_approval_token(
            token,
            SECRET,
            job_id=approval.job_id,
            document_id=approval.document_id,
            checkpoint_id=approval.checkpoint_id,
            current_revision=approval.current_revision,
            now=approval.expires_at + timedelta(microseconds=1),
        )


def test_commit_approval_cannot_authorize_rollback_and_empty_secret_fails_closed() -> None:
    commit_approval, commit_token = issue_approval(
        job_id="job_rollback",
        document_id="doc_rollback",
        plan_hash="sha256:plan",
        expected_revision="sha256:current",
        approved_by="engineer-1",
        secret=SECRET,
        ttl=timedelta(minutes=5),
    )
    with pytest.raises(ApprovalScopeMismatchError):
        verify_rollback_approval_token(
            commit_token,
            SECRET,
            job_id=commit_approval.job_id,
            document_id=commit_approval.document_id,
            checkpoint_id="checkpoint_rollback",
            current_revision=commit_approval.expected_revision,
        )
    with pytest.raises(ApprovalRequiredError):
        issue_rollback_approval(
            job_id="job",
            document_id="doc",
            checkpoint_id="checkpoint",
            current_revision="revision",
            approved_by="engineer",
            secret="",
            ttl=timedelta(minutes=1),
        )


def test_previous_contract_version_cannot_authorize_rollback() -> None:
    approval, _ = _issued()
    legacy = approval.model_copy(update={"schema_version": "1.10"})
    token = make_rollback_approval_token(legacy, SECRET)

    with pytest.raises(ApprovalScopeMismatchError, match="contract version"):
        verify_rollback_approval_token(
            token,
            SECRET,
            job_id=approval.job_id,
            document_id=approval.document_id,
            checkpoint_id=approval.checkpoint_id,
            current_revision=approval.current_revision,
            now=approval.approved_at,
        )
