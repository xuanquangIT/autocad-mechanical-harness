"""Application policy for bounded, revision-consistent drawing reads."""

from __future__ import annotations

from contextlib import nullcontext

from cad_harness.application.timeout import OperationDeadline, run_cancellable
from cad_harness.config import Settings
from cad_harness.domain.errors import (
    ReadScopeTooLargeError,
    StaleDocumentRevisionError,
    UnsupportedInputFormatError,
)
from cad_harness.domain.models.drawing_model import DrawingModel, DrawingSummary
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourcePort
from cad_harness.metrics.recorder import OperationMeasurement, OperationMetricsRecorder

SUPPORTED_DRAWING_FORMATS = frozenset({"dwg", "dxf"})


class DrawingReadService:
    def __init__(
        self,
        settings: Settings,
        source: DrawingSourcePort,
        operation_metrics: OperationMetricsRecorder | None = None,
    ) -> None:
        self._settings = settings
        self._source = source
        self._operation_metrics = operation_metrics

    def read(self, request: DrawingReadRequest) -> DrawingModel | DrawingSummary:
        measurement = (
            self._operation_metrics.measure("read")
            if self._operation_metrics is not None
            else nullcontext(OperationMeasurement())
        )
        with measurement as metric:
            deadline = OperationDeadline(self._settings.read.read_timeout_seconds, "read")
            result = run_cancellable(deadline, lambda token: self._read(request, token))
            metric.entity_count = (
                len(result.entities)
                if isinstance(result, DrawingModel)
                else sum(result.counts_by_entity_type.values())
            )
            return result

    def _read(
        self, request: DrawingReadRequest, deadline: OperationDeadline
    ) -> DrawingModel | DrawingSummary:
        """Read atomically from the caller's perspective; partial geometry never escapes."""
        source_format = request.source.format.strip().lower().lstrip(".")
        if source_format not in SUPPORTED_DRAWING_FORMATS:
            raise UnsupportedInputFormatError(
                f"Unsupported drawing format: {source_format or '<empty>'}",
                details={"supported_formats": sorted(SUPPORTED_DRAWING_FORMATS)},
            )

        deadline.checkpoint()
        revision_before = self._current_revision(request.source.ref, deadline)
        deadline.checkpoint()
        effective_request = request.model_copy(
            update={
                "max_block_nesting_depth": min(
                    request.max_block_nesting_depth,
                    self._settings.read.max_block_nesting_depth,
                )
            }
        )
        summary_request = effective_request.model_copy(update={"include_geometry": False})
        summary = self._source.summarize_cancellable(summary_request, deadline)
        deadline.checkpoint()
        self._require_payload_revision(summary.revision, revision_before)
        limit = min(request.max_entities, self._settings.read.max_entities)
        entity_count = sum(summary.counts_by_entity_type.values())
        if entity_count > limit:
            raise ReadScopeTooLargeError(
                "Requested drawing scope exceeds the configured entity limit",
                details={"entity_count": entity_count, "max_entities": limit},
            )

        if request.scope is None:
            self._require_unchanged(request.source.ref, revision_before, deadline)
            deadline.checkpoint()
            return summary

        model = self._source.read_cancellable(effective_request, deadline)
        deadline.checkpoint()
        self._require_payload_revision(model.revision, revision_before)
        if len(model.entities) > limit:
            raise ReadScopeTooLargeError(
                "Drawing source returned more entities than the approved scope budget",
                details={"entity_count": len(model.entities), "max_entities": limit},
            )
        self._require_unchanged(request.source.ref, revision_before, deadline)
        deadline.checkpoint()
        normalized = model.to_mm_factor is not None
        if model.geometry_normalized is not normalized:
            model = model.model_copy(update={"geometry_normalized": normalized})
        return model

    @staticmethod
    def _require_payload_revision(actual_revision: str, expected_revision: str) -> None:
        if actual_revision != expected_revision:
            raise StaleDocumentRevisionError(
                "Drawing source returned data from an unexpected revision",
                required_action="Read the drawing again from a stable revision",
                details={
                    "expected_revision": expected_revision,
                    "actual_revision": actual_revision,
                },
            )

    def _require_unchanged(
        self,
        document_id: str,
        expected_revision: str,
        deadline: OperationDeadline,
    ) -> None:
        actual_revision = self._current_revision(document_id, deadline)
        if actual_revision != expected_revision:
            raise StaleDocumentRevisionError(
                "Drawing changed while it was being read",
                required_action="Read the drawing again from a stable revision",
                details={
                    "expected_revision": expected_revision,
                    "actual_revision": actual_revision,
                },
            )

    def _current_revision(self, document_id: str, deadline: OperationDeadline | None) -> str:
        if deadline is not None:
            return self._source.current_revision_cancellable(document_id, deadline)
        return self._source.current_revision(document_id)
