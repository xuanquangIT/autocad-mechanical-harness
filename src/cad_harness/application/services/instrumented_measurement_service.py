"""Application wrapper that times the otherwise pure MeasurementService."""

from __future__ import annotations

from cad_harness.application.services.measurement_service import MeasurementService
from cad_harness.application.timeout import OperationDeadline, run_cancellable
from cad_harness.config import Settings
from cad_harness.domain.models.drawing_model import DrawingModel
from cad_harness.domain.models.measurement import MeasurementRequest, MeasurementResult
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.metrics.recorder import OperationMetricsRecorder


class InstrumentedMeasurementService:
    def __init__(
        self,
        service: MeasurementService,
        operation_metrics: OperationMetricsRecorder,
        settings: Settings | None = None,
    ) -> None:
        self._service = service
        self._operation_metrics = operation_metrics
        self._timeout_seconds = settings.measure.timeout_seconds if settings is not None else 1.0

    def measure(
        self,
        model: DrawingModel,
        request: MeasurementRequest,
        *,
        tolerance: ToleranceProfile,
    ) -> MeasurementResult:
        with self._operation_metrics.measure("measure") as metric:
            deadline = OperationDeadline(self._timeout_seconds, "measure")
            result = run_cancellable(
                deadline,
                lambda token: self._measure(model, request, tolerance=tolerance, deadline=token),
            )
            metric.entity_count = len(request.entity_refs)
            return result

    def _measure(
        self,
        model: DrawingModel,
        request: MeasurementRequest,
        *,
        tolerance: ToleranceProfile,
        deadline: OperationDeadline,
    ) -> MeasurementResult:
        deadline.checkpoint()
        result = self._service.measure_cancellable(
            model,
            request,
            tolerance=tolerance,
            deadline=deadline,
        )
        deadline.checkpoint()
        return result


__all__ = ["InstrumentedMeasurementService"]
