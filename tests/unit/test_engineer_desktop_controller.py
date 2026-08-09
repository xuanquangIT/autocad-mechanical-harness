"""Engineer desktop uses HarnessService directly and never renders its token."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from apps.engineer_desktop.controller import EngineerDesktopController
from apps.engineer_desktop.effort_session import EngineerEffortSession

from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.application.services.metrics_service import MetricsService
from cad_harness.domain.errors import (
    ApprovalRequiredError,
    PlanHashMismatchError,
    StaleDocumentRevisionError,
)
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.metrics import FailureReason
from cad_harness.domain.models.validation import Severity, ValidationStage
from cad_harness.metrics.collector import load_pilot_thresholds


def _prepared(
    service: HarnessService,
    spec: dict[str, Any],
) -> tuple[str, frozenset[str]]:
    job = service.create_job()
    service.submit_spec(job.job_id, spec)
    service.preview(job.job_id)
    report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    warnings = frozenset(
        finding.rule_id for finding in report.findings if finding.severity is Severity.WARNING
    )
    return job.job_id, warnings


def test_controller_calls_service_directly_and_keeps_token_out_of_display(
    service: HarnessService,
    base_plate_spec: dict[str, Any],
    monkeypatch,
    caplog,
) -> None:
    job_id, warnings = _prepared(service, base_plate_spec)
    controller = EngineerDesktopController(service)
    view = controller.refresh(job_id)
    calls: list[tuple[str, str, tuple[str, ...], str | None, str | None]] = []
    sentinel = "approval_test.SUPER_SECRET_TOKEN"

    def approve(
        job: str,
        engineer: str,
        acknowledged: tuple[str, ...],
        *,
        displayed_plan_hash: str | None = None,
        displayed_revision: str | None = None,
    ):
        calls.append((job, engineer, acknowledged, displayed_plan_hash, displayed_revision))
        return "approval_test", sentinel

    monkeypatch.setattr(service, "approve", approve)
    outcome = controller.approve(
        approved_by="engineer-1",
        acknowledged_warning_rule_ids=warnings,
    )

    assert outcome.approved
    assert calls == [
        (
            job_id,
            "engineer-1",
            tuple(sorted(warnings)),
            service.store.get_plan(job_id).plan_hash,  # type: ignore[union-attr]
            service.store.get_job(job_id).expected_revision,  # type: ignore[union-attr]
        )
    ]
    assert controller.has_in_memory_approval
    rendered = " ".join((repr(controller), repr(view), repr(outcome), caplog.text))
    assert sentinel not in rendered
    assert not hasattr(view, "approval_token")
    assert not hasattr(outcome, "approval_token")


def test_controller_rechecks_plan_hash_before_calling_approve(
    service: HarnessService,
    base_plate_spec: dict[str, Any],
    monkeypatch,
) -> None:
    job_id, warnings = _prepared(service, base_plate_spec)
    controller = EngineerDesktopController(service)
    controller.refresh(job_id)
    changed = {
        **base_plate_spec,
        "features": [
            {
                **base_plate_spec["features"][0],
                "parameters": {
                    **base_plate_spec["features"][0]["parameters"],
                    "width_mm": 190.0,
                },
            }
        ],
    }
    service.submit_spec(job_id, changed)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("service.approve must not run for a changed plan")

    monkeypatch.setattr(service, "approve", forbidden)
    outcome = controller.approve(
        approved_by="engineer-1",
        acknowledged_warning_rule_ids=warnings,
    )
    assert not outcome.approved
    assert outcome.reasons == ("plan_hash_changed",)


def test_controller_rechecks_document_revision_before_calling_approve(
    service: HarnessService,
    adapter: FakeAutoCADAdapter,
    base_plate_spec: dict[str, Any],
    monkeypatch,
) -> None:
    job_id, warnings = _prepared(service, base_plate_spec)
    controller = EngineerDesktopController(service)
    controller.refresh(job_id)
    adapter.document.write_counter += 1

    def forbidden(*_args, **_kwargs):
        raise AssertionError("service.approve must not run for a changed revision")

    monkeypatch.setattr(service, "approve", forbidden)
    outcome = controller.approve(
        approved_by="engineer-1",
        acknowledged_warning_rule_ids=warnings,
    )
    assert not outcome.approved
    assert outcome.reasons == ("revision_changed",)


def test_controller_consumes_private_token_during_commit(
    service: HarnessService,
    base_plate_spec: dict[str, Any],
) -> None:
    job_id, warnings = _prepared(service, base_plate_spec)
    controller = EngineerDesktopController(service)
    controller.refresh(job_id)
    outcome = controller.approve(
        approved_by="engineer-1",
        acknowledged_warning_rule_ids=warnings,
    )
    assert outcome.approved
    result = controller.commit_approved(idempotency_key="desktop-controller-test")
    assert result.new_revision != result.previous_revision
    assert not controller.has_in_memory_approval


def test_controller_keeps_separate_rollback_token_memory_only(
    service: HarnessService,
    base_plate_spec: dict[str, Any],
    caplog,
) -> None:
    job_id, warnings = _prepared(service, base_plate_spec)
    controller = EngineerDesktopController(service)
    controller.refresh(job_id)
    assert controller.approve(
        approved_by="engineer-1",
        acknowledged_warning_rule_ids=warnings,
    ).approved
    controller.commit_approved(idempotency_key="desktop-rollback-commit")

    scope = controller.prepare_rollback(job_id)
    outcome = controller.approve_rollback(approved_by="engineer-rollback")
    assert outcome.approved
    assert controller.has_in_memory_rollback_approval
    rendered = " ".join((repr(controller), repr(scope), repr(outcome), caplog.text))
    assert "rb1." not in rendered

    result = controller.rollback_approved()
    assert result.checkpoint_id == scope.checkpoint_id
    assert not controller.has_in_memory_rollback_approval


def test_controller_can_reopen_committed_job_for_rollback_review(
    service: HarnessService,
    base_plate_spec: dict[str, Any],
) -> None:
    job_id, warnings = _prepared(service, base_plate_spec)
    first = EngineerDesktopController(service)
    first.refresh(job_id)
    assert first.approve(
        approved_by="engineer-1",
        acknowledged_warning_rule_ids=warnings,
    ).approved
    first.commit_approved(idempotency_key="desktop-reopen-commit")

    reopened = EngineerDesktopController(service)
    view = reopened.refresh(job_id)
    assert view.state is JobState.COMMITTED
    assert reopened.rollback_is_available(job_id)
    scope = reopened.prepare_rollback(job_id)
    assert scope.current_revision == view.current_revision


def test_service_rechecks_displayed_bindings_at_approval_boundary(
    service: HarnessService,
    adapter: FakeAutoCADAdapter,
    base_plate_spec: dict[str, Any],
) -> None:
    job_id, warnings = _prepared(service, base_plate_spec)
    job = service.store.get_job(job_id)
    plan = service.store.get_plan(job_id)
    assert job is not None and plan is not None and plan.plan_hash is not None

    with pytest.raises(PlanHashMismatchError):
        service.approve(
            job_id,
            "engineer",
            tuple(warnings),
            displayed_plan_hash="sha256:stale-plan",
            displayed_revision=job.expected_revision,
        )

    adapter.document.write_counter += 1
    with pytest.raises(StaleDocumentRevisionError):
        service.approve(
            job_id,
            "engineer",
            tuple(warnings),
            displayed_plan_hash=plan.plan_hash,
            displayed_revision=job.expected_revision,
        )


def test_controller_refuses_recompiled_evidence_for_a_different_stored_plan(
    service: HarnessService,
    base_plate_spec: dict[str, Any],
) -> None:
    job_id, _ = _prepared(service, base_plate_spec)
    plan = service.store.get_plan(job_id)
    assert plan is not None
    service.store.save_plan(plan.model_copy(update={"plan_hash": "sha256:changed"}))

    with pytest.raises(ApprovalRequiredError):
        EngineerDesktopController(service).refresh(job_id)


def test_controller_persists_click_marked_pilot_effort(
    service: HarnessService,
    base_plate_spec: dict[str, Any],
) -> None:
    class Store:
        pilot_run_id = "pilot-test"

        def __init__(self) -> None:
            self.efforts = []
            self.baselines = []

        def save_baseline_case(self, case) -> None:
            self.baselines.append(case)

        def save_effort_record(self, record) -> None:
            self.efforts.append(record)

        def baseline_cases(self):
            return tuple(self.baselines)

        def effort_records(self):
            return tuple(self.efforts)

        def operation_samples_ms(self):
            return {}

    job_id, warnings = _prepared(service, base_plate_spec)
    store = Store()
    metrics = MetricsService(
        thresholds=load_pilot_thresholds(),
        store=store,
        audit_events=service.audit,
    )
    metrics.record_baseline(
        case_id="case-test",
        capability_group="B",
        work_label="ve_moi",
        raw_manual_minutes=10.0,
        measured_by="engineer-pilot",
        biased=False,
        measured_in_single_session=True,
    )
    controller = EngineerDesktopController(
        service,
        metrics_service=metrics,
        pilot_case_id="case-test",
        pilot_job_id=job_id,
    )
    moments = iter(
        (
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=2),
        )
    )
    controller.effort_session = EngineerEffortSession(clock=lambda: next(moments))
    controller.effort_session.start_activity()
    controller.effort_session.stop_activity()
    controller.refresh(job_id)
    outcome = controller.approve(
        approved_by="engineer-pilot",
        acknowledged_warning_rule_ids=warnings,
    )
    assert outcome.approved
    controller.commit_approved(idempotency_key="pilot-controller-commit")
    record = controller.finalize_pilot_effort(job_id=job_id, entities_manually_edited=1)

    assert store.efforts == [record]
    assert record.pilot_run_id == "pilot-test"
    assert record.case_id == "case-test"
    assert record.completed


def test_controller_persists_classified_failed_effort_only_for_bound_job(
    service: HarnessService,
) -> None:
    class Store:
        pilot_run_id = "pilot-failed"

        def __init__(self) -> None:
            self.efforts = []
            self.baselines = []

        def save_baseline_case(self, case) -> None:
            self.baselines.append(case)

        def save_effort_record(self, record) -> None:
            self.efforts.append(record)

        def baseline_cases(self):
            return tuple(self.baselines)

        def effort_records(self):
            return tuple(self.efforts)

        def operation_samples_ms(self):
            return {}

    bound_job = service.create_job()
    unrelated_job = service.create_job()
    store = Store()
    metrics = MetricsService(
        thresholds=load_pilot_thresholds(),
        store=store,
        audit_events=service.audit,
    )
    metrics.record_baseline(
        case_id="case-failed",
        capability_group="D",
        work_label="sua_ban_co_san",
        raw_manual_minutes=12.0,
        measured_by="engineer-pilot",
        biased=False,
        measured_in_single_session=True,
    )
    controller = EngineerDesktopController(
        service,
        metrics_service=metrics,
        pilot_case_id="case-failed",
        pilot_job_id=bound_job.job_id,
    )

    with pytest.raises(ValueError, match="bound desktop job"):
        controller.finalize_failed_pilot_effort(
            job_id=unrelated_job.job_id,
            failure_reason=FailureReason.UNSUPPORTED_FEATURE,
            entities_manually_edited=0,
        )

    record = controller.finalize_failed_pilot_effort(
        job_id=bound_job.job_id,
        failure_reason=FailureReason.UNSUPPORTED_FEATURE,
        entities_manually_edited=0,
    )
    report = metrics.build_report(report_id="report-failed")

    assert store.efforts == [record]
    assert not record.completed
    assert record.failure_reason is FailureReason.UNSUPPORTED_FEATURE
    assert report.cases[0].failure_reason is FailureReason.UNSUPPORTED_FEATURE
