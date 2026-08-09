"""Properties 66-70: measured pilot effectiveness remains complete and reproducible."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from statistics import median

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.property.strategies import (
    AuditEventSequenceCase,
    BaselineSetCase,
    audit_event_sequences,
    baseline_sets,
)

from cad_harness.domain.models.metrics import EffortRecord, FailureReason
from cad_harness.metrics.collector import (
    MetricsCollector,
    calculate_harness_minutes,
    calculate_saving,
    calculate_statistics,
    is_valid_baseline,
    load_pilot_thresholds,
    normalize_manual_minutes,
)

THRESHOLDS = load_pilot_thresholds()


def _half_up(value: float, quantum: str) -> float:
    return float(Decimal(str(value)).quantize(Decimal(quantum), rounding=ROUND_HALF_UP))


# Feature: cad-ai-production-roadmap, Property 66: deterministic baseline predicate
@given(
    case=baseline_sets(),
    raw_minutes=st.floats(min_value=0.0, max_value=500.0, allow_nan=False),
)
@settings(max_examples=100, deadline=None)
def test_baseline_and_manual_measurement_validity_are_exact_predicates(
    case: BaselineSetCase, raw_minutes: float
) -> None:
    """**Validates: Requirements 24.1, 24.2, 24.14, Property 66**"""
    counts = Counter(item.capability_group for item in case.baseline)
    reference = (
        len(case.baseline) >= THRESHOLDS.minimum_baseline_cases
        and len({item.case_id for item in case.baseline}) == len(case.baseline)
        and all(counts[group] >= THRESHOLDS.minimum_cases_per_group for group in ("B", "D", "E"))
        and all(item.manual_measured_in_single_session for item in case.baseline)
        and all(item.manual_minutes >= THRESHOLDS.minimum_manual_minutes for item in case.baseline)
    )
    assert is_valid_baseline(case.baseline, THRESHOLDS) is reference

    if raw_minutes < THRESHOLDS.minimum_manual_minutes:
        with pytest.raises(ValueError):
            normalize_manual_minutes(raw_minutes, THRESHOLDS)
    else:
        assert normalize_manual_minutes(raw_minutes, THRESHOLDS) == _half_up(raw_minutes, "0.1")


def _reference_active_minutes(case: AuditEventSequenceCase) -> tuple[float, float]:
    events = sorted(case.events, key=lambda event: event.created_at)
    start = events[0].created_at
    spans: list[tuple[datetime, datetime]] = [
        (event.created_at, event.created_at) for event in events
    ]
    spans.extend((interval.started_at, interval.ended_at) for interval in case.engineer_activity)
    spans.sort()
    merged: list[list[datetime]] = []
    for left, right in spans:
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    gaps = [(right[0] - left[1]).total_seconds() / 60.0 for left, right in pairwise(merged)]
    excluded = sum(gap for gap in gaps if gap > THRESHOLDS.idle_gap_minutes)
    end = max([events[-1].created_at, *(item.ended_at for item in case.engineer_activity)])
    wall = (end - start).total_seconds() / 60.0
    return _half_up(wall - excluded + case.manual_fixup_minutes, "0.1"), _half_up(excluded, "0.1")


# Feature: cad-ai-production-roadmap, Property 67: time and saving formulas
@given(
    case=audit_event_sequences(),
    harness=st.floats(min_value=0.0, max_value=2_000.0, allow_nan=False),
    manual=st.floats(min_value=5.0, max_value=1_000.0, allow_nan=False),
)
@settings(max_examples=100, deadline=None)
def test_harness_time_and_saving_match_independent_reference_including_negative(
    case: AuditEventSequenceCase, harness: float, manual: float
) -> None:
    """**Validates: Requirements 24.3, 24.4, 24.5, Property 67**"""
    actual = calculate_harness_minutes(
        case.events,
        job_id="job-pilot-property",
        manual_fixup_minutes=case.manual_fixup_minutes,
        idle_gap_minutes=THRESHOLDS.idle_gap_minutes,
        engineer_activity=case.engineer_activity,
    )
    assert actual == _reference_active_minutes(case)

    reference_saving = _half_up(1.0 - harness / manual, "0.01")
    assert calculate_saving(harness_minutes=harness, manual_minutes=manual) == reference_saving
    if harness > manual:
        assert reference_saving <= 0.0


# Feature: cad-ai-production-roadmap, Property 68: unusable data never proves the goal
@given(case=baseline_sets())
@settings(max_examples=100, deadline=None)
def test_insufficient_or_biased_metrics_never_prove_goal(case: BaselineSetCase) -> None:
    """**Validates: Requirements 24.12, 24.15, Property 68**"""
    report = MetricsCollector(THRESHOLDS).aggregate(
        report_id="report-property-68",
        baseline=case.baseline,
        efforts=case.efforts,
    )
    metrics = (report.overall_saving, *report.group_savings)
    assert all(
        metric.insufficient_sample is (metric.sample_count < THRESHOLDS.minimum_metric_samples)
        for metric in metrics
    )
    if any(metric.insufficient_sample for metric in metrics) or report.biased_case_ids:
        assert not report.goal_met


# Feature: cad-ai-production-roadmap, Property 69: complete pilot aggregation
@given(case=baseline_sets())
@settings(max_examples=100, deadline=None)
def test_pilot_aggregation_keeps_denominators_partitions_and_failure_cases(
    case: BaselineSetCase,
) -> None:
    """**Validates: Requirements 24.6, 24.7, 24.16, 24.17, Property 69**"""
    report = MetricsCollector(THRESHOLDS).aggregate(
        report_id="report-property-69",
        baseline=case.baseline,
        efforts=case.efforts,
    )
    expected_ids = {item.case_id for item in case.baseline}
    assert {item.case_id for item in report.cases} == expected_ids
    assert report.baseline_case_count == len(case.baseline)
    for result in report.cases:
        effort = next(item for item in case.efforts if item.case_id == result.case_id)
        if not effort.completed:
            assert result.saving == 0.0
        if result.saving < THRESHOLDS.minimum_case_saving:
            assert isinstance(result.failure_reason, FailureReason)

    partitions = [set(summary.case_ids) for summary in report.work_label_summaries]
    assert partitions[0].isdisjoint(partitions[1])
    assert partitions[0] | partitions[1] == expected_ids

    overall = float(median(result.saving for result in report.cases)) if report.cases else None
    groups = {
        group: [result.saving for result in report.cases if result.capability_group == group]
        for group in ("B", "D", "E")
    }
    structurally_valid = is_valid_baseline(case.baseline, THRESHOLDS)
    enough_samples = bool(
        len(report.cases) >= THRESHOLDS.minimum_metric_samples
        and all(len(values) >= THRESHOLDS.minimum_metric_samples for values in groups.values())
    )
    medians_pass = bool(
        overall is not None
        and overall >= THRESHOLDS.overall_median_saving
        and all(
            values and median(values) >= THRESHOLDS.group_median_saving
            for values in groups.values()
        )
    )
    expected_goal = (
        structurally_valid
        and enough_samples
        and medians_pass
        and not any(item.manual_measurement_biased for item in case.baseline)
    )
    assert report.goal_met is expected_goal


# Feature: cad-ai-production-roadmap, Property 70: ratios, median and p95
@given(
    case=baseline_sets(),
    values=st.lists(st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False), max_size=40),
    zero_entities=st.booleans(),
)
@settings(max_examples=100, deadline=None)
def test_statistics_and_rates_match_independent_reference(
    case: BaselineSetCase, values: list[float], zero_entities: bool
) -> None:
    """**Validates: Requirements 24.8, 24.10, 24.11, 26.8, Property 70**"""
    ordered = sorted(values)
    if ordered:
        reference_median = float(median(ordered))
        rank = (len(ordered) - 1) * 0.95
        lower = int(rank)
        fraction = rank - lower
        upper = min(lower + 1, len(ordered) - 1)
        reference_p95 = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
        assert calculate_statistics(values) == (reference_median, reference_p95)
    else:
        assert calculate_statistics(values) == (None, None)

    efforts = tuple(
        EffortRecord(
            **item.model_dump(exclude={"entities_created"}),
            entities_created=0 if zero_entities else item.entities_created,
        )
        for item in case.efforts
    )
    report = MetricsCollector(THRESHOLDS).aggregate(
        report_id="report-property-70",
        baseline=case.baseline,
        efforts=efforts,
        operation_samples_ms={"compile": values},
    )
    preview_numerator = sum(item.first_preview_clean for item in efforts)
    committed_numerator = sum(item.completed for item in efforts)
    denominator = len(case.baseline)
    assert report.first_preview_clean_rate.value == (
        preview_numerator / denominator if denominator else None
    )
    assert report.committed_job_rate.value == (
        committed_numerator / denominator if denominator else None
    )
    created = sum(item.entities_created for item in efforts)
    edited = sum(item.entities_manually_edited for item in efforts)
    assert report.manual_entity_edit_rate.value == (edited / created if created else None)
    expected_quality = bool(
        report.first_preview_clean_rate.value is not None
        and report.first_preview_clean_rate.value >= THRESHOLDS.minimum_first_preview_clean_rate
        and report.median_spec_changes.value is not None
        and report.median_spec_changes.value <= THRESHOLDS.maximum_median_spec_changes
        and report.manual_entity_edit_rate.value is not None
        and report.manual_entity_edit_rate.value <= THRESHOLDS.maximum_manual_entity_edit_rate
        and report.committed_job_rate.value is not None
        and report.committed_job_rate.value >= THRESHOLDS.minimum_committed_job_rate
        and all(
            not metric.insufficient_sample
            for metric in (
                report.first_preview_clean_rate,
                report.median_spec_changes,
                report.manual_entity_edit_rate,
                report.committed_job_rate,
            )
        )
    )
    assert report.quality_gates_met is expected_quality
    assert report.pilot_acceptance_met is (report.goal_met and expected_quality)
    compile_summary = next(
        item for item in report.operation_metrics if item.operation_name == "compile"
    )
    assert (compile_summary.median_ms.value, compile_summary.p95_ms.value) == calculate_statistics(
        values
    )
