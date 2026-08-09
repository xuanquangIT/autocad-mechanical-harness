"""Property 8: forbidden data never reaches observability records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.observability.audit import InMemoryAuditSink
from cad_harness.observability.logging import _redact_event
from cad_harness.persistence.models import OperationMetricRow

forbidden_text = st.text(min_size=8, max_size=40).filter(
    lambda value: "[redacted]" not in value and "path:" not in value
)
coordinates = st.lists(
    st.tuples(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    ),
    min_size=1,
    max_size=200,
)


def _numeric_leaf_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_numeric_leaf_count(item) for item in value.values())
    if isinstance(value, list | tuple):
        return sum(_numeric_leaf_count(item) for item in value)
    return int(isinstance(value, int | float) and not isinstance(value, bool))


def _string_leaves(value: Any) -> tuple[str, ...]:
    if isinstance(value, dict):
        return tuple(item for child in value.values() for item in _string_leaves(child))
    if isinstance(value, list | tuple):
        return tuple(item for child in value for item in _string_leaves(child))
    return (value,) if isinstance(value, str) else ()


# Feature: cad-ai-production-roadmap, Property 8: Không log, audit hay metric nào chứa dữ liệu bị cấm
@given(
    prompt=forbidden_text,
    token=forbidden_text,
    customer=forbidden_text,
    geometry=coordinates,
)
@settings(max_examples=100, deadline=None)
def test_forbidden_data_never_occurs_in_logs_audit_or_metrics(
    prompt: str,
    token: str,
    customer: str,
    geometry: list[tuple[float, float]],
) -> None:
    """**Validates: Requirements 3.4, 13.10, 18.9, 24.13, 27.2**"""
    prompt = f"PROMPT<{prompt}>"
    token = f"TOKEN<{token}>"
    raw_path = f"C:/Customers/{customer}/part.dwg"
    payload = {
        "prompt": prompt,
        "approval_token": token,
        "target_path": raw_path,
        "geometry": {"points": geometry},
        "entity_count": len(geometry),
    }

    log_record = _redact_event(None, "info", {"event": "operation", **payload})
    audit = InMemoryAuditSink()
    audit.append(
        event_type="PROPERTY_EVENT",
        job_id=None,
        actor_type="system",
        actor_id="property",
        payload=payload,
    )
    audit_record = audit.events[-1].payload
    metric = OperationMetricRow(
        metric_id="metric_property",
        operation_name="property_operation",
        duration_ms=1.0,
        entity_count=len(geometry),
        created_at=datetime.now(UTC),
    )
    metric_record = {
        "metric_id": metric.metric_id,
        "operation_name": metric.operation_name,
        "duration_ms": metric.duration_ms,
        "entity_count": metric.entity_count,
    }

    for record in (log_record, audit_record, metric_record):
        leaves = _string_leaves(record)
        assert all(prompt not in leaf for leaf in leaves)
        assert all(token not in leaf for leaf in leaves)
        assert all(raw_path not in leaf for leaf in leaves)
        assert _numeric_leaf_count(record) <= 3
        assert json.dumps(record, ensure_ascii=False)
