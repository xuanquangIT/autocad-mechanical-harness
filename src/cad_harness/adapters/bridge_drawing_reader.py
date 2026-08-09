"""Read-only :class:`DrawingSourcePort` backed by the local C# bridge."""

from __future__ import annotations

from typing import Any

from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter
from cad_harness.domain.errors import (
    ComCallFailedError,
    ReadScopeTooLargeError,
    UnsupportedInputFormatError,
)
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.document import DocumentSnapshot
from cad_harness.domain.models.drawing_model import DrawingModel, DrawingSummary
from cad_harness.domain.ports.autocad_adapter import AdapterCapability, InspectRequest
from cad_harness.domain.ports.drawing_source import DrawingReadRequest
from cad_harness.domain.ports.repositories import CancellationTokenPort

_SUPPORTED_FORMATS = frozenset({"dwg", "dxf"})


class BridgeDrawingReader:
    """Extract the semantic drawing contracts without exposing a mutation API.

    ``inspect_document`` is also the bridge's bounded semantic-read endpoint.  The
    ``response_contract`` discriminator keeps the ordinary adapter snapshot and the
    two drawing-source responses monomorphic on the wire.
    """

    def __init__(self, adapter: DotNetBridgeAdapter) -> None:
        self._adapter = adapter

    def current_revision(self, document_id: str) -> str:
        if not document_id.strip():
            raise ValueError("document_id must not be blank")
        return self._adapter.inspect_document(InspectRequest(document_id=document_id)).revision

    def current_revision_cancellable(
        self, document_id: str, deadline: CancellationTokenPort
    ) -> str:
        if not document_id.strip():
            raise ValueError("document_id must not be blank")
        deadline.checkpoint()
        timeout = self._remaining_timeout(deadline)
        self._adapter._ensure_handshake(timeout_seconds=timeout)
        self._adapter.require(AdapterCapability.INSPECT_DOCUMENT)
        data = self._adapter._request(
            "inspect_document",
            InspectRequest(document_id=document_id).model_dump(mode="json"),
            timeout_seconds=self._remaining_timeout(deadline),
        )
        result = self._adapter._model(DocumentSnapshot, data)
        deadline.checkpoint()
        return result.revision

    def summarize(self, request: DrawingReadRequest) -> DrawingSummary:
        data = self._semantic_request(request, response_contract="drawing_summary", deadline=None)
        summary = self._adapter._model(DrawingSummary, data)
        self._require_matching_response(request, summary.document_id, summary.schema_version)
        return summary

    def summarize_cancellable(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort
    ) -> DrawingSummary:
        data = self._semantic_request(
            request, response_contract="drawing_summary", deadline=deadline
        )
        summary = self._adapter._model(DrawingSummary, data)
        self._require_matching_response(request, summary.document_id, summary.schema_version)
        return summary

    def read(self, request: DrawingReadRequest) -> DrawingModel:
        if request.scope is None:
            raise ValueError("Detailed reads require an explicit scope; use summarize otherwise")
        data = self._semantic_request(request, response_contract="drawing_model", deadline=None)
        model = self._adapter._model(DrawingModel, data)
        self._require_matching_response(request, model.document_id, model.schema_version)
        if model.scope != request.scope:
            raise ComCallFailedError(
                "Bridge returned a drawing model for a different read scope",
                required_action="Install a bridge version matching the Python contract",
            )
        if len(model.entities) > request.max_entities:
            raise ReadScopeTooLargeError(
                "Bridge returned more entities than the approved scope budget",
                required_action="Narrow the requested layer, selection, or layout scope",
                details={
                    "entity_count": len(model.entities),
                    "max_entities": request.max_entities,
                },
            )
        return model

    def read_cancellable(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort
    ) -> DrawingModel:
        if request.scope is None:
            raise ValueError("Detailed reads require an explicit scope; use summarize otherwise")
        data = self._semantic_request(request, response_contract="drawing_model", deadline=deadline)
        model = self._adapter._model(DrawingModel, data)
        self._require_matching_response(request, model.document_id, model.schema_version)
        if model.scope != request.scope:
            raise ComCallFailedError(
                "Bridge returned a drawing model for a different read scope",
                required_action="Install a bridge version matching the Python contract",
            )
        if len(model.entities) > request.max_entities:
            raise ReadScopeTooLargeError(
                "Bridge returned more entities than the approved scope budget",
                required_action="Narrow the requested layer, selection, or layout scope",
                details={"entity_count": len(model.entities), "max_entities": request.max_entities},
            )
        deadline.checkpoint()
        return model

    def _semantic_request(
        self,
        request: DrawingReadRequest,
        *,
        response_contract: str,
        deadline: CancellationTokenPort | None,
    ) -> dict[str, Any]:
        self._require_supported_source(request)
        timeout = None if deadline is None else self._remaining_timeout(deadline)
        self._adapter.handshake(timeout_seconds=timeout)
        self._adapter.require(AdapterCapability.INSPECT_DOCUMENT)
        params = request.model_dump(mode="json")
        params["response_contract"] = response_contract
        return self._adapter._request(
            "inspect_document",
            params,
            timeout_seconds=(None if deadline is None else self._remaining_timeout(deadline)),
        )

    def _remaining_timeout(self, deadline: CancellationTokenPort) -> float:
        deadline.checkpoint()
        # Transport requires a positive timeout. At exact equality, a zero-work call is
        # still permitted by the strict elapsed > limit contract.
        return max(
            min(deadline.remaining_seconds, self._adapter.timeout_seconds),
            1.0e-9,
        )

    @staticmethod
    def _require_supported_source(request: DrawingReadRequest) -> None:
        source_format = request.source.format.strip().lower().lstrip(".")
        if request.source.kind != "active_document" or source_format not in _SUPPORTED_FORMATS:
            raise UnsupportedInputFormatError(
                "BridgeDrawingReader reads an active DWG or DXF document only",
                required_action=(
                    "Open a DWG or DXF document in AutoCAD and select it by document id"
                ),
                details={
                    "source_kind": request.source.kind,
                    "received_format": source_format,
                    "supported_formats": sorted(_SUPPORTED_FORMATS),
                },
            )
        if not request.source.ref.strip():
            raise ValueError("active document ref must not be blank")

    @staticmethod
    def _require_matching_response(
        request: DrawingReadRequest,
        document_id: str,
        schema_version: str,
    ) -> None:
        if schema_version != SCHEMA_VERSION or document_id != request.source.ref:
            raise ComCallFailedError(
                "Bridge semantic read did not match the requested document and schema",
                required_action="Install a bridge version matching the Python contract",
            )


__all__ = ["BridgeDrawingReader"]
