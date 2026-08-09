"""SQLite round-trip for local-only pilot and operation metric records."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cad_harness.domain.models.metrics import BaselineCase, EffortRecord
from cad_harness.persistence.engine import build_engine, build_session_factory, create_all
from cad_harness.persistence.models import Document, Job
from cad_harness.persistence.sql_metrics_store import SqlMetricsStore


@pytest.fixture
def metrics_store(tmp_path: Path) -> SqlMetricsStore:
    engine = build_engine(tmp_path / "metrics.db")
    create_all(engine)
    factory: sessionmaker[Session] = build_session_factory(engine)
    with factory() as session:
        session.add(
            Document(
                document_id="doc-metrics",
                path_hash="sha256:path",
                current_revision="sha256:revision",
            )
        )
        session.add(
            Job(
                job_id="job-metrics",
                document_id="doc-metrics",
                state="committed",
                expected_revision="sha256:revision",
            )
        )
        session.commit()
    return SqlMetricsStore(factory)


def test_local_metrics_round_trip_contains_only_ids_counts_and_durations(
    metrics_store: SqlMetricsStore,
) -> None:
    baseline = BaselineCase(
        case_id="case-metrics",
        capability_group="B",
        work_label="ve_moi",
        manual_minutes=15.0,
        manual_measured_by="engineer-1",
        manual_measurement_biased=False,
        manual_measured_in_single_session=True,
    )
    effort = EffortRecord(
        record_id="effort-metrics",
        case_id=baseline.case_id,
        job_id="job-metrics",
        harness_minutes=3.0,
        idle_minutes_excluded=6.0,
        manual_fixup_minutes=0.0,
        spec_change_count=1,
        entities_created=10,
        entities_manually_edited=1,
        first_preview_clean=True,
        completed=True,
    )
    metrics_store.save_baseline_case(baseline)
    metrics_store.save_effort_record(effort)
    metrics_store.record_operation(
        metric_id="metric-1", operation_name="compile", duration_ms=12.5, entity_count=10
    )

    assert metrics_store.baseline_cases() == (baseline,)
    assert metrics_store.effort_records() == (effort,)
    assert metrics_store.operation_samples_ms()["compile"] == (12.5,)
    with pytest.raises(IntegrityError):
        metrics_store.save_baseline_case(baseline)
    with pytest.raises(ValueError):
        metrics_store.record_operation(
            metric_id="metric-invalid",
            operation_name="prompt",
            duration_ms=1.0,
            entity_count=0,
        )


def test_same_case_identifier_is_isolated_between_pilot_runs(tmp_path: Path) -> None:
    engine = build_engine(tmp_path / "run-isolation.db")
    create_all(engine)
    factory = build_session_factory(engine)
    first = SqlMetricsStore(factory, pilot_run_id="run-a")
    second = SqlMetricsStore(factory, pilot_run_id="run-b")
    common = {
        "case_id": "same-case",
        "capability_group": "B",
        "work_label": "ve_moi",
        "manual_minutes": 10.0,
        "manual_measured_by": "engineer-1",
        "manual_measurement_biased": False,
        "manual_measured_in_single_session": True,
    }
    first.save_baseline_case(BaselineCase(pilot_run_id="run-a", **common))
    second.save_baseline_case(BaselineCase(pilot_run_id="run-b", **common))

    assert [case.pilot_run_id for case in first.baseline_cases()] == ["run-a"]
    assert [case.pilot_run_id for case in second.baseline_cases()] == ["run-b"]
