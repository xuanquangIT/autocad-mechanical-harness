"""Property 1 for durable SQLite job aggregate persistence."""

from __future__ import annotations

from uuid import uuid4

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cad_harness.domain.models.job import CadJob, JobState
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.validation import (
    Finding,
    Severity,
    ValidationReport,
    ValidationStage,
)
from cad_harness.persistence.engine import build_engine, build_session_factory, create_all
from cad_harness.persistence.sql_job_store import SqlJobStore


# Feature: cad-ai-production-roadmap, Property 1: Ghi trạng thái job bền vững và round-trip qua persistence
@given(
    revision=st.text(alphabet="abcdef0123456789", min_size=1, max_size=32),
    severity=st.sampled_from(tuple(Severity)),
    mapping_count=st.integers(min_value=0, max_value=6),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_job_state_round_trips_across_store_restart(
    tmp_path, revision: str, severity: Severity, mapping_count: int
) -> None:
    """**Validates: Requirements 1.2, 1.3, 1.4**"""
    suffix = uuid4().hex
    database = tmp_path / f"property-1-{suffix}.db"
    engine = build_engine(database)
    create_all(engine)
    store = SqlJobStore(build_session_factory(engine))

    job = CadJob(job_id=f"job_{suffix}", document_id=f"doc_{suffix}", expected_revision=revision)
    store.save_job(job)
    job = job.transition_to(JobState.SPEC_ACCEPTED)
    store.save_job(job)

    plan = OperationPlan(
        plan_id=f"plan_{suffix}",
        job_id=job.job_id,
        document_id=job.document_id,
        expected_revision=revision,
        profile_ref="demo-profile@1.0",
    ).with_hash()
    store.save_plan(plan)
    job = job.transition_to(JobState.PLANNED, plan_id=plan.plan_id, plan_hash=plan.plan_hash)
    store.save_job(job)
    job = job.transition_to(JobState.PREVIEWED)
    store.save_job(job)

    report = ValidationReport(
        validation_id=f"validation_{suffix}",
        job_id=job.job_id,
        stage=ValidationStage.PRE_COMMIT,
        plan_hash=plan.plan_hash,
        findings=(Finding(rule_id="PERSISTED", severity=severity, message="persist me"),),
    )
    store.save_validation(report)
    job = job.transition_to(JobState.VALIDATED)
    store.save_job(job)

    for index in range(mapping_count):
        store.map_entity(
            document_id=job.document_id,
            feature_id=f"feature:{index}",
            operation_id=f"op:{index}",
            entity_ref=f"entity:{index}",
            revision=revision,
        )

    expected_mappings = store.entity_mappings_for(job.document_id)
    engine.dispose()
    restarted = SqlJobStore(build_session_factory(build_engine(database)))
    restored = restarted.get_job(job.job_id)
    assert restored == job
    assert restarted.get_plan(job.job_id) == plan
    assert restarted.get_validation(job.job_id) == report
    assert restarted.entity_mappings_for(job.document_id) == expected_mappings
