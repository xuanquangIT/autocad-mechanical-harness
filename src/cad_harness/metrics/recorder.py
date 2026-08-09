"""Monotonic operation timing with a local, privacy-safe persistence boundary."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from logging import getLogger
from time import monotonic_ns
from typing import Protocol
from uuid import uuid4

from cad_harness.metrics.collector import OPERATION_NAMES, OperationName

_LOGGER = getLogger(__name__)


class OperationMetricStore(Protocol):
    def record_operation(
        self,
        *,
        metric_id: str,
        operation_name: str,
        duration_ms: float,
        entity_count: int,
    ) -> None: ...


@dataclass(slots=True)
class OperationMeasurement:
    entity_count: int = 0


class OperationMetricsRecorder:
    """Record one end-to-end sample for each public operation, including failures."""

    def __init__(
        self,
        store: OperationMetricStore,
        *,
        clock_ns: Callable[[], int] = monotonic_ns,
    ) -> None:
        self._store = store
        self._clock_ns = clock_ns

    @contextmanager
    def measure(self, operation_name: OperationName) -> Iterator[OperationMeasurement]:
        if operation_name not in OPERATION_NAMES:  # pragma: no cover - type guard at runtime
            raise ValueError(f"Unsupported operation metric: {operation_name}")
        measurement = OperationMeasurement()
        started = self._clock_ns()
        try:
            yield measurement
        finally:
            elapsed_ms = max(0.0, (self._clock_ns() - started) / 1_000_000.0)
            try:
                self._store.record_operation(
                    metric_id=f"metric_{uuid4().hex}",
                    operation_name=operation_name,
                    duration_ms=elapsed_ms,
                    entity_count=max(0, measurement.entity_count),
                )
            except Exception:
                # Observability must not replace a successful CAD result or mask the
                # primary exception. Pilot acceptance will fail later on missing data.
                _LOGGER.warning(
                    "operation_metric_write_failed",
                    extra={"operation_name": operation_name},
                    exc_info=True,
                )


__all__ = [
    "OperationMeasurement",
    "OperationMetricStore",
    "OperationMetricsRecorder",
]
