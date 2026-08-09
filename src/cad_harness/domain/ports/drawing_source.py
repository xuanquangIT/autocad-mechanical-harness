"""Read-only drawing source port; mutation is absent by construction."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from cad_harness.domain.models.base import ContractModel
from cad_harness.domain.models.drawing_model import DrawingModel, DrawingSummary, ReadScope
from cad_harness.domain.ports.repositories import CancellationTokenPort


class DrawingSourceRef(ContractModel):
    """Opaque source locator interpreted only by an infrastructure adapter."""

    kind: Literal["active_document", "file"]
    format: str
    ref: str


class DrawingReadRequest(ContractModel):
    source: DrawingSourceRef
    scope: ReadScope | None = None
    max_entities: int = Field(ge=1)
    max_block_nesting_depth: int = Field(ge=1, le=10)
    include_geometry: bool = True


@runtime_checkable
class DrawingSourcePort(Protocol):
    """Only observation methods are intentionally available on this port."""

    def read(self, request: DrawingReadRequest) -> DrawingModel: ...

    def summarize(self, request: DrawingReadRequest) -> DrawingSummary: ...

    def current_revision(self, document_id: str) -> str: ...

    def read_cancellable(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort
    ) -> DrawingModel: ...

    def summarize_cancellable(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort
    ) -> DrawingSummary: ...

    def current_revision_cancellable(
        self, document_id: str, deadline: CancellationTokenPort
    ) -> str: ...


__all__ = [
    "DrawingReadRequest",
    "DrawingSourcePort",
    "DrawingSourceRef",
    "ReadScope",
]
