"""Read-only drawing-source facade over the COM adapter's public inspections."""

from __future__ import annotations

from collections import Counter
from typing import Protocol

from cad_harness.domain.errors import (
    AdapterCapabilityMissingError,
    ComCallFailedError,
    ReadScopeTooLargeError,
    UnsupportedInputFormatError,
)
from cad_harness.domain.models.document import DocumentSnapshot, SelectionSnapshot
from cad_harness.domain.models.drawing_model import (
    DrawingModel,
    DrawingSummary,
    UnsupportedEntityCount,
)
from cad_harness.domain.ports.autocad_adapter import InspectRequest, SelectionRequest
from cad_harness.domain.ports.drawing_source import DrawingReadRequest
from cad_harness.domain.ports.repositories import CancellationTokenPort

_SUPPORTED_FORMATS = frozenset({"dwg", "dxf"})
_UNCLASSIFIED_ENTITY = "com_unclassified_entity"
_SELECTION_SEMANTICS_GAP = "com_selection_geometry_or_space_unavailable"


class _ComInspectionPort(Protocol):
    def inspect_document(self, request: InspectRequest) -> DocumentSnapshot: ...

    def inspect_selection(self, request: SelectionRequest) -> SelectionSnapshot: ...


class ComDrawingReader:
    """Expose only semantics the current ActiveX inspection contract can prove.

    The COM adapter publishes document metadata and active-selection summaries, but
    not entity geometry or bounding boxes.  This facade therefore returns honest,
    incomplete summaries and refuses detailed models instead of manufacturing
    geometry.  It cannot write because its dependency protocol contains read methods
    only; COM imports remain confined to ``autocad_com.py``.
    """

    def __init__(self, adapter: _ComInspectionPort) -> None:
        self._adapter = adapter

    def current_revision(self, document_id: str) -> str:
        snapshot = self._snapshot(document_id)
        return snapshot.revision

    def current_revision_cancellable(
        self, document_id: str, deadline: CancellationTokenPort
    ) -> str:
        deadline.checkpoint()
        revision = self.current_revision(document_id)
        deadline.checkpoint()
        return revision

    def summarize(self, request: DrawingReadRequest) -> DrawingSummary:
        self._require_supported_source(request)
        snapshot = self._snapshot(request.source.ref)
        scope = request.scope
        if scope is not None and scope.kind == "selection":
            return self._selection_summary(request, snapshot)
        if scope is not None and scope.kind in {"layer", "layout"}:
            raise self._scope_gap(scope.kind)
        return self._coarse_model_space_summary(snapshot)

    def summarize_cancellable(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort
    ) -> DrawingSummary:
        deadline.checkpoint()
        summary = self.summarize(request)
        deadline.checkpoint()
        return summary

    def read(self, request: DrawingReadRequest) -> DrawingModel:
        self._require_supported_source(request)
        if request.scope is None:
            raise ValueError("Detailed reads require an explicit scope; use summarize otherwise")
        raise self._geometry_gap()

    def read_cancellable(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort
    ) -> DrawingModel:
        deadline.checkpoint()
        model = self.read(request)
        deadline.checkpoint()  # pragma: no cover - ``read`` currently always refuses
        return model

    def _snapshot(self, document_id: str) -> DocumentSnapshot:
        if not document_id.strip():
            raise ValueError("document_id must not be blank")
        snapshot = self._adapter.inspect_document(InspectRequest(document_id=document_id))
        if snapshot.document_id != document_id:
            raise ComCallFailedError(
                "COM inspection returned a different active document",
                required_action="Activate the requested drawing in AutoCAD and retry",
                details={"requested_document_id": document_id},
            )
        return snapshot

    def _selection_summary(
        self, request: DrawingReadRequest, document: DocumentSnapshot
    ) -> DrawingSummary:
        selection = self._adapter.inspect_selection(
            SelectionRequest(
                document_id=document.document_id,
                max_entities=request.max_entities,
            )
        )
        if selection.document_id != document.document_id or selection.revision != document.revision:
            raise ComCallFailedError(
                "COM selection changed during drawing inspection",
                required_action="Freeze the active selection and read the drawing again",
            )
        if selection.truncated:
            raise ReadScopeTooLargeError(
                "Active COM selection exceeds the approved entity budget",
                required_action="Select fewer entities and retry",
                details={"max_entities": request.max_entities},
            )
        requested_refs = set(request.scope.entity_refs) if request.scope is not None else set()
        actual_refs = {entity.entity_ref for entity in selection.entities}
        if requested_refs != actual_refs:
            raise ComCallFailedError(
                "Active COM selection does not match the requested stable handles",
                required_action="Reselect the requested entities and read again",
                details={
                    "requested_entity_count": len(requested_refs),
                    "active_entity_count": len(actual_refs),
                },
            )
        by_type = Counter(entity.entity_type for entity in selection.entities)
        by_layer = Counter(entity.layer for entity in selection.entities)
        count = len(selection.entities)
        unsupported = (
            (UnsupportedEntityCount(entity_type=_SELECTION_SEMANTICS_GAP, count=count),)
            if count
            else ()
        )
        return DrawingSummary(
            document_id=document.document_id,
            revision=document.revision,
            counts_by_entity_type=dict(by_type),
            counts_by_layer=dict(by_layer),
            counts_by_space={"unknown": count} if count else {},
            unsupported=unsupported,
            coverage_complete=not unsupported,
        )

    @staticmethod
    def _coarse_model_space_summary(snapshot: DocumentSnapshot) -> DrawingSummary:
        count = snapshot.entity_count
        unsupported = (
            (UnsupportedEntityCount(entity_type=_UNCLASSIFIED_ENTITY, count=count),)
            if count
            else ()
        )
        return DrawingSummary(
            document_id=snapshot.document_id,
            revision=snapshot.revision,
            counts_by_entity_type={_UNCLASSIFIED_ENTITY: count} if count else {},
            counts_by_layer={},
            counts_by_space={"model": count} if count else {},
            unsupported=unsupported,
            coverage_complete=not unsupported,
        )

    @staticmethod
    def _require_supported_source(request: DrawingReadRequest) -> None:
        source_format = request.source.format.strip().lower().lstrip(".")
        if request.source.kind != "active_document" or source_format not in _SUPPORTED_FORMATS:
            raise UnsupportedInputFormatError(
                "ComDrawingReader reads the active DWG or DXF document only",
                required_action="Open and activate a DWG or DXF drawing in AutoCAD",
                details={
                    "source_kind": request.source.kind,
                    "received_format": source_format,
                    "supported_formats": sorted(_SUPPORTED_FORMATS),
                },
            )
        if not request.source.ref.strip():
            raise ValueError("active document ref must not be blank")

    @staticmethod
    def _scope_gap(scope_kind: str) -> AdapterCapabilityMissingError:
        return AdapterCapabilityMissingError(
            f"COM inspection cannot enumerate the requested {scope_kind} scope",
            required_action="Use the .NET bridge or export a local DXF for semantic reading",
            details={
                "adapter_type": "com",
                "missing_capability": f"semantic_{scope_kind}_inspection",
            },
        )

    @staticmethod
    def _geometry_gap() -> AdapterCapabilityMissingError:
        return AdapterCapabilityMissingError(
            "COM inspection does not expose bounded semantic geometry",
            required_action="Use the .NET bridge or export a local DXF for semantic reading",
            details={
                "adapter_type": "com",
                "missing_capability": "semantic_geometry_read",
                "available_inspection": ["document_metadata", "active_selection_summary"],
            },
        )


__all__ = ["ComDrawingReader"]
