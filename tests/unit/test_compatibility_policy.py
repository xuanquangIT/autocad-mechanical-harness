"""Focused examples for the fail-closed live-writer compatibility boundary."""

from __future__ import annotations

import pytest

from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.config import Settings
from cad_harness.domain.errors import AdapterCapabilityMissingError
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.result import ExportResult, RollbackResult
from cad_harness.domain.ports.autocad_adapter import (
    AdapterStatus,
    ExportRequest,
    RollbackRequest,
)


class _UnsupportedLiveAdapter(FakeAutoCADAdapter):
    def __init__(self, detected_version: str = "99.9") -> None:
        super().__init__()
        self.detected_version = detected_version
        self.rollback_calls = 0
        self.export_calls = 0

    def status(self) -> AdapterStatus:
        return AdapterStatus(adapter_type="com", available=True, cad_version=self.detected_version)

    def rollback(self, request: RollbackRequest) -> RollbackResult:
        self.rollback_calls += 1
        return super().rollback(request)

    def export(self, request: ExportRequest) -> ExportResult:
        self.export_calls += 1
        return super().export(request)


@pytest.mark.parametrize(
    "detected",
    ("x24.3x", "garbage 24.3 payload", "24.3.999", "25.0 trailing"),
)
def test_version_like_substrings_do_not_enable_live_writer(detected: str) -> None:
    adapter = _UnsupportedLiveAdapter(detected)
    service = HarnessService(Settings(), adapter)

    with pytest.raises(AdapterCapabilityMissingError):
        service._require_writer_compatible()


def test_unsupported_live_adapter_never_reaches_rollback_implementation(monkeypatch) -> None:
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "compatibility-test-secret")
    adapter = _UnsupportedLiveAdapter()
    service = HarnessService(Settings(), adapter)
    job = service.create_job()
    service.store.save_job(
        job.model_copy(update={"state": JobState.COMMITTED, "checkpoint_id": "checkpoint-compat"})
    )
    scope = service.rollback_scope(job.job_id)
    _, token = service.approve_rollback(
        job.job_id,
        "engineer-compat",
        displayed_checkpoint_id=scope["checkpoint_id"],
        displayed_current_revision=scope["current_revision"],
    )

    with pytest.raises(AdapterCapabilityMissingError) as error:
        service.rollback(
            job.job_id,
            checkpoint_id=scope["checkpoint_id"],
            current_revision=scope["current_revision"],
            rollback_approval_token=token,
        )

    assert adapter.rollback_calls == 0
    assert error.value.details["detected_version"] == "99.9"
    assert error.value.details["supported_versions"]


def test_unsupported_live_adapter_never_reaches_export_implementation() -> None:
    adapter = _UnsupportedLiveAdapter()
    service = HarnessService(Settings(), adapter)

    with pytest.raises(AdapterCapabilityMissingError):
        service.export("document-fake", "data/exports/test.dxf", "dxf")

    assert adapter.export_calls == 0
