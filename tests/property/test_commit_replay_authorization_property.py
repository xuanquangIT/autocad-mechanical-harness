"""Property: malformed credentials never turn an idempotency key into a receipt token."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.config import Settings
from cad_harness.domain.errors import ApprovalScopeMismatchError
from cad_harness.domain.models.validation import Severity, ValidationStage


@given(invalid_token=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", max_size=96))
@hypothesis_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_malformed_token_never_authorizes_exact_commit_replay(
    invalid_token: str,
    settings: Settings,
    base_plate_spec: dict[str, Any],
) -> None:
    adapter = FakeAutoCADAdapter()
    service = HarnessService(settings, adapter)
    job = service.create_job()
    submitted = service.submit_spec(job.job_id, base_plate_spec)
    plan_hash = str(submitted["plan_hash"])
    service.preview(job.job_id)
    report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    warnings = tuple(
        finding.rule_id for finding in report.findings if finding.severity is Severity.WARNING
    )
    _, valid_token = service.approve(job.job_id, "property-engineer", warnings)
    service.commit(
        job.job_id,
        idempotency_key="property-replay-auth",
        expected_revision=job.expected_revision,
        plan_hash=plan_hash,
        approval_token=valid_token,
    )
    writes_after_first_commit = adapter.document.write_counter

    with pytest.raises(ApprovalScopeMismatchError):
        service.commit(
            job.job_id,
            idempotency_key="property-replay-auth",
            expected_revision=job.expected_revision,
            plan_hash=plan_hash,
            approval_token=invalid_token,
        )

    assert adapter.document.write_counter == writes_after_first_commit
