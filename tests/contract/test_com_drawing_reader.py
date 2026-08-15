"""Focused read-only contract tests for ``ComDrawingReader``."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from cad_harness.adapters.com_drawing_reader import ComDrawingReader
from cad_harness.application.timeout import OperationDeadline
from cad_harness.domain.errors import (
    AdapterCapabilityMissingError,
    ComCallFailedError,
    UnsupportedInputFormatError,
)
from cad_harness.domain.models.document import (
    DocumentSnapshot,
    EntitySummary,
    SelectionSnapshot,
)
from cad_harness.domain.models.drawing_model import (
    CircleGeometry,
    DrawingModel,
    EntityRecord,
    ReadScope,
)
from cad_harness.domain.ports.autocad_adapter import InspectRequest, SelectionRequest
from cad_harness.domain.ports.drawing_source import (
    DrawingReadRequest,
    DrawingSourcePort,
    DrawingSourceRef,
)
from cad_harness.domain.value_objects.units import Unit

DOCUMENT_ID = "doc_COM_READER"
REVISION = "sha256:stable-com-revision"


@dataclass(slots=True)
class InspectionOnlyAdapter:
    document: DocumentSnapshot
    selection: SelectionSnapshot
    calls: list[InspectRequest | SelectionRequest] = field(default_factory=list)

    def inspect_document(self, request: InspectRequest) -> DocumentSnapshot:
        self.calls.append(request)
        return self.document

    def inspect_selection(self, request: SelectionRequest) -> SelectionSnapshot:
        self.calls.append(request)
        return self.selection


@dataclass(slots=True)
class SemanticInspectionAdapter(InspectionOnlyAdapter):
    model: DrawingModel = field(default_factory=lambda: _model())
    semantic_deadline: object | None = None

    def inspect_semantic_drawing(
        self,
        request: DrawingReadRequest,
        deadline: object | None = None,
    ) -> DrawingModel:
        assert request.source.ref == self.model.document_id
        self.semantic_deadline = deadline
        return self.model


def _document(*, entity_count: int = 2) -> DocumentSnapshot:
    return DocumentSnapshot(
        document_id=DOCUMENT_ID,
        revision=REVISION,
        path_hash="sha256:redacted",
        display_name="fixture.dwg",
        units=Unit.MM,
        active_space="model",
        entity_count=entity_count,
    )


def _selection() -> SelectionSnapshot:
    return SelectionSnapshot(
        document_id=DOCUMENT_ID,
        revision=REVISION,
        entities=(
            EntitySummary(entity_ref="acad:handle:A1", entity_type="AcDbLine", layer="PART"),
            EntitySummary(entity_ref="acad:handle:B2", entity_type="AcDbCircle", layer="HOLE"),
        ),
    )


def _model() -> DrawingModel:
    return DrawingModel(
        document_id=DOCUMENT_ID,
        revision=REVISION,
        display_name="fixture.dwg",
        source_unit_code="mm",
        to_mm_factor=1.0,
        geometry_normalized=True,
        scope=ReadScope(),
        entities=(
            EntityRecord(
                entity_ref="acad:handle:B2",
                entity_type="AcDbCircle",
                layer="HOLE",
                visible=True,
                space="model",
                geometry=CircleGeometry(center_mm=(5.0, 5.0), radius_mm=2.0),
                bounding_box_mm=(3.0, 3.0, 7.0, 7.0),
            ),
        ),
        arc_chord_tolerance_mm=0.01,
    )


def _request(scope: ReadScope | None) -> DrawingReadRequest:
    return DrawingReadRequest(
        source=DrawingSourceRef(kind="active_document", format="dwg", ref=DOCUMENT_ID),
        scope=scope,
        max_entities=10,
        max_block_nesting_depth=3,
    )


def test_com_reader_is_read_only_port_and_preserves_revision_and_state() -> None:
    selection = _selection()
    adapter = InspectionOnlyAdapter(_document(), selection)
    reader = ComDrawingReader(adapter)
    original_selection = selection.model_dump(mode="json")

    assert isinstance(reader, DrawingSourcePort)
    assert reader.current_revision(DOCUMENT_ID) == REVISION
    summary = reader.summarize(_request(ReadScope()))

    assert summary.revision == REVISION
    assert summary.counts_by_entity_type == {"com_unclassified_entity": 2}
    assert summary.counts_by_space == {"model": 2}
    assert summary.coverage_complete is False
    assert summary.unsupported[0].entity_type == "com_unclassified_entity"
    assert selection.model_dump(mode="json") == original_selection
    assert [type(call) for call in adapter.calls] == [InspectRequest, InspectRequest]


def test_com_selection_summary_keeps_stable_handles_and_exact_counts() -> None:
    selection = _selection()
    adapter = InspectionOnlyAdapter(_document(), selection)
    reader = ComDrawingReader(adapter)
    refs = tuple(entity.entity_ref for entity in selection.entities)

    summary = reader.summarize(_request(ReadScope(kind="selection", entity_refs=refs)))

    assert tuple(entity.entity_ref for entity in selection.entities) == refs
    assert summary.counts_by_entity_type == {"AcDbLine": 1, "AcDbCircle": 1}
    assert summary.counts_by_layer == {"PART": 1, "HOLE": 1}
    assert summary.counts_by_space == {"unknown": 2}
    assert summary.coverage_complete is False
    assert isinstance(adapter.calls[-1], SelectionRequest)
    assert adapter.calls[-1].max_entities == 10


def test_com_reader_refuses_to_invent_detailed_geometry() -> None:
    adapter = InspectionOnlyAdapter(_document(), _selection())
    reader = ComDrawingReader(adapter)

    with pytest.raises(AdapterCapabilityMissingError) as captured:
        reader.read(_request(ReadScope()))

    assert captured.value.details == {
        "adapter_type": "com",
        "missing_capability": "semantic_geometry_read",
        "available_inspection": ["document_metadata", "active_selection_summary"],
    }
    assert adapter.calls == []


def test_com_reader_delegates_bounded_semantic_model_without_mutation() -> None:
    adapter = SemanticInspectionAdapter(_document(), _selection())
    reader = ComDrawingReader(adapter)
    deadline = OperationDeadline(1.0, "read")

    model = reader.read_cancellable(_request(ReadScope()), deadline)

    assert model == adapter.model
    assert model.revision == REVISION
    assert model.entities[0].entity_ref == "acad:handle:B2"
    assert adapter.semantic_deadline is deadline
    assert adapter.calls == []


def test_com_reader_cancellable_methods_checkpoint_around_public_reads() -> None:
    adapter = InspectionOnlyAdapter(_document(entity_count=0), _selection())
    reader = ComDrawingReader(adapter)
    revision_deadline = OperationDeadline(1.0, "read")
    summary_deadline = OperationDeadline(1.0, "read")

    assert reader.current_revision_cancellable(DOCUMENT_ID, revision_deadline) == REVISION
    assert reader.summarize_cancellable(_request(None), summary_deadline).coverage_complete is True
    assert revision_deadline.cancelled is False
    assert summary_deadline.cancelled is False


def test_com_reader_rejects_unprovable_scope_or_changed_selection() -> None:
    adapter = InspectionOnlyAdapter(_document(), _selection())
    reader = ComDrawingReader(adapter)

    with pytest.raises(AdapterCapabilityMissingError) as scope_error:
        reader.summarize(_request(ReadScope(kind="layer", layer_name="PART")))
    assert scope_error.value.details["missing_capability"] == "semantic_layer_inspection"

    with pytest.raises(ComCallFailedError):
        reader.summarize(
            _request(ReadScope(kind="selection", entity_refs=("acad:handle:NOT-ACTIVE",)))
        )


def test_com_reader_rejects_file_sources_before_adapter_access() -> None:
    adapter = InspectionOnlyAdapter(_document(), _selection())
    reader = ComDrawingReader(adapter)
    request = _request(None).model_copy(
        update={"source": DrawingSourceRef(kind="file", format="dwg", ref="drawing.dwg")}
    )

    with pytest.raises(UnsupportedInputFormatError):
        reader.summarize(request)

    assert adapter.calls == []
