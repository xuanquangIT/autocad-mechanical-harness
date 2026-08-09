"""Property 34: format, scope, size, and units are enforced atomically."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.application.services.drawing_read_service import DrawingReadService
from cad_harness.config import Settings
from cad_harness.domain.errors import ReadScopeTooLargeError, UnsupportedInputFormatError
from cad_harness.domain.models.drawing_model import (
    DrawingModel,
    DrawingSummary,
    EntityRecord,
    LineGeometry,
    ReadScope,
)
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef
from cad_harness.domain.ports.repositories import CancellationTokenPort


class RecordingSource:
    def __init__(self, count: int, factor: float | None) -> None:
        self.count = count
        self.factor = factor
        self.calls: list[str] = []

    def current_revision(self, document_id: str) -> str:
        self.calls.append("revision")
        return "sha256:stable"

    def summarize(self, request: DrawingReadRequest) -> DrawingSummary:
        self.calls.append("summarize")
        return DrawingSummary(
            document_id=request.source.ref,
            revision="sha256:stable",
            counts_by_entity_type={"AcDbLine": self.count} if self.count else {},
            counts_by_layer={"OBJECT": self.count} if self.count else {},
            counts_by_space={"model": self.count} if self.count else {},
        )

    def read(self, request: DrawingReadRequest) -> DrawingModel:
        self.calls.append("read")
        entities = tuple(
            EntityRecord(
                entity_ref=str(index),
                entity_type="AcDbLine",
                layer="OBJECT",
                visible=True,
                space="model",
                geometry=LineGeometry(start_mm=(0.0, 0.0), end_mm=(1.0, 0.0)),
                bounding_box_mm=(0.0, 0.0, 1.0, 0.0),
            )
            for index in range(self.count)
        )
        return DrawingModel(
            document_id=request.source.ref,
            revision="sha256:stable",
            display_name="drawing.dxf",
            source_unit_code="unknown" if self.factor is None else "mm",
            to_mm_factor=self.factor,
            geometry_normalized=self.factor is not None,
            scope=request.scope,
            entities=entities,
            arc_chord_tolerance_mm=0.01,
        )

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


# Feature: cad-ai-production-roadmap, Property 34: scope, format, and units enforced without partial results
@given(
    source_format=st.sampled_from(("dxf", "pdf", "png")),
    count=st.integers(min_value=0, max_value=15),
    limit=st.integers(min_value=1, max_value=10),
    detailed=st.booleans(),
    known_units=st.booleans(),
)
@settings(max_examples=120, deadline=None)
def test_read_policy_never_returns_partial_geometry(
    source_format: str, count: int, limit: int, detailed: bool, known_units: bool
) -> None:
    """**Validates: Requirements 13.6, 13.7, 13.9**"""
    port = RecordingSource(count, 1.0 if known_units else None)
    service = DrawingReadService(Settings.model_validate({"read": {"max_entities": limit}}), port)
    request = DrawingReadRequest(
        source=DrawingSourceRef(kind="file", format=source_format, ref="drawing.dxf"),
        scope=ReadScope() if detailed else None,
        max_entities=limit,
        max_block_nesting_depth=3,
    )

    if source_format != "dxf":
        with pytest.raises(UnsupportedInputFormatError):
            service.read(request)
        assert port.calls == []
    elif count > limit:
        with pytest.raises(ReadScopeTooLargeError):
            service.read(request)
        assert "summarize" in port.calls
        assert "read" not in port.calls
    else:
        result = service.read(request)
        if detailed:
            assert isinstance(result, DrawingModel)
            assert len(result.entities) == count
            assert result.geometry_normalized is known_units
        else:
            assert isinstance(result, DrawingSummary)
            assert "read" not in port.calls
