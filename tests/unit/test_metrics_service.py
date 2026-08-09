"""Focused examples for pilot collection, operation timing and desktop click markers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from apps.engineer_desktop.effort_session import EngineerEffortSession

from cad_harness.domain.models.metrics import FailureReason
from cad_harness.metrics.collector import MetricsCollector, load_pilot_thresholds
from cad_harness.metrics.recorder import OperationMetricsRecorder
from cad_harness.observability.audit import AuditEvent, AuditEventType


def _event(
    event_type: AuditEventType,
    minute: float,
    *,
    payload: dict[str, object] | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=f"event-{minute}-{event_type.value}",
        event_type=event_type.value,
        job_id="job-metrics",
        actor_type="system",
        actor_id="harness",
        payload=payload or {},
        created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute),
        previous_event_hash=None,
        event_hash=f"hash-{minute}",
    )


def test_effort_record_is_derived_from_audit_and_explicit_engineer_inputs() -> None:
    events = (
        _event(AuditEventType.JOB_CREATED, 0.0),
        _event(AuditEventType.PREVIEW_GENERATED, 1.0),
        _event(
            AuditEventType.VALIDATION_COMPLETED,
            2.0,
            payload={"blocking": 0, "errors": 0},
        ),
        _event(AuditEventType.SPEC_CHANGED, 3.0),
        _event(AuditEventType.COMMIT_SUCCEEDED, 4.0, payload={"entities": 7}),
    )
    record = MetricsCollector(load_pilot_thresholds()).effort_from_events(
        record_id="effort-1",
        case_id="case-1",
        job_id="job-metrics",
        events=events,
        manual_fixup_minutes=1.2,
        entities_manually_edited=2,
    )
    assert record.harness_minutes == 5.2
    assert record.spec_change_count == 1
    assert record.first_preview_clean
    assert record.entities_created == 7
    assert record.entities_manually_edited == 2
    assert record.completed


def test_validation_after_a_second_preview_does_not_relabel_first_preview_clean() -> None:
    events = (
        _event(AuditEventType.JOB_CREATED, 0.0),
        _event(AuditEventType.PREVIEW_GENERATED, 1.0),
        _event(AuditEventType.PREVIEW_GENERATED, 2.0),
        _event(
            AuditEventType.VALIDATION_COMPLETED,
            3.0,
            payload={"blocking": 0, "errors": 0},
        ),
    )
    record = MetricsCollector(load_pilot_thresholds()).effort_from_events(
        record_id="effort-preview",
        case_id="case-preview",
        job_id="job-metrics",
        events=events,
        failure_reason=FailureReason.WORKFLOW_OVERHEAD,
    )
    assert not record.first_preview_clean


def test_desktop_effort_session_requires_explicit_same_session_markers() -> None:
    moments = iter(
        (
            datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 9, 3, tzinfo=UTC),
        )
    )
    session = EngineerEffortSession(clock=lambda: next(moments))
    with pytest.raises(ValueError):
        session.stop_activity()
    session.start_activity()
    interval = session.stop_activity()
    session.add_manual_fixup(1.25)
    assert interval.ended_at - interval.started_at == timedelta(minutes=3)
    assert session.intervals == (interval,)
    assert session.manual_fixup_minutes == 1.3


def test_operation_recorder_uses_monotonic_duration_and_records_failures() -> None:
    class Store:
        def __init__(self) -> None:
            self.rows: list[dict[str, object]] = []

        def record_operation(self, **values: object) -> None:
            self.rows.append(values)

    ticks = iter((1_000_000_000, 1_250_000_000))
    store = Store()
    recorder = OperationMetricsRecorder(store, clock_ns=lambda: next(ticks))
    with pytest.raises(RuntimeError), recorder.measure("read") as measurement:
        measurement.entity_count = 12
        raise RuntimeError("injected")
    assert store.rows[0]["operation_name"] == "read"
    assert store.rows[0]["duration_ms"] == 250.0
    assert store.rows[0]["entity_count"] == 12


def test_metric_store_failure_never_masks_primary_operation_outcome() -> None:
    class BrokenStore:
        def record_operation(self, **_values: object) -> None:
            raise OSError("disk full")

    ticks = iter((0, 1_000_000))
    recorder = OperationMetricsRecorder(BrokenStore(), clock_ns=lambda: next(ticks))
    with recorder.measure("compile"):
        pass

    ticks = iter((0, 1_000_000))
    recorder = OperationMetricsRecorder(BrokenStore(), clock_ns=lambda: next(ticks))
    with pytest.raises(RuntimeError, match="primary"), recorder.measure("commit"):
        raise RuntimeError("primary")
