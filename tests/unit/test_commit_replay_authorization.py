"""Authorization and adapter-isolation checks for idempotent commit replay."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.config import Settings
from cad_harness.domain.errors import (
    ApprovalExpiredError,
    ApprovalRequiredError,
    ApprovalScopeMismatchError,
    IdempotencyKeyReusedError,
)
from cad_harness.domain.models.result import CommitResult
from cad_harness.domain.models.validation import Severity, ValidationStage
from cad_harness.security.approval import make_approval_token


def _commit_once(
    settings: Settings,
    adapter: FakeAutoCADAdapter,
    spec: dict[str, Any],
) -> tuple[HarnessService, str, str, str, str, CommitResult]:
    service = HarnessService(settings, adapter)
    job = service.create_job()
    submitted = service.submit_spec(job.job_id, spec)
    plan_hash = str(submitted["plan_hash"])
    service.preview(job.job_id)
    report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    warnings = tuple(
        finding.rule_id for finding in report.findings if finding.severity is Severity.WARNING
    )
    _, token = service.approve(job.job_id, "replay-test-engineer", warnings)
    result = service.commit(
        job.job_id,
        idempotency_key="replay-auth-key",
        expected_revision=job.expected_revision,
        plan_hash=plan_hash,
        approval_token=token,
    )
    return service, job.job_id, job.expected_revision, plan_hash, token, result


def _deny_adapter_access(adapter: FakeAutoCADAdapter) -> ExitStack:
    """Make any adapter contact after the first commit fail the test immediately."""
    stack = ExitStack()
    for method_name in ("status", "validate_revision", "commit"):
        stack.enter_context(
            patch.object(
                adapter,
                method_name,
                side_effect=AssertionError(f"replay contacted adapter.{method_name}"),
            )
        )
    return stack


def test_exact_authorized_replay_returns_receipt_without_adapter_contact(
    settings: Settings,
    adapter: FakeAutoCADAdapter,
    base_plate_spec: dict[str, Any],
) -> None:
    service, job_id, revision, plan_hash, token, first = _commit_once(
        settings, adapter, base_plate_spec
    )
    writes_after_first_commit = adapter.document.write_counter

    with _deny_adapter_access(adapter):
        replay = service.commit(
            job_id,
            idempotency_key="replay-auth-key",
            expected_revision=revision,
            plan_hash=plan_hash,
            approval_token=token,
        )

    assert replay == first
    assert adapter.document.write_counter == writes_after_first_commit


@pytest.mark.parametrize("invalid_token", ["", "garbage", "v2.not-base64.deadbeef"])
def test_invalid_token_cannot_retrieve_stored_commit_receipt(
    settings: Settings,
    adapter: FakeAutoCADAdapter,
    base_plate_spec: dict[str, Any],
    invalid_token: str,
) -> None:
    service, job_id, revision, plan_hash, _, _ = _commit_once(settings, adapter, base_plate_spec)
    writes_after_first_commit = adapter.document.write_counter

    with _deny_adapter_access(adapter), pytest.raises(ApprovalScopeMismatchError):
        service.commit(
            job_id,
            idempotency_key="replay-auth-key",
            expected_revision=revision,
            plan_hash=plan_hash,
            approval_token=invalid_token,
        )

    assert adapter.document.write_counter == writes_after_first_commit


def test_expired_stored_approval_cannot_retrieve_commit_receipt(
    settings: Settings,
    adapter: FakeAutoCADAdapter,
    base_plate_spec: dict[str, Any],
) -> None:
    service, job_id, revision, plan_hash, _, _ = _commit_once(settings, adapter, base_plate_spec)
    job = service.store.get_job(job_id)
    assert job is not None and job.approval_id is not None
    approval = service.store.get_approval(job.approval_id)
    assert approval is not None
    expired = approval.model_copy(
        update={"expires_at": approval.approved_at - timedelta(seconds=1)}
    )
    service.store.save_approval(expired)
    expired_token = make_approval_token(expired, settings.approval_secret())

    with _deny_adapter_access(adapter), pytest.raises(ApprovalExpiredError):
        service.commit(
            job_id,
            idempotency_key="replay-auth-key",
            expected_revision=revision,
            plan_hash=plan_hash,
            approval_token=expired_token,
        )


def test_missing_stored_approval_cannot_retrieve_commit_receipt(
    settings: Settings,
    adapter: FakeAutoCADAdapter,
    base_plate_spec: dict[str, Any],
) -> None:
    service, job_id, revision, plan_hash, token, _ = _commit_once(
        settings, adapter, base_plate_spec
    )
    job = service.store.get_job(job_id)
    assert job is not None
    service.store.save_job(job.model_copy(update={"approval_id": None}))

    with _deny_adapter_access(adapter), pytest.raises(ApprovalRequiredError):
        service.commit(
            job_id,
            idempotency_key="replay-auth-key",
            expected_revision=revision,
            plan_hash=plan_hash,
            approval_token=token,
        )


def test_digest_conflict_is_rejected_before_replay_authorization(
    settings: Settings,
    adapter: FakeAutoCADAdapter,
    base_plate_spec: dict[str, Any],
) -> None:
    service, job_id, revision, plan_hash, _, _ = _commit_once(settings, adapter, base_plate_spec)

    with _deny_adapter_access(adapter), pytest.raises(IdempotencyKeyReusedError):
        service.commit(
            job_id,
            idempotency_key="replay-auth-key",
            expected_revision=f"{revision}-different",
            plan_hash=plan_hash,
            approval_token="garbage",
        )


def test_approval_disabled_configuration_keeps_authorization_bypass_for_replay(
    settings: Settings,
    adapter: FakeAutoCADAdapter,
    base_plate_spec: dict[str, Any],
) -> None:
    service, job_id, revision, plan_hash, _, first = _commit_once(
        settings, adapter, base_plate_spec
    )
    service.settings = settings.model_copy(
        update={"security": settings.security.model_copy(update={"require_commit_approval": False})}
    )

    with _deny_adapter_access(adapter):
        replay = service.commit(
            job_id,
            idempotency_key="replay-auth-key",
            expected_revision=revision,
            plan_hash=plan_hash,
            approval_token="not-checked-when-disabled",
        )

    assert replay == first
