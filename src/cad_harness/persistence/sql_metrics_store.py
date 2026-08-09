"""Local SQLite persistence for privacy-safe pilot inputs and operation timings."""

from __future__ import annotations

from math import isfinite

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cad_harness.domain.models.metrics import BaselineCase, EffortRecord, FailureReason
from cad_harness.metrics.collector import OPERATION_NAMES
from cad_harness.persistence.models import BaselineCaseRow, EffortRecordRow, OperationMetricRow
from cad_harness.persistence.retry import DEFAULT_SQLITE_RETRY, RetryPolicy


class SqlMetricsStore:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        pilot_run_id: str = "pilot_default",
        retry: RetryPolicy = DEFAULT_SQLITE_RETRY,
    ) -> None:
        self._session_factory = session_factory
        self.pilot_run_id = pilot_run_id
        self._retry = retry

    def save_baseline_case(self, case: BaselineCase) -> None:
        """Append one engineer-measured baseline without prompt or geometry data."""
        if case.pilot_run_id != self.pilot_run_id:
            raise ValueError("Baseline case belongs to a different pilot run")

        def attempt() -> None:
            with self._session_factory() as session:
                session.add(
                    BaselineCaseRow(
                        baseline_record_id=f"{case.pilot_run_id}:{case.case_id}",
                        case_id=case.case_id,
                        pilot_run_id=case.pilot_run_id,
                        capability_group=case.capability_group,
                        work_label=case.work_label,
                        manual_minutes=case.manual_minutes,
                        manual_measured_by=case.manual_measured_by,
                        manual_measurement_biased=case.manual_measurement_biased,
                        manual_measured_in_single_session=case.manual_measured_in_single_session,
                    )
                )
                session.commit()

        self._retry.run(attempt)

    def save_effort_record(self, record: EffortRecord) -> None:
        """Append one derived effort record; the job foreign key must already exist."""
        if record.record_id is None:
            raise ValueError("Persisted effort records require record_id")
        if record.pilot_run_id != self.pilot_run_id:
            raise ValueError("Effort record belongs to a different pilot run")

        def attempt() -> None:
            with self._session_factory() as session:
                session.add(
                    EffortRecordRow(
                        record_id=record.record_id,
                        pilot_run_id=record.pilot_run_id,
                        case_id=record.case_id,
                        job_id=record.job_id,
                        harness_minutes=record.harness_minutes,
                        idle_minutes_excluded=record.idle_minutes_excluded,
                        manual_fixup_minutes=record.manual_fixup_minutes,
                        spec_change_count=record.spec_change_count,
                        entities_created=record.entities_created,
                        entities_manually_edited=record.entities_manually_edited,
                        first_preview_clean=record.first_preview_clean,
                        completed=record.completed,
                        failure_reason=(
                            record.failure_reason.value
                            if record.failure_reason is not None
                            else None
                        ),
                    )
                )
                session.commit()

        self._retry.run(attempt)

    def record_operation(
        self,
        *,
        metric_id: str,
        operation_name: str,
        duration_ms: float,
        entity_count: int,
    ) -> None:
        if operation_name not in OPERATION_NAMES:
            raise ValueError(f"Unsupported operation metric: {operation_name}")
        if not isfinite(duration_ms) or duration_ms < 0.0 or entity_count < 0:
            raise ValueError("Operation duration and entity count must be non-negative")

        def attempt() -> None:
            with self._session_factory() as session:
                session.add(
                    OperationMetricRow(
                        metric_id=metric_id,
                        pilot_run_id=self.pilot_run_id,
                        operation_name=operation_name,
                        duration_ms=duration_ms,
                        entity_count=entity_count,
                    )
                )
                session.commit()

        self._retry.run(attempt)

    def baseline_cases(self) -> tuple[BaselineCase, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(BaselineCaseRow)
                .where(BaselineCaseRow.pilot_run_id == self.pilot_run_id)
                .order_by(BaselineCaseRow.case_id)
            ).all()
            return tuple(
                BaselineCase.model_validate(
                    {
                        "case_id": row.case_id,
                        "pilot_run_id": row.pilot_run_id,
                        "capability_group": row.capability_group,
                        "work_label": row.work_label,
                        "manual_minutes": row.manual_minutes,
                        "manual_measured_by": row.manual_measured_by,
                        "manual_measurement_biased": row.manual_measurement_biased,
                        "manual_measured_in_single_session": row.manual_measured_in_single_session,
                    }
                )
                for row in rows
            )

    def effort_records(self) -> tuple[EffortRecord, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(EffortRecordRow)
                .where(EffortRecordRow.pilot_run_id == self.pilot_run_id)
                .order_by(EffortRecordRow.record_id)
            ).all()
            return tuple(
                EffortRecord(
                    record_id=row.record_id,
                    pilot_run_id=row.pilot_run_id,
                    case_id=row.case_id,
                    job_id=row.job_id,
                    harness_minutes=row.harness_minutes,
                    idle_minutes_excluded=row.idle_minutes_excluded,
                    manual_fixup_minutes=row.manual_fixup_minutes,
                    spec_change_count=row.spec_change_count,
                    entities_created=row.entities_created,
                    entities_manually_edited=row.entities_manually_edited,
                    first_preview_clean=row.first_preview_clean,
                    completed=row.completed,
                    failure_reason=(
                        FailureReason(row.failure_reason)
                        if row.failure_reason is not None
                        else None
                    ),
                )
                for row in rows
            )

    def operation_samples_ms(self) -> dict[str, tuple[float, ...]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(OperationMetricRow)
                .order_by(OperationMetricRow.operation_name, OperationMetricRow.created_at)
                .where(OperationMetricRow.pilot_run_id == self.pilot_run_id)
            ).all()
            samples: dict[str, list[float]] = {name: [] for name in OPERATION_NAMES}
            for row in rows:
                samples[row.operation_name].append(row.duration_ms)
            return {name: tuple(values) for name, values in samples.items()}


__all__ = ["SqlMetricsStore"]
