"""COM adapter integration tests.

Skipped unless AutoCAD is running on Windows with a document open. Run explicitly:

    uv run pytest -m com

These tests write to the active drawing. Open a scratch document, never a real one.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.com]


def _adapter():
    """Attach to a running AutoCAD, or skip.

    ``launch_if_missing`` stays False on purpose: a test suite must not start AutoCAD
    and take over the developer's session.
    """
    if os.getenv("CAD_HARNESS_LIVE_COM_WRITE_ACCEPTANCE") != "1":
        pytest.skip(
            "active-document COM write acceptance is disabled; use an explicit disposable "
            "drawing and CAD_HARNESS_LIVE_COM_WRITE_ACCEPTANCE=1"
        )
    if sys.platform != "win32":
        pytest.skip("COM adapter requires Windows")
    try:
        from cad_harness.adapters.autocad_com import ComAutoCADAdapter
    except ImportError:
        pytest.skip("pywin32 not installed. Run: uv sync --extra com")

    from cad_harness.domain.errors import HarnessError

    adapter = ComAutoCADAdapter("autocad")
    try:
        adapter.connect(launch_if_missing=False)
    except HarnessError as error:
        pytest.skip(f"AutoCAD not available: {error.code.value}")
    return adapter


@pytest.fixture
def com_adapter():
    adapter = _adapter()

    # The COM adapter maps an already-resolved plan; it must never invent or select
    # layers. This scratch-drawing fixture therefore provisions the profile contract
    # before any write, just as a production DWT/DWS setup would.
    from cad_harness.company_rules.loader import load_profile

    profile = load_profile("demo-profile@1.0")
    document = adapter._require_document()
    existing = {str(document.Layers.Item(index).Name) for index in range(document.Layers.Count)}
    for layer in profile.layers:
        if layer.name not in existing:
            document.Layers.Add(layer.name)
            existing.add(layer.name)

    yield adapter
    adapter.disconnect()


class TestConnection:
    def test_status_reports_a_version_and_document(self, com_adapter) -> None:
        status = com_adapter.status()
        assert status.available is True
        assert status.cad_version
        assert status.active_document_id


class TestCapabilityHonesty:
    def test_com_does_not_claim_atomic_transactions(self, com_adapter) -> None:
        """The gap that justifies the C# bridge must be declared, not implied."""
        from cad_harness.domain.ports.autocad_adapter import AdapterCapability

        assert not com_adapter.supports(AdapterCapability.ATOMIC_TRANSACTION)
        assert not com_adapter.supports(AdapterCapability.DOCUMENT_LOCK)
        assert not com_adapter.supports(AdapterCapability.STABLE_METADATA)

    def test_preview_is_refused_rather_than_faked(self, com_adapter) -> None:
        from tests.contract.test_adapter_contract import sample_plan

        from cad_harness.domain.errors import AdapterCapabilityMissingError

        with pytest.raises(AdapterCapabilityMissingError):
            com_adapter.preview(sample_plan())


class TestInspection:
    def test_document_snapshot_has_layers_and_a_revision(self, com_adapter) -> None:
        from cad_harness.domain.ports.autocad_adapter import InspectRequest

        snapshot = com_adapter.inspect_document(InspectRequest())
        assert snapshot.revision.startswith("sha256:")
        assert any(layer.name == "0" for layer in snapshot.layers)
        # The raw path must not leak into the snapshot.
        assert snapshot.path_hash.startswith("sha256:")

    def test_revision_is_stable_between_reads(self, com_adapter) -> None:
        from cad_harness.domain.ports.autocad_adapter import InspectRequest

        first = com_adapter.inspect_document(InspectRequest())
        second = com_adapter.inspect_document(InspectRequest())
        assert first.revision == second.revision

    def test_stale_revision_is_detected(self, com_adapter) -> None:
        from cad_harness.domain.ports.autocad_adapter import InspectRequest

        snapshot = com_adapter.inspect_document(InspectRequest())
        assert com_adapter.validate_revision(snapshot.document_id, snapshot.revision)
        assert not com_adapter.validate_revision(snapshot.document_id, "sha256:stale")


class TestCommit:
    def test_plate_and_holes_are_created_and_measured(self, com_adapter) -> None:
        """Writes a 160x100 outline plus four holes into the active drawing."""
        from tests.contract.test_adapter_contract import sample_plan

        from cad_harness.domain.ports.autocad_adapter import CommitRequest, InspectRequest

        snapshot = com_adapter.inspect_document(InspectRequest())
        plan = sample_plan(document_id=snapshot.document_id)

        result = com_adapter.commit(
            CommitRequest(
                plan=plan,
                idempotency_key="integration-key-1",
                expected_revision=snapshot.revision,
                approval_token="integration-token",
                create_checkpoint=False,
            )
        )

        assert len(result.entity_results) == 5
        assert result.new_revision != result.previous_revision

        outline = next(e for e in result.entity_results if e.operation_id == "op-outline")
        assert outline.measurements["closed"] is True
        assert outline.measurements["area_mm2"] == pytest.approx(16000.0, abs=1e-3)
        assert outline.entity_ref.startswith("acad:handle:")

    def test_stale_revision_blocks_the_write(self, com_adapter) -> None:
        from tests.contract.test_adapter_contract import sample_plan

        from cad_harness.domain.errors import StaleDocumentRevisionError
        from cad_harness.domain.ports.autocad_adapter import CommitRequest, InspectRequest

        snapshot = com_adapter.inspect_document(InspectRequest())
        with pytest.raises(StaleDocumentRevisionError):
            com_adapter.commit(
                CommitRequest(
                    plan=sample_plan(document_id=snapshot.document_id),
                    idempotency_key="integration-key-2",
                    expected_revision="sha256:not-the-current-revision",
                    approval_token="integration-token",
                    create_checkpoint=False,
                )
            )
