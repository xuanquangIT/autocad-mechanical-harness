"""Property 2 for persisted audit hash chains."""

from __future__ import annotations

from datetime import UTC
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.errors import ErrorCode, HarnessError
from cad_harness.persistence.engine import build_engine, build_session_factory, create_all
from cad_harness.persistence.models import AuditEventRow
from cad_harness.persistence.sql_audit_sink import SqlAuditSink


# Feature: cad-ai-production-roadmap, Property 2: Chuỗi audit hash được xây, xác minh, và phát hiện mọi can thiệp
@given(
    payload_values=st.lists(
        st.text(alphabet="abcXYZ0123", min_size=0, max_size=20), min_size=1, max_size=10
    ),
    tamper_index=st.integers(min_value=0, max_value=100),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_audit_chain_matches_reference_formula_and_detects_tampering(
    tmp_path, payload_values: list[str], tamper_index: int
) -> None:
    """**Validates: Requirements 1.5, 1.6, 27.4**"""
    database = tmp_path / f"property-2-{uuid4().hex}.db"
    engine = build_engine(database)
    create_all(engine)
    factory = build_session_factory(engine)
    sink = SqlAuditSink(factory)
    job_id = f"job_{uuid4().hex}"

    for index, value in enumerate(payload_values):
        sink.append(
            event_type=f"EVENT_{index}",
            job_id=job_id,
            actor_type="property_test",
            actor_id="hypothesis",
            payload={"value": value},
        )

    assert sink.verify_chain(job_id)
    timeline = sink.events_for_job(job_id)
    assert len(timeline) == len(payload_values)
    assert all(event.created_at.tzinfo is not None for event in timeline)
    assert [event.created_at for event in timeline] == sorted(
        event.created_at for event in timeline
    )
    with factory() as session:
        rows = session.scalars(
            select(AuditEventRow)
            .where(AuditEventRow.job_id == job_id)
            .order_by(AuditEventRow.created_at, AuditEventRow.event_id)
        ).all()
        previous_hash = None
        for row in rows:
            created_at = row.created_at.replace(tzinfo=UTC)
            expected_hash = sha256_of(
                {
                    "event_id": row.event_id,
                    "event_type": row.event_type,
                    "job_id": row.job_id,
                    "actor_type": row.actor_type,
                    "actor_id": row.actor_id,
                    "payload": row.payload_redacted_json,
                    "created_at": created_at.isoformat(),
                    "previous_event_hash": previous_hash,
                }
            )
            assert row.previous_event_hash == previous_hash
            assert row.event_hash == expected_hash
            previous_hash = expected_hash

        target = rows[tamper_index % len(rows)]
        target.payload_redacted_json = {"tampered": True}
        broken_id = target.event_id
        session.commit()

    with pytest.raises(HarnessError) as caught:
        sink.verify_chain(job_id)
    assert caught.value.code is ErrorCode.INTERNAL_ERROR
    assert caught.value.required_action
    assert caught.value.details["broken_at_event_id"] == broken_id
