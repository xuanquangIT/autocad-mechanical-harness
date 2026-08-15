"""Same-session Python gate for an already-journaled whole-DWG restore."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import cad_harness.domain.models.approval as approval_models
from cad_harness.adapters.autocad_com import ComAutoCADAdapter
from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter
from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.config import Settings
from cad_harness.domain.errors import (
    ApprovalScopeMismatchError,
    InvalidFeatureParametersError,
    RollbackRecoveryRequiredError,
)
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.result import RollbackResult
from cad_harness.domain.ports.autocad_adapter import (
    AdapterCapability,
    AdapterStatus,
    InspectRequest,
    RollbackRequest,
)
from cad_harness.security.rollback_approval import (
    issue_rollback_approval,
    rollback_approval_token_digest,
)


class _UnusedTransport:
    def request(self, envelope: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        raise AssertionError("The recording adapter overrides every exercised bridge operation")


class _RecordingRecoveryBridge(DotNetBridgeAdapter):
    """A real DotNetBridgeAdapter type with deterministic local operation hooks."""

    def __init__(self) -> None:
        super().__init__(transport=_UnusedTransport())
        self._fake = FakeAutoCADAdapter()
        self.capabilities = frozenset({AdapterCapability.CHECKPOINT_RESTORE})
        self.rollback_calls: list[RollbackRequest] = []
        self.status_calls = 0
        self.validation_calls = 0
        self.recovery_failures_remaining = 1

    def status(self) -> AdapterStatus:
        self.status_calls += 1
        return AdapterStatus(
            adapter_type="dotnet_bridge",
            available=True,
            capabilities=(AdapterCapability.CHECKPOINT_RESTORE,),
            cad_application="AutoCAD",
            cad_version="26.0",
            active_document_id=self._fake.document.document_id,
        )

    def inspect_document(self, request: InspectRequest):
        return self._fake.inspect_document(request)

    def validate_revision(self, document_id: str, expected_revision: str) -> bool:
        self.validation_calls += 1
        return self._fake.validate_revision(document_id, expected_revision)

    def rollback(self, request: RollbackRequest) -> RollbackResult:
        self.rollback_calls.append(request)
        if self.recovery_failures_remaining:
            self.recovery_failures_remaining -= 1
            raise RollbackRecoveryRequiredError("journal retry required")
        return RollbackResult(
            job_id=request.job_id,
            restored_revision="sha256:restored",
            checkpoint_id=request.checkpoint_id,
            method="checkpoint_restore",
        )


class _JournalSimulatingBridge(_RecordingRecoveryBridge):
    """Models the final C# journal proof retained across a Python service restart."""

    def __init__(self) -> None:
        super().__init__()
        self.recovery_failures_remaining = 0
        self.recorded_digest: str | None = None
        self.recorded_scope: tuple[str, str, str, str] | None = None

    def rollback(self, request: RollbackRequest) -> RollbackResult:
        self.rollback_calls.append(request)
        supplied_scope = (
            request.job_id,
            request.document_id,
            request.checkpoint_id,
            request.current_revision,
        )
        if (
            self.recorded_digest is None
            or self.recorded_scope != supplied_scope
            or self.recorded_digest
            != rollback_approval_token_digest(request.rollback_approval_token)
        ):
            raise InvalidFeatureParametersError("C# journal rejected the recovery candidate")
        return RollbackResult(
            job_id=request.job_id,
            restored_revision="sha256:restored",
            checkpoint_id=request.checkpoint_id,
            method="checkpoint_restore",
        )


def _service_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[HarnessService, _RecordingRecoveryBridge, dict[str, str], str]:
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "recovery-service-secret")
    adapter = _RecordingRecoveryBridge()
    service = HarnessService(Settings(), adapter)
    job = service.create_job()
    service.store.save_job(
        job.model_copy(update={"state": JobState.COMMITTED, "checkpoint_id": "checkpoint-1"})
    )
    scope = service.rollback_scope(job.job_id)
    _, token = service.approve_rollback(
        job.job_id,
        "engineer-1",
        displayed_checkpoint_id=scope["checkpoint_id"],
        displayed_current_revision=scope["current_revision"],
    )
    return service, adapter, scope, token


