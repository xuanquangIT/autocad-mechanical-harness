"""Property 59: approval scope and exact TTL boundary."""

from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cad_harness.domain.errors import ApprovalExpiredError, ApprovalScopeMismatchError
from cad_harness.security.approval import issue_approval, verify_approval_token

SECRET = "property-secret"


@given(
    job_id=st.text(min_size=1, max_size=24),
    plan_hash=st.text(min_size=1, max_size=40),
    revision=st.text(min_size=1, max_size=40),
    changed_field=st.sampled_from(("job_id", "plan_hash", "expected_revision")),
)
def test_token_is_valid_for_exactly_one_scope_triple(
    job_id: str,
    plan_hash: str,
    revision: str,
    changed_field: str,
) -> None:
    """**Validates: Requirements 20.5, 22.4, 27.5**"""
    approval, token = issue_approval(
        job_id=job_id,
        document_id="doc-property",
        plan_hash=plan_hash,
        expected_revision=revision,
        approved_by="engineer",
        secret=SECRET,
        ttl=timedelta(minutes=5),
    )
    verify_approval_token(
        token,
        approval,
        SECRET,
        job_id=job_id,
        plan_hash=plan_hash,
        expected_revision=revision,
        now=approval.approved_at,
    )

    submitted = {
        "job_id": job_id,
        "plan_hash": plan_hash,
        "expected_revision": revision,
    }
    submitted[changed_field] += "-changed"
    with pytest.raises(ApprovalScopeMismatchError):
        verify_approval_token(
            token,
            approval,
            SECRET,
            job_id=submitted["job_id"],
            plan_hash=submitted["plan_hash"],
            expected_revision=submitted["expected_revision"],
            now=approval.approved_at,
        )


@given(ttl_seconds=st.integers(min_value=1, max_value=86_400))
def test_token_expires_only_after_elapsed_time_exceeds_ttl(ttl_seconds: int) -> None:
    """**Validates: Requirements 20.5, 27.6**"""
    ttl = timedelta(seconds=ttl_seconds)
    approval, token = issue_approval(
        job_id="job-ttl",
        document_id="doc-ttl",
        plan_hash="sha256:plan",
        expected_revision="sha256:revision",
        approved_by="engineer",
        secret=SECRET,
        ttl=ttl,
    )
    verify_approval_token(
        token,
        approval,
        SECRET,
        job_id=approval.job_id,
        plan_hash=approval.plan_hash,
        expected_revision=approval.expected_revision,
        now=approval.expires_at,
    )
    with pytest.raises(ApprovalExpiredError):
        verify_approval_token(
            token,
            approval,
            SECRET,
            job_id=approval.job_id,
            plan_hash=approval.plan_hash,
            expected_revision=approval.expected_revision,
            now=approval.expires_at + timedelta(microseconds=1),
        )
