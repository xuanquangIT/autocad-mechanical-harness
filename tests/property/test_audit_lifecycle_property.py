"""Property 58: ordered and complete audit lifecycle evidence."""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cad_harness.application.services.harness_service import HarnessService
from cad_harness.domain.models.validation import ValidationStage


def _assert_ordered_subsequence(actual: list[str], expected: list[str]) -> None:
    cursor = 0
    for event in actual:
        if cursor < len(expected) and event == expected[cursor]:
            cursor += 1
    assert cursor == len(expected), (actual, expected[cursor:])


# Feature: cad-ai-production-roadmap, Property 58: complete ordered write lifecycle
@given(change_spec=st.booleans())
@settings(
    max_examples=2,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_write_lifecycle_events_are_complete_and_ordered(
    service: HarnessService,
    base_plate_spec: dict[str, Any],
    change_spec: bool,
) -> None:
    """**Validates: Requirements 19.4, 22.3, 27.3**"""
    job = service.create_job()
    submitted = service.submit_spec(job.job_id, base_plate_spec)
    expected = [
        "DOCUMENT_INSPECTED",
        "JOB_CREATED",
        "SPEC_SUBMITTED",
        "PLAN_COMPILED",
    ]
    if change_spec:
        changed = {
            **base_plate_spec,
            "features": [
                {
                    **base_plate_spec["features"][0],
                    "parameters": {
                        **base_plate_spec["features"][0]["parameters"],
                        "width_mm": 180.0,
                    },
                }
            ],
        }
        submitted = service.submit_spec(job.job_id, changed)
        expected.extend(["SPEC_CHANGED", "SPEC_SUBMITTED", "PLAN_COMPILED"])

    service.preview(job.job_id)
    service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    _, token = service.approve(job.job_id, "engineer", ("STD-PROFILE-PROVENANCE",))
    service.commit(
        job.job_id,
        idempotency_key=f"property-{change_spec}",
        expected_revision=job.expected_revision,
        plan_hash=str(submitted["plan_hash"]),
        approval_token=token,
    )
    expected.extend(
        [
            "PREVIEW_GENERATED",
            "VALIDATION_COMPLETED",
            "APPROVAL_GRANTED",
            "COMMIT_STARTED",
            "COMMIT_SUCCEEDED",
        ]
    )
    events = [event.event_type for event in service.audit.events]
    _assert_ordered_subsequence(events, expected)
    assert service.audit.verify_chain()
