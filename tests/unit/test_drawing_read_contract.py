"""Focused examples for DrawingModel and DrawingReadService edge behavior."""

import pytest
from pydantic import ValidationError

from cad_harness.application.services.drawing_read_service import DrawingReadService
from cad_harness.config import Settings
from cad_harness.domain.errors import StaleDocumentRevisionError
from cad_harness.domain.models.drawing_model import DrawingModel, DrawingSummary, ReadScope
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef
from cad_harness.domain.ports.repositories import CancellationTokenPort


class ChangingSummarySource:
    def __init__(self) -> None:
        self.revision_reads = 0

    def current_revision(self, document_id: str) -> str:
        self.revision_reads += 1
        return f"sha256:{self.revision_reads}"

    def summarize(self, request: DrawingReadRequest) -> DrawingSummary:
        return DrawingSummary(
            document_id=request.source.ref,
            revision="sha256:1",
            counts_by_entity_type={},
            counts_by_layer={},
            counts_by_space={},
        )

    def read(self, request: DrawingReadRequest) -> DrawingModel:
        raise AssertionError("summary-only requests must not read geometry")

    def current_revision_cancellable(
        self, document_id: str, deadline: CancellationTokenPort
    ) -> str:
        deadline.checkpoint()
        return self.current_revision(document_id)

    def summarize_cancellable(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort
    ) -> DrawingSummary:
        deadline.checkpoint()
        return self.summarize(request)

    def read_cancellable(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort
    ) -> DrawingModel:
        deadline.checkpoint()
        return self.read(request)


def test_to_mm_factor_is_required_and_unknown_units_are_explicit() -> None:
    payload = {
        "document_id": "doc",
        "revision": "rev",
        "display_name": "drawing.dxf",
        "source_unit_code": "unknown",
        "geometry_normalized": False,
        "scope": ReadScope(),
        "arc_chord_tolerance_mm": 0.01,
    }
    with pytest.raises(ValidationError):
        DrawingModel.model_validate(payload)
    model = DrawingModel.model_validate({**payload, "to_mm_factor": None})
    assert model.to_mm_factor is None
    assert not model.geometry_normalized


def test_revision_change_rejects_even_summary_result() -> None:
    source = ChangingSummarySource()
    service = DrawingReadService(Settings(), source)
    request = DrawingReadRequest(
        source=DrawingSourceRef(kind="file", format="dxf", ref="drawing.dxf"),
        scope=None,
        max_entities=10,
        max_block_nesting_depth=3,
    )
    with pytest.raises(StaleDocumentRevisionError):
        service.read(request)
