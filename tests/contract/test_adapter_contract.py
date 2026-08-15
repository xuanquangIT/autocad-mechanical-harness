"""Adapter contract suite (architecture section 22.5).

The same expectations run against every adapter, gated on declared capability rather
than assuming all adapters can do everything.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from cad_harness.adapters.base import BaseAdapter
from cad_harness.adapters.dotnet_bridge import (
    DotNetBridgeAdapter,
    build_request,
    decode_frame,
    encode_frame,
)
from cad_harness.adapters.dxf_preview import DxfPreviewAdapter
from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.domain.errors import AdapterCapabilityMissingError
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.ports.autocad_adapter import (
    AdapterCapability,
    AutoCADAdapter,
    InspectRequest,
)


def sample_plan(job_id: str = "job_1", document_id: str = "doc_1") -> OperationPlan:
    return OperationPlan(
        plan_id="plan_1",
        job_id=job_id,
        document_id=document_id,
        expected_revision="sha256:rev",
        profile_ref="demo-profile@1.0",
        operations=(
            Operation(
                operation_id="op-outline",
                feature_id="plate-1",
                type=OperationType.CREATE_CLOSED_POLYLINE,
                layer="OBJECT",
                geometry={
                    "vertices_mm": [[0, 0], [160, 0], [160, 100], [0, 100]],
                },
                expected={"closed": True, "area_mm2": 16000.0, "vertex_count": 4},
            ),
            Operation(
                operation_id="op-holes",
                feature_id="plate-1-holes",
                type=OperationType.CREATE_CIRCLES,
                layer="OBJECT",
                geometry={
                    "centers_mm": [[20, 20], [140, 20], [20, 80], [140, 80]],
                    "diameter_mm": 14.0,
                },
                expected={"count": 4, "diameter_mm": 14.0},
            ),
        ),
    ).with_hash()


def unavailable_bridge() -> DotNetBridgeAdapter:
    """Return a bridge isolated from any real AutoCAD pipe on the test machine."""
    return DotNetBridgeAdapter(
        rf"\\.\pipe\cad-harness-test-unavailable-{uuid4().hex}",
        timeout_seconds=0.1,
    )


@pytest.fixture(params=["fake", "dxf_preview", "dotnet_bridge"])
def any_adapter(request: pytest.FixtureRequest, tmp_path: Path) -> BaseAdapter:
    if request.param == "fake":
        return FakeAutoCADAdapter()
    if request.param == "dxf_preview":
        return DxfPreviewAdapter(tmp_path / "previews")
    return unavailable_bridge()


class TestPortConformance:
    def test_adapters_satisfy_the_port(self, any_adapter: BaseAdapter) -> None:
        assert isinstance(any_adapter, AutoCADAdapter)

    def test_status_declares_capabilities_honestly(self, any_adapter: BaseAdapter) -> None:
        status = any_adapter.status()
        assert status.adapter_type
        assert set(status.capabilities) == set(any_adapter.capabilities)

    def test_unsupported_operations_fail_loudly(self, any_adapter: BaseAdapter) -> None:
        """A missing capability raises; it never silently does nothing."""
        if not any_adapter.supports(AdapterCapability.INSPECT_DOCUMENT):
            with pytest.raises(AdapterCapabilityMissingError):
                any_adapter.inspect_document(InspectRequest())


class TestFakeAdapterBehaviour:
    def test_revision_changes_after_a_write(self) -> None:
        from cad_harness.domain.ports.autocad_adapter import CommitRequest

        adapter = FakeAutoCADAdapter()
        before = adapter.current_revision()
        plan = sample_plan(document_id=adapter.document.document_id)
        adapter.commit(
            CommitRequest(
                plan=plan,
                idempotency_key="key-1",
                expected_revision=before,
                approval_token="token",
            )
        )
        assert adapter.current_revision() != before

    def test_measurements_are_derived_not_echoed(self) -> None:
        """If the adapter echoed `expected`, post-commit validation would be vacuous."""
        from cad_harness.domain.ports.autocad_adapter import CommitRequest

        adapter = FakeAutoCADAdapter()
        plan = sample_plan(document_id=adapter.document.document_id)
        result = adapter.commit(
            CommitRequest(
                plan=plan,
                idempotency_key="key-1",
                expected_revision=adapter.current_revision(),
                approval_token="token",
            )
        )
        outline = next(e for e in result.entity_results if e.operation_id == "op-outline")
        assert outline.measurements["area_mm2"] == pytest.approx(16000.0)
        assert outline.measurements["perimeter_mm"] == pytest.approx(520.0)


class TestPreviewAdapter:
    def test_preview_writes_dxf_and_svg(self, tmp_path: Path) -> None:
        adapter = DxfPreviewAdapter(tmp_path / "previews")
        result = adapter.preview(sample_plan())
        kinds = {artifact.kind for artifact in result.artifacts}
        assert kinds == {"dxf", "svg"}
        for artifact in result.artifacts:
            assert Path(artifact.artifact_ref).is_file()

    def test_preview_reports_unrenderable_operations(self, tmp_path: Path) -> None:
        adapter = DxfPreviewAdapter(tmp_path / "previews")
        plan = OperationPlan(
            plan_id="plan_2",
            job_id="job_1",
            document_id="doc_1",
            expected_revision="sha256:rev",
            profile_ref="demo-profile@1.0",
            operations=(
                Operation(
                    operation_id="op-dim",
                    feature_id="plate-1",
                    type=OperationType.CREATE_LINEAR_DIMENSION,
                    layer="DIM",
                    geometry={},
                ),
            ),
        )
        assert adapter.preview_gaps(plan) == []

    def test_preview_adapter_cannot_confirm_a_revision(self, tmp_path: Path) -> None:
        adapter = DxfPreviewAdapter(tmp_path / "previews")
        assert adapter.validate_revision("doc_1", "sha256:rev") is False


class TestBridgeIpcContract:
    def test_frame_round_trip(self) -> None:
        payload = build_request("inspect_document", {"document_id": "doc_1"}, request_id="req_1")
        assert decode_frame(encode_frame(payload)) == payload

    def test_truncated_frame_is_rejected(self) -> None:
        frame = encode_frame({"a": 1})
        with pytest.raises(ValueError, match="truncated"):
            decode_frame(frame[:-1])

    def test_bridge_is_declared_unavailable(self) -> None:
        status = unavailable_bridge().status()
        assert status.available is False
        assert status.capabilities == ()
        assert "Phase 5" in str(status.message)

    def test_planned_capabilities_include_atomic_transaction(self) -> None:
        """The reason the bridge exists: guarantees COM cannot give."""
        planned = DotNetBridgeAdapter.PLANNED_CAPABILITIES
        assert AdapterCapability.ATOMIC_TRANSACTION in planned
        assert AdapterCapability.DOCUMENT_LOCK in planned
