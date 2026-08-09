"""Application orchestration for local baseline, effort and pilot report data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from cad_harness.domain.models.metrics import (
    BaselineCase,
    CapabilityGroup,
    EffortRecord,
    FailureReason,
    PilotReport,
    WorkLabel,
)
from cad_harness.metrics.collector import (
    EngineerActivityInterval,
    MetricsCollector,
    PilotThresholds,
    normalize_manual_minutes,
)
from cad_harness.observability.audit import AuditEvent


class MetricsStore(Protocol):
    pilot_run_id: str

    def save_baseline_case(self, case: BaselineCase) -> None: ...

    def save_effort_record(self, record: EffortRecord) -> None: ...

    def baseline_cases(self) -> tuple[BaselineCase, ...]: ...

    def effort_records(self) -> tuple[EffortRecord, ...]: ...

    def operation_samples_ms(self) -> dict[str, tuple[float, ...]]: ...


class AuditEventSource(Protocol):
    def events_for_job(self, job_id: str) -> tuple[AuditEvent, ...]: ...


class MetricsService:
    """Persist safe numeric pilot evidence and reproduce aggregate reports."""

    def __init__(
        self,
        *,
        thresholds: PilotThresholds,
        store: MetricsStore,
        audit_events: AuditEventSource,
    ) -> None:
        self._thresholds = thresholds
        self._store = store
        self._audit_events = audit_events
        self._collector = MetricsCollector(thresholds)

    def record_baseline(
        self,
        *,
        case_id: str,
        capability_group: CapabilityGroup,
        work_label: WorkLabel,
        raw_manual_minutes: float,
        measured_by: str,
        biased: bool,
        measured_in_single_session: bool,
    ) -> BaselineCase:
        case = BaselineCase(
            pilot_run_id=self._store.pilot_run_id,
            case_id=case_id,
            capability_group=capability_group,
            work_label=work_label,
            manual_minutes=normalize_manual_minutes(raw_manual_minutes, self._thresholds),
            manual_measured_by=measured_by,
            manual_measurement_biased=biased,
            manual_measured_in_single_session=measured_in_single_session,
        )
        self._store.save_baseline_case(case)
        return case

    def collect_effort(
        self,
        *,
        record_id: str,
        case_id: str,
        job_id: str,
        engineer_activity: Sequence[EngineerActivityInterval] = (),
        manual_fixup_minutes: float = 0.0,
        entities_manually_edited: int = 0,
    ) -> EffortRecord:
        if case_id not in {case.case_id for case in self._store.baseline_cases()}:
            raise ValueError("Pilot effort requires a baseline case in the same pilot run")
        record = self._collector.effort_from_events(
            record_id=record_id,
            pilot_run_id=self._store.pilot_run_id,
            case_id=case_id,
            job_id=job_id,
            events=self._audit_events.events_for_job(job_id),
            engineer_activity=engineer_activity,
            manual_fixup_minutes=manual_fixup_minutes,
            entities_manually_edited=entities_manually_edited,
        )
        self._store.save_effort_record(record)
        return record

    def collect_failed_effort(
        self,
        *,
        record_id: str,
        case_id: str,
        job_id: str,
        failure_reason: FailureReason,
        engineer_activity: Sequence[EngineerActivityInterval] = (),
        manual_fixup_minutes: float = 0.0,
        entities_manually_edited: int = 0,
    ) -> EffortRecord:
        """Append an explicitly classified unsupported or failed pilot attempt."""
        if failure_reason is FailureReason.MISSING_EFFORT_RECORD:
            raise ValueError("missing_effort_record is generated only by report aggregation")
        if case_id not in {case.case_id for case in self._store.baseline_cases()}:
            raise ValueError("Pilot effort requires a baseline case in the same pilot run")
        record = self._collector.effort_from_events(
            record_id=record_id,
            pilot_run_id=self._store.pilot_run_id,
            case_id=case_id,
            job_id=job_id,
            events=self._audit_events.events_for_job(job_id),
            engineer_activity=engineer_activity,
            manual_fixup_minutes=manual_fixup_minutes,
            entities_manually_edited=entities_manually_edited,
            failure_reason=failure_reason,
        )
        self._store.save_effort_record(record)
        return record

    def build_report(
        self,
        *,
        report_id: str,
        failure_reasons: Mapping[str, FailureReason] | None = None,
    ) -> PilotReport:
        return self._collector.aggregate(
            report_id=report_id,
            pilot_run_id=self._store.pilot_run_id,
            baseline=self._store.baseline_cases(),
            efforts=self._store.effort_records(),
            failure_reasons=failure_reasons,
            operation_samples_ms=self._store.operation_samples_ms(),
        )


__all__ = ["AuditEventSource", "MetricsService", "MetricsStore"]
