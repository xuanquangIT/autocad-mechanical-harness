"""Focused wire and port contract tests for ``BridgeDrawingReader``."""

from __future__ import annotations

from typing import Any

import pytest

from cad_harness.adapters.bridge_drawing_reader import BridgeDrawingReader
from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter
from cad_harness.application.timeout import OperationDeadline
from cad_harness.domain.errors import ReadScopeTooLargeError, UnsupportedInputFormatError
from cad_harness.domain.models.drawing_model import ReadScope
from cad_harness.domain.ports.drawing_source import (
    DrawingReadRequest,
    DrawingSourcePort,
    DrawingSourceRef,
)


class SemanticTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.timeouts: list[float] = []
        self.model_entities: list[dict[str, Any]] = [
            {
                "entity_ref": "acad:handle:1A",
                "entity_type": "AcDbLine",
                "layer": "CUT",
                "visible": True,
                "space": "model",
                "geometry": {"kind": "line", "start_mm": [0.0, 0.0], "end_mm": [5.0, 0.0]},
                "bounding_box_mm": [0.0, 0.0, 5.0, 0.0],
            }
        ]

    def request(self, envelope: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        self.timeouts.append(timeout_seconds)
        self.requests.append(envelope)
        method = envelope["method"]
        params = envelope["params"]
        if method == "handshake":
            data: dict[str, Any] = {
                "schema_version": "1.10",
                "capabilities": ["inspect_document"],
                "supported_operations": [],
            }
        elif params.get("response_contract") == "drawing_summary":
            data = {
                "schema_version": "1.10",
                "document_id": "doc_A",
                "revision": "sha256:r1",
                "counts_by_entity_type": {"AcDbLine": len(self.model_entities)},
                "counts_by_layer": {"CUT": len(self.model_entities)},
                "counts_by_space": {"model": len(self.model_entities)},
                "unsupported": [],
                "coverage_complete": True,
            }
        elif params.get("response_contract") == "drawing_model":
            data = {
                "schema_version": "1.10",
                "document_id": "doc_A",
                "revision": "sha256:r1",
                "display_name": "part.dwg",
                "source_unit_code": "mm",
                "to_mm_factor": 1.0,
                "geometry_normalized": True,
                "scope": params["scope"],
                "entities": self.model_entities,
                "layers": [],
                "dimension_styles": [],
                "text_styles": [],
                "unsupported": [],
                "coverage_complete": True,
                "arc_chord_tolerance_mm": 0.01,
            }
        else:
            data = {
                "schema_version": "1.10",
                "document_id": "doc_A",
                "revision": "sha256:r1",
                "path_hash": "sha256:path",
                "display_name": "part.dwg",
                "units": "mm",
            }
        return {
            "schema_version": "1.10",
            "request_id": envelope["request_id"],
            "status": "ok",
            "data": data,
        }


def _request(*, scope: ReadScope | None, max_entities: int = 10) -> DrawingReadRequest:
    return DrawingReadRequest(
        source=DrawingSourceRef(kind="active_document", format="DWG", ref="doc_A"),
        scope=scope,
        max_entities=max_entities,
        max_block_nesting_depth=3,
    )


def test_bridge_reader_satisfies_port_and_requests_typed_semantic_contracts() -> None:
    transport = SemanticTransport()
    reader = BridgeDrawingReader(DotNetBridgeAdapter(transport=transport, timeout_seconds=2.0))
    assert isinstance(reader, DrawingSourcePort)

    summary = reader.summarize(_request(scope=None))
    model = reader.read(_request(scope=ReadScope(kind="layer", layer_name="CUT")))
    revision = reader.current_revision("doc_A")

    assert summary.counts_by_entity_type == {"AcDbLine": 1}
    assert model.entities[0].geometry.kind == "line"
    assert revision == "sha256:r1"
    semantic_requests = [
        envelope for envelope in transport.requests if "response_contract" in envelope["params"]
    ]
    assert [item["params"]["response_contract"] for item in semantic_requests] == [
        "drawing_summary",
        "drawing_model",
    ]
    assert all(item["method"] == "inspect_document" for item in semantic_requests)
    assert all(item["params"]["source"]["ref"] == "doc_A" for item in semantic_requests)


def test_detailed_read_requires_scope_without_contacting_bridge() -> None:
    transport = SemanticTransport()
    reader = BridgeDrawingReader(DotNetBridgeAdapter(transport=transport, timeout_seconds=2.0))
    with pytest.raises(ValueError, match="explicit scope"):
        reader.read(_request(scope=None))
    assert transport.requests == []


def test_reader_rejects_unsupported_source_before_contacting_bridge() -> None:
    transport = SemanticTransport()
    reader = BridgeDrawingReader(DotNetBridgeAdapter(transport=transport, timeout_seconds=2.0))
    request = DrawingReadRequest(
        source=DrawingSourceRef(kind="file", format="pdf", ref="drawing.pdf"),
        scope=None,
        max_entities=10,
        max_block_nesting_depth=3,
    )
    with pytest.raises(UnsupportedInputFormatError) as caught:
        reader.summarize(request)
    assert caught.value.details["supported_formats"] == ["dwg", "dxf"]
    assert transport.requests == []


def test_reader_rejects_oversized_model_instead_of_returning_partial_geometry() -> None:
    transport = SemanticTransport()
    transport.model_entities *= 2
    reader = BridgeDrawingReader(DotNetBridgeAdapter(transport=transport, timeout_seconds=2.0))
    with pytest.raises(ReadScopeTooLargeError) as caught:
        reader.read(_request(scope=ReadScope(), max_entities=1))
    assert caught.value.details == {"entity_count": 2, "max_entities": 1}


def test_read_deadline_cannot_widen_the_bridge_ipc_timeout() -> None:
    transport = SemanticTransport()
    reader = BridgeDrawingReader(DotNetBridgeAdapter(transport=transport, timeout_seconds=0.5))

    reader.summarize_cancellable(
        _request(scope=None),
        OperationDeadline(10.0, "read"),
    )

    assert transport.timeouts == [0.5, 0.5]