def test_expired_exact_original_retry_reaches_only_the_bridge_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, adapter, scope, token = _service_and_scope(monkeypatch)

    with pytest.raises(RollbackRecoveryRequiredError):
        service.rollback(
            scope["job_id"],
            checkpoint_id=scope["checkpoint_id"],
            current_revision=scope["current_revision"],
            rollback_approval_token=token,
        )
    assert len(adapter.rollback_calls) == 1
    assert adapter.status_calls == 1
    assert adapter.validation_calls == 1

    approval = service.approve_rollback(
        scope["job_id"],
        "engineer-clock-read",
        displayed_checkpoint_id=scope["checkpoint_id"],
        displayed_current_revision=scope["current_revision"],
    )[0]

    class _AfterExpiry(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return approval.expires_at + timedelta(microseconds=1)

    monkeypatch.setattr(approval_models, "datetime", _AfterExpiry)
    result = service.rollback(
        scope["job_id"],
        checkpoint_id=scope["checkpoint_id"],
        current_revision=scope["current_revision"],
        rollback_approval_token=token,
    )

    assert result.method == "checkpoint_restore"
    assert len(adapter.rollback_calls) == 2
    # Recovery must not inspect a document that may already be closed; C# journal
    # validation and semantic reopen verification are the final proof.
    assert adapter.status_calls == 1
    assert adapter.validation_calls == 1


def test_reissued_or_changed_scope_cannot_enter_pending_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, adapter, scope, token = _service_and_scope(monkeypatch)
    with pytest.raises(RollbackRecoveryRequiredError):
        service.rollback(
            scope["job_id"],
            checkpoint_id=scope["checkpoint_id"],
            current_revision=scope["current_revision"],
            rollback_approval_token=token,
        )

    _, reissued = issue_rollback_approval(
        job_id=scope["job_id"],
        document_id=scope["document_id"],
        checkpoint_id=scope["checkpoint_id"],
        current_revision=scope["current_revision"],
        approved_by="engineer-2",
        secret="recovery-service-secret",
        ttl=timedelta(minutes=5),
    )
    with pytest.raises(ApprovalScopeMismatchError, match="journaled attempt"):
        service.rollback(
            scope["job_id"],
            checkpoint_id=scope["checkpoint_id"],
            current_revision=scope["current_revision"],
            rollback_approval_token=reissued,
        )
    with pytest.raises(ApprovalScopeMismatchError, match="journaled attempt"):
        service.rollback(
            scope["job_id"],
            checkpoint_id=scope["checkpoint_id"],
            current_revision="sha256:changed",
            rollback_approval_token=token,
        )
    assert len(adapter.rollback_calls) == 1


def test_expired_token_without_process_local_cache_is_deferred_to_dotnet_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, adapter, scope, _ = _service_and_scope(monkeypatch)
    _, expired = issue_rollback_approval(
        job_id=scope["job_id"],
        document_id=scope["document_id"],
        checkpoint_id=scope["checkpoint_id"],
        current_revision=scope["current_revision"],
        approved_by="engineer-old",
        secret="recovery-service-secret",
        ttl=timedelta(minutes=1),
        now=datetime(2000, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(RollbackRecoveryRequiredError):
        service.rollback(
            scope["job_id"],
            checkpoint_id=scope["checkpoint_id"],
            current_revision=scope["current_revision"],
            rollback_approval_token=expired,
        )
    assert len(adapter.rollback_calls) == 1
    assert adapter.status_calls == 0
    assert adapter.validation_calls == 0


def test_only_dotnet_bridge_opts_into_expired_checkpoint_recovery() -> None:
    assert DotNetBridgeAdapter.allows_expired_checkpoint_recovery is True
    assert FakeAutoCADAdapter.allows_expired_checkpoint_recovery is False
    assert ComAutoCADAdapter.allows_expired_checkpoint_recovery is False


def _expired_token(scope: dict[str, str], *, approval_id_hint: str) -> str:
    del approval_id_hint
    return issue_rollback_approval(
        job_id=scope["job_id"],
        document_id=scope["document_id"],
        checkpoint_id=scope["checkpoint_id"],
        current_revision=scope["current_revision"],
        approved_by="engineer-recovery",
        secret="recovery-service-secret",
        ttl=timedelta(minutes=1),
        now=datetime(2000, 1, 1, tzinfo=UTC),
    )[1]


def _restarted_service(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[HarnessService, _JournalSimulatingBridge, dict[str, str]]:
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "recovery-service-secret")
    adapter = _JournalSimulatingBridge()
    original = HarnessService(Settings(), adapter)
    job = original.create_job()
    original.store.save_job(
        job.model_copy(update={"state": JobState.COMMITTED, "checkpoint_id": "checkpoint-restart"})
    )
    scope = original.rollback_scope(job.job_id)
    restarted = HarnessService(Settings(), adapter, store=original.store)
    assert restarted._durable_rollback_recoveries == {}
    return restarted, adapter, scope


def test_python_restart_routes_exact_expired_candidate_to_authenticated_bridge_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, adapter, scope = _restarted_service(monkeypatch)
    token = _expired_token(scope, approval_id_hint="original")
    adapter.recorded_digest = rollback_approval_token_digest(token)
    adapter.recorded_scope = (
        scope["job_id"],
        scope["document_id"],
        scope["checkpoint_id"],
        scope["current_revision"],
    )

    result = service.rollback(
        scope["job_id"],
        checkpoint_id=scope["checkpoint_id"],
        current_revision=scope["current_revision"],
        rollback_approval_token=token,
    )

    assert result.method == "checkpoint_restore"
    assert len(adapter.rollback_calls) == 1
    assert adapter.status_calls == 0
    assert adapter.validation_calls == 0
    assert service.store.get_job(scope["job_id"]).state is JobState.ROLLED_BACK  # type: ignore[union-attr]


def test_python_restart_missing_journal_rejects_without_job_state_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, adapter, scope = _restarted_service(monkeypatch)
    token = _expired_token(scope, approval_id_hint="missing")

    with pytest.raises(InvalidFeatureParametersError, match="journal rejected"):
        service.rollback(
            scope["job_id"],
            checkpoint_id=scope["checkpoint_id"],
            current_revision=scope["current_revision"],
            rollback_approval_token=token,
        )

    assert len(adapter.rollback_calls) == 1
    assert service.store.get_job(scope["job_id"]).state is JobState.COMMITTED  # type: ignore[union-attr]
    assert service._durable_rollback_recoveries == {}


def test_python_restart_defers_reissued_and_signed_wrong_scope_to_csharp_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, adapter, scope = _restarted_service(monkeypatch)
    original = _expired_token(scope, approval_id_hint="original")
    adapter.recorded_digest = rollback_approval_token_digest(original)
    adapter.recorded_scope = (
        scope["job_id"],
        scope["document_id"],
        scope["checkpoint_id"],
        scope["current_revision"],
    )

    reissued = _expired_token(scope, approval_id_hint="reissued")
    with pytest.raises(InvalidFeatureParametersError):
        service.rollback(
            scope["job_id"],
            checkpoint_id=scope["checkpoint_id"],
            current_revision=scope["current_revision"],
            rollback_approval_token=reissued,
        )

    wrong_scope = {**scope, "current_revision": "sha256:signed-wrong-scope"}
    signed_wrong_scope = _expired_token(wrong_scope, approval_id_hint="wrong-scope")
    with pytest.raises(InvalidFeatureParametersError):
        service.rollback(
            scope["job_id"],
            checkpoint_id=scope["checkpoint_id"],
            current_revision=wrong_scope["current_revision"],
            rollback_approval_token=signed_wrong_scope,
        )

    assert len(adapter.rollback_calls) == 2
    assert service.store.get_job(scope["job_id"]).state is JobState.COMMITTED  # type: ignore[union-attr]
