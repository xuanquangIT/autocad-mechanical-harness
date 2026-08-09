"""Pure effectiveness calculations sourced from audit events and local counters."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from math import isfinite
from pathlib import Path
from statistics import median
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cad_harness.domain.models.metrics import (
    BaselineCase,
    CapabilityGroup,
    EffortRecord,
    FailureReason,
    Metric,
    OperationMetricSummary,
    PilotCaseResult,
    PilotReport,
    WorkLabel,
    WorkLabelSummary,
    round_minutes,
    round_saving,
)
from cad_harness.observability.audit import AuditEvent, AuditEventType

type OperationName = Literal["compile", "preview", "commit", "read", "takeoff", "measure"]

OPERATION_NAMES: tuple[OperationName, ...] = (
    "compile",
    "preview",
    "commit",
    "read",
    "takeoff",
    "measure",
)
CAPABILITY_GROUPS: tuple[CapabilityGroup, ...] = ("B", "D", "E")
WORK_LABELS: tuple[WorkLabel, ...] = ("ve_moi", "sua_ban_co_san")


@dataclass(frozen=True, slots=True)
class EngineerActivityInterval:
    """A local click-marked interval; it carries timestamps only, never drawing data."""

    started_at: datetime
    ended_at: datetime

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None or self.ended_at.tzinfo is None:
            raise ValueError("Engineer activity timestamps must be timezone-aware")
        if self.ended_at < self.started_at:
            raise ValueError("Engineer activity cannot end before it starts")


class PerformanceThresholds(BaseModel):
    """Requirement 26 budgets, kept in policy rather than benchmark code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    compile_p95_seconds: float = Field(gt=0.0)
    preview_p95_seconds: float = Field(gt=0.0)
    commit_p95_seconds: float = Field(gt=0.0)
    read_p95_seconds: float = Field(gt=0.0)
    takeoff_p95_seconds: float = Field(gt=0.0)
    measure_p95_seconds: float = Field(gt=0.0)
    max_autocad_command_block_seconds: float = Field(gt=0.0)


class PilotThresholds(BaseModel):
    """Acceptance policy loaded from ``config/pilot.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_baseline_cases: int = Field(ge=1)
    minimum_cases_per_group: int = Field(ge=1)
    minimum_manual_minutes: float = Field(gt=0.0)
    idle_gap_minutes: float = Field(gt=0.0)
    minimum_metric_samples: int = Field(ge=1)
    overall_median_saving: float
    group_median_saving: float
    minimum_case_saving: float
    minimum_first_preview_clean_rate: float = Field(ge=0.0, le=1.0)
    maximum_median_spec_changes: float = Field(ge=0.0)
    maximum_manual_entity_edit_rate: float = Field(ge=0.0, le=1.0)
    minimum_committed_job_rate: float = Field(ge=0.0, le=1.0)
    performance: PerformanceThresholds


def load_pilot_thresholds(path: Path = Path("config/pilot.yaml")) -> PilotThresholds:
    """Load the explicit pilot policy; a missing or malformed file fails closed."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return PilotThresholds.model_validate(data)


def is_valid_baseline(cases: Sequence[BaselineCase], thresholds: PilotThresholds) -> bool:
    """Return the exact deterministic predicate from Requirement 24.1/24.2/24.14."""
    if len(cases) < thresholds.minimum_baseline_cases:
        return False
    if len({case.case_id for case in cases}) != len(cases):
        return False
    group_counts = Counter(case.capability_group for case in cases)
    if any(group_counts[group] < thresholds.minimum_cases_per_group for group in CAPABILITY_GROUPS):
        return False
    return all(
        case.manual_measured_in_single_session
        and case.manual_minutes >= thresholds.minimum_manual_minutes
        for case in cases
    )


def normalize_manual_minutes(value: float, thresholds: PilotThresholds) -> float:
    """Validate the observed raw value before rounding it for local persistence."""
    if value < thresholds.minimum_manual_minutes:
        raise ValueError("Manual measurement is below the configured validity floor")
    return round_minutes(value)


