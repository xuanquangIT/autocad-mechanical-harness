"""Measured, local-only pilot aggregation."""

from cad_harness.metrics.collector import (
    MetricsCollector,
    PerformanceThresholds,
    PilotThresholds,
    calculate_saving,
    calculate_statistics,
    is_valid_baseline,
    load_pilot_thresholds,
    normalize_manual_minutes,
)
from cad_harness.metrics.recorder import OperationMeasurement, OperationMetricsRecorder

__all__ = [
    "MetricsCollector",
    "OperationMeasurement",
    "OperationMetricsRecorder",
    "PerformanceThresholds",
    "PilotThresholds",
    "calculate_saving",
    "calculate_statistics",
    "is_valid_baseline",
    "load_pilot_thresholds",
    "normalize_manual_minutes",
]