def calculate_harness_minutes(
    events: Sequence[AuditEvent],
    *,
    job_id: str,
    manual_fixup_minutes: float,
    idle_gap_minutes: float,
    engineer_activity: Sequence[EngineerActivityInterval] = (),
) -> tuple[float, float]:
    """Return ``(active + fixup, excluded idle)`` in rounded minutes.

    Every event from either party is activity. A gap is excluded in full only when it
    is strictly greater than the configured threshold.
    """
    if not isfinite(manual_fixup_minutes) or manual_fixup_minutes < 0.0:
        raise ValueError("manual_fixup_minutes must be finite and non-negative")
    relevant = sorted(
        (event for event in events if event.job_id == job_id), key=lambda event: event.created_at
    )
    try:
        start_index = next(
            index
            for index, event in enumerate(relevant)
            if event.event_type == AuditEventType.JOB_CREATED.value
        )
    except StopIteration as exc:
        raise ValueError("A JOB_CREATED event is required to measure harness effort") from exc
    measured = relevant[start_index:]
    if any(event.created_at.tzinfo is None for event in measured):
        raise ValueError("Audit event timestamps must be timezone-aware")
    start = measured[0].created_at
    spans = [(event.created_at, event.created_at) for event in measured]
    spans.extend(
        (interval.started_at, interval.ended_at)
        for interval in engineer_activity
        if interval.ended_at >= start
    )
    spans.sort(key=lambda span: (span[0], span[1]))
    merged: list[tuple[datetime, datetime]] = []
    for span_start, span_end in spans:
        span_start = max(span_start, start)
        if merged and span_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span_end))
        else:
            merged.append((span_start, span_end))
    idle_seconds = sum(
        (current[0] - previous[1]).total_seconds()
        for previous, current in pairwise(merged)
        if (current[0] - previous[1]).total_seconds() > idle_gap_minutes * 60.0
    )
    end = max([measured[-1].created_at, *(item.ended_at for item in engineer_activity)])
    wall_seconds = (end - start).total_seconds()
    active_minutes = max(0.0, (wall_seconds - idle_seconds) / 60.0)
    return (
        round_minutes(active_minutes + manual_fixup_minutes),
        round_minutes(idle_seconds / 60.0),
    )


def calculate_saving(*, harness_minutes: float, manual_minutes: float) -> float:
    """Calculate and round saving without clamping a negative result."""
    if manual_minutes <= 0.0:
        raise ValueError("manual_minutes must be positive")
    return round_saving(1.0 - harness_minutes / manual_minutes)


def calculate_statistics(values: Sequence[float]) -> tuple[float | None, float | None]:
    """Return median and linearly interpolated p95 for zero, one, odd, or even samples."""
    if not values:
        return None, None
    ordered = sorted(float(value) for value in values)
    median_value = float(median(ordered))
    rank = (len(ordered) - 1) * 0.95
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    p95 = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return median_value, p95


class MetricsCollector:
    """Build local effort records and aggregate complete pilot reports."""

    def __init__(self, thresholds: PilotThresholds) -> None:
        self.thresholds = thresholds

    def effort_from_events(
        self,
        *,
        record_id: str,
        pilot_run_id: str = "pilot_default",
        case_id: str,
        job_id: str,
        events: Sequence[AuditEvent],
        manual_fixup_minutes: float = 0.0,
        entities_manually_edited: int = 0,
        engineer_activity: Sequence[EngineerActivityInterval] = (),
        failure_reason: FailureReason | None = None,
    ) -> EffortRecord:
        """Collect safe numeric/id-only fields; prompts and geometry never enter the record."""
        harness_minutes, idle_minutes = calculate_harness_minutes(
            events,
            job_id=job_id,
            manual_fixup_minutes=manual_fixup_minutes,
            idle_gap_minutes=self.thresholds.idle_gap_minutes,
            engineer_activity=engineer_activity,
        )
        relevant = sorted(
            (event for event in events if event.job_id == job_id),
            key=lambda event: event.created_at,
        )
        spec_changes = sum(
            event.event_type == AuditEventType.SPEC_CHANGED.value for event in relevant
        )
        first_preview_index = next(
            (
                index
                for index, event in enumerate(relevant)
                if event.event_type == AuditEventType.PREVIEW_GENERATED.value
            ),
            None,
        )
        first_preview_clean = False
        if first_preview_index is not None:
            validation = None
            for event in relevant[first_preview_index + 1 :]:
                if event.event_type == AuditEventType.PREVIEW_GENERATED.value:
                    break
                if event.event_type == AuditEventType.VALIDATION_COMPLETED.value:
                    validation = event
                    break
            first_preview_clean = bool(
                validation is not None
                and validation.payload.get("blocking", 0) == 0
                and validation.payload.get("errors", 0) == 0
            )
        commit = next(
            (
                event
                for event in reversed(relevant)
                if event.event_type == AuditEventType.COMMIT_SUCCEEDED.value
            ),
            None,
        )
        entities_created = int(commit.payload.get("entities", 0)) if commit is not None else 0
        if commit is not None and failure_reason is not None:
            raise ValueError("A committed job cannot be recorded as a failed pilot case")
        return EffortRecord(
            pilot_run_id=pilot_run_id,
            record_id=record_id,
            case_id=case_id,
            job_id=job_id,
            harness_minutes=harness_minutes,
            idle_minutes_excluded=idle_minutes,
            manual_fixup_minutes=manual_fixup_minutes,
            spec_change_count=spec_changes,
            entities_created=entities_created,
            entities_manually_edited=entities_manually_edited,
            first_preview_clean=first_preview_clean,
            completed=commit is not None,
            failure_reason=failure_reason,
        )

    def aggregate(
        self,
        *,
        report_id: str,
        pilot_run_id: str = "pilot_default",
        baseline: Sequence[BaselineCase],
        efforts: Sequence[EffortRecord],
        failure_reasons: Mapping[str, FailureReason] | None = None,
        operation_samples_ms: Mapping[str, Sequence[float]] | None = None,
    ) -> PilotReport:
        """Aggregate without dropping failed or missing baseline cases."""
        if any(case.pilot_run_id != pilot_run_id for case in baseline):
            raise ValueError("Baseline contains a case from a different pilot run")
        if any(effort.pilot_run_id != pilot_run_id for effort in efforts):
            raise ValueError("Effort input contains a record from a different pilot run")
        reasons = failure_reasons or {}
        effort_by_case: dict[str, EffortRecord] = {}
        for effort in efforts:
            if effort.case_id in effort_by_case:
                raise ValueError(f"Duplicate effort record for case {effort.case_id}")
            effort_by_case[effort.case_id] = effort

        case_results = tuple(
            self._case_result(case, effort_by_case.get(case.case_id), reasons.get(case.case_id))
            for case in baseline
        )
        overall = self._metric("median_saving_overall", [case.saving for case in case_results])
        groups = tuple(
            self._metric(
                f"median_saving_{group}",
                [case.saving for case in case_results if case.capability_group == group],
            )
            for group in CAPABILITY_GROUPS
        )
        label_summaries = tuple(
            WorkLabelSummary(
                work_label=label,
                case_ids=tuple(case.case_id for case in case_results if case.work_label == label),
                median_saving=self._metric(
                    f"median_saving_{label}",
                    [case.saving for case in case_results if case.work_label == label],
                ),
            )
            for label in WORK_LABELS
        )
        aligned_efforts = tuple(effort_by_case.get(case.case_id) for case in baseline)
        first_preview_clean = self._ratio_metric(
            "first_preview_clean_rate",
            sum(bool(effort and effort.first_preview_clean) for effort in aligned_efforts),
            len(baseline),
            len(baseline),
        )
        spec_change_values = [
            float(effort.spec_change_count) if effort is not None else 0.0
            for effort in aligned_efforts
        ]
        median_spec_changes = self._metric("median_spec_changes", spec_change_values)
        entities_created = sum(effort.entities_created for effort in efforts)
        entities_edited = sum(effort.entities_manually_edited for effort in efforts)
        manual_edit_rate = self._ratio_metric(
            "manual_entity_edit_rate",
            entities_edited,
            entities_created,
            len(baseline),
        )
        committed_rate = self._ratio_metric(
            "committed_job_rate",
            sum(bool(effort and effort.completed) for effort in aligned_efforts),
            len(baseline),
            len(baseline),
        )
        biased = tuple(sorted(case.case_id for case in baseline if case.manual_measurement_biased))
        baseline_valid = is_valid_baseline(baseline, self.thresholds)
        all_efforts_present = len(effort_by_case) == len(baseline) and all(
            case.case_id in effort_by_case for case in baseline
        )
        goal_metrics_usable = not overall.insufficient_sample and all(
            not metric.insufficient_sample for metric in groups
        )
        medians_pass = bool(
            overall.value is not None
            and overall.value >= self.thresholds.overall_median_saving
            and all(
                metric.value is not None and metric.value >= self.thresholds.group_median_saving
                for metric in groups
            )
        )
        quality_failures: list[str] = []
        for metric, passes, name in (
            (
                first_preview_clean,
                first_preview_clean.value is not None
                and first_preview_clean.value >= self.thresholds.minimum_first_preview_clean_rate,
                "first_preview_clean_rate",
            ),
            (
                median_spec_changes,
                median_spec_changes.value is not None
                and median_spec_changes.value <= self.thresholds.maximum_median_spec_changes,
                "median_spec_changes",
            ),
            (
                manual_edit_rate,
                manual_edit_rate.value is not None
                and manual_edit_rate.value <= self.thresholds.maximum_manual_entity_edit_rate,
                "manual_entity_edit_rate",
            ),
            (
                committed_rate,
                committed_rate.value is not None
                and committed_rate.value >= self.thresholds.minimum_committed_job_rate,
                "committed_job_rate",
            ),
        ):
            if metric.insufficient_sample or not passes:
                quality_failures.append(name)
        savings_goal_met = (
            baseline_valid
            and all_efforts_present
            and not biased
            and goal_metrics_usable
            and medians_pass
        )
        quality_gates_met = not quality_failures
        return PilotReport(
            pilot_run_id=pilot_run_id,
            report_id=report_id,
            baseline_valid=baseline_valid,
            baseline_case_count=len(baseline),
            cases=case_results,
            overall_saving=overall,
            group_savings=groups,
            work_label_summaries=label_summaries,
            first_preview_clean_rate=first_preview_clean,
            median_spec_changes=median_spec_changes,
            manual_entity_edit_rate=manual_edit_rate,
            committed_job_rate=committed_rate,
            operation_metrics=self._operation_summaries(operation_samples_ms or {}),
            biased_case_ids=biased,
            goal_met=savings_goal_met,
            quality_gates_met=quality_gates_met,
            quality_gate_failures=tuple(quality_failures),
            pilot_acceptance_met=savings_goal_met and quality_gates_met,
        )

    def _case_result(
        self,
        case: BaselineCase,
        effort: EffortRecord | None,
        supplied_reason: FailureReason | None,
    ) -> PilotCaseResult:
        if effort is None:
            return PilotCaseResult(
                case_id=case.case_id,
                capability_group=case.capability_group,
                work_label=case.work_label,
                manual_minutes=case.manual_minutes,
                harness_minutes=None,
                saving=0.0,
                completed=False,
                effort_record_present=False,
                failure_reason=FailureReason.MISSING_EFFORT_RECORD,
            )
        saving = (
            calculate_saving(
                harness_minutes=effort.harness_minutes, manual_minutes=case.manual_minutes
            )
            if effort.completed
            else 0.0
        )
        failure_reason = supplied_reason or effort.failure_reason
        if saving < self.thresholds.minimum_case_saving and failure_reason is None:
            failure_reason = self._classify_failure(effort)
        return PilotCaseResult(
            case_id=case.case_id,
            capability_group=case.capability_group,
            work_label=case.work_label,
            manual_minutes=case.manual_minutes,
            harness_minutes=effort.harness_minutes,
            saving=saving,
            completed=effort.completed,
            effort_record_present=True,
            failure_reason=failure_reason,
        )

    def _classify_failure(self, effort: EffortRecord) -> FailureReason:
        if not effort.completed:
            return FailureReason.UNSUPPORTED_FEATURE
        if effort.manual_fixup_minutes > 0.0 or effort.entities_manually_edited > 0:
            return FailureReason.MANUAL_FIXUP
        if effort.spec_change_count > self.thresholds.maximum_median_spec_changes:
            return FailureReason.EXCESSIVE_SPEC_ITERATIONS
        return FailureReason.WORKFLOW_OVERHEAD

    def _metric(self, name: str, values: Sequence[float]) -> Metric:
        median_value, _ = calculate_statistics(values)
        return Metric(
            name=name,
            value=median_value,
            sample_count=len(values),
            insufficient_sample=len(values) < self.thresholds.minimum_metric_samples,
        )

    def _ratio_metric(
        self, name: str, numerator: int, denominator: int, sample_count: int
    ) -> Metric:
        return Metric(
            name=name,
            value=(numerator / denominator if denominator else None),
            sample_count=sample_count,
            insufficient_sample=sample_count < self.thresholds.minimum_metric_samples,
        )

    def _operation_summaries(
        self, operation_samples_ms: Mapping[str, Sequence[float]]
    ) -> tuple[OperationMetricSummary, ...]:
        summaries: list[OperationMetricSummary] = []
        for operation_name in OPERATION_NAMES:
            samples = operation_samples_ms.get(operation_name, ())
            median_value, p95_value = calculate_statistics(samples)
            insufficient = len(samples) < self.thresholds.minimum_metric_samples
            summaries.append(
                OperationMetricSummary(
                    operation_name=operation_name,
                    median_ms=Metric(
                        name=f"{operation_name}_median_ms",
                        value=median_value,
                        sample_count=len(samples),
                        insufficient_sample=insufficient,
                    ),
                    p95_ms=Metric(
                        name=f"{operation_name}_p95_ms",
                        value=p95_value,
                        sample_count=len(samples),
                        insufficient_sample=insufficient,
                    ),
                )
            )
        return tuple(summaries)


__all__ = [
    "OPERATION_NAMES",
    "EngineerActivityInterval",
    "MetricsCollector",
    "PerformanceThresholds",
    "PilotThresholds",
    "calculate_harness_minutes",
    "calculate_saving",
    "calculate_statistics",
    "is_valid_baseline",
    "load_pilot_thresholds",
    "normalize_manual_minutes",
]
