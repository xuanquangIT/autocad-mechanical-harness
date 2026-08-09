"""Human-only controller that talks to HarnessService directly, never through MCP."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from pydantic import SecretStr

from apps.engineer_desktop.approval_gate import ApprovalReason, can_approve
from apps.engineer_desktop.effort_session import EngineerEffortSession
from apps.engineer_desktop.view_model import (
    ApprovalViewInputs,
    ApprovalViewModel,
    build_approval_view_model,
)
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.application.services.metrics_service import MetricsService
from cad_harness.application.services.plan_compiler import CompilationResult
from cad_harness.diff.semantic_diff import build_semantic_diff
from cad_harness.domain.errors import ApprovalRequiredError, DocumentNotFoundError
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.metrics import EffortRecord, FailureReason
from cad_harness.domain.models.result import CommitResult, PreviewArtifact, RollbackResult


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    approved: bool
    approval_id: str | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalEligibility:
    can_approve: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _DisplayedScope:
    job_id: str
    plan_hash: str
    expected_revision: str


@dataclass(frozen=True, slots=True)
class RollbackReviewScope:
    job_id: str
    document_id: str
    checkpoint_id: str
    current_revision: str


class EngineerDesktopController:
    """Own one approval window session and keep its credential memory-only."""

    def __init__(
        self,
        service: HarnessService,
        *,
        metrics_service: MetricsService | None = None,
        pilot_case_id: str | None = None,
        pilot_job_id: str | None = None,
    ) -> None:
        supplied = (
            metrics_service is not None,
            pilot_case_id is not None,
            pilot_job_id is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError(
                "metrics_service, pilot_case_id and pilot_job_id must be supplied together"
            )
        self._service = service
        self._metrics_service = metrics_service
        self._pilot_case_id = pilot_case_id
        self._pilot_job_id = pilot_job_id
        self._displayed_scope: _DisplayedScope | None = None
        self._approval_token: SecretStr | None = None
        self._approved_scope: _DisplayedScope | None = None
        self._rollback_scope: RollbackReviewScope | None = None
        self._rollback_approval_token: SecretStr | None = None
        self.effort_session = EngineerEffortSession()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(displayed={self._displayed_scope is not None}, "
            f"approved={self._approval_token is not None})"
        )

    @property
    def has_in_memory_approval(self) -> bool:
        return self._approval_token is not None

    @property
    def has_in_memory_rollback_approval(self) -> bool:
        return self._rollback_approval_token is not None

    @property
    def pilot_effort_enabled(self) -> bool:
        return all(
            item is not None
            for item in (self._metrics_service, self._pilot_case_id, self._pilot_job_id)
        )

    def _require_pilot_job(self, job_id: str) -> tuple[MetricsService, str]:
        if self._metrics_service is None or self._pilot_case_id is None:
            raise ValueError("This desktop session is not attached to a pilot case")
        if self._pilot_job_id != job_id:
            raise ValueError("Pilot effort can be finalized only for its bound desktop job")
        return self._metrics_service, self._pilot_case_id

    def finalize_pilot_effort(self, *, job_id: str, entities_manually_edited: int) -> EffortRecord:
        """Persist explicit desktop activity into the configured local pilot run."""
        if self.effort_session.active:
            raise ValueError("Stop engineer effort timing before finalizing the pilot case")
        metrics_service, pilot_case_id = self._require_pilot_job(job_id)
        job = self._service.store.get_job(job_id)
        if job is None or job.state is not JobState.COMMITTED:
            raise ValueError("A desktop pilot effort can be finalized only after commit succeeds")
        return metrics_service.collect_effort(
            record_id=f"effort_{uuid4().hex}",
            case_id=pilot_case_id,
            job_id=job_id,
            engineer_activity=self.effort_session.intervals,
            manual_fixup_minutes=self.effort_session.manual_fixup_minutes,
            entities_manually_edited=entities_manually_edited,
        )

    def finalize_failed_pilot_effort(
        self,
        *,
        job_id: str,
        failure_reason: FailureReason,
        entities_manually_edited: int,
    ) -> EffortRecord:
        """Persist a deliberately classified non-committed pilot attempt."""
        if self.effort_session.active:
            raise ValueError("Stop engineer effort timing before finalizing the pilot case")
        metrics_service, pilot_case_id = self._require_pilot_job(job_id)
        job = self._service.store.get_job(job_id)
        if job is None:
            raise ValueError("The bound pilot job does not exist")
        if job.state is JobState.COMMITTED:
            raise ValueError("Use successful pilot finalization for a committed job")
        return metrics_service.collect_failed_effort(
            record_id=f"effort_{uuid4().hex}",
            case_id=pilot_case_id,
            job_id=job_id,
            failure_reason=failure_reason,
            engineer_activity=self.effort_session.intervals,
            manual_fixup_minutes=self.effort_session.manual_fixup_minutes,
            entities_manually_edited=entities_manually_edited,
        )

    def refresh(self, job_id: str) -> ApprovalViewModel:
        """Read current service state, recompute compile evidence, and build the view."""
        job = self._service.store.get_job(job_id)
        if job is None:
            raise DocumentNotFoundError(
                "Approval job was not found",
                required_action="Open an existing harness job",
                details={"job_id": job_id},
            )
        spec = self._service.store.get_spec(job_id)
        if spec is None:
            raise ApprovalRequiredError(
                "Approval view requires a submitted specification",
                required_action="Submit the drawing specification before opening approval",
            )

        snapshot = self._service.inspect_document(job.document_id)
        plan = self._service.store.get_plan(job_id)
        terminal_job = job.state in {
            JobState.COMMITTED,
            JobState.FAILED,
            JobState.ROLLED_BACK,
            JobState.UNKNOWN_COMMIT_STATE,
        }
        sealed_raster = any(feature.type == "_accepted_raster_trace" for feature in spec.features)
        if (sealed_raster or terminal_job) and plan is not None:
            # The short-lived raster token is authority to create this immutable plan,
            # not a credential that must remain live throughout later human review.
            # Revalidate the stored hash instead of re-authorizing an expired token.
            if plan.plan_hash != job.plan_hash or not plan.verify_hash(str(job.plan_hash)):
                self._displayed_scope = None
                raise ApprovalRequiredError(
                    "Stored plan no longer matches the accepted job scope",
                    required_action="Reconcile the stored job before further action",
                )
            compilation = CompilationResult(
                plan=plan,
                missing_inputs=[],
                defaults_applied=list(spec.explicit_defaults),
                assumptions=list(spec.assumptions),
            )
        else:
            compilation = self._service.compiler.compile(
                spec,
                job_id=job_id,
                expected_revision=job.expected_revision,
            )
        if plan is not None and (
            compilation.plan is None or compilation.plan.plan_hash != plan.plan_hash
        ):
            self._displayed_scope = None
            raise ApprovalRequiredError(
                "Stored plan no longer matches the submitted specification",
                required_action="Submit the specification again and regenerate the preview",
            )
        report = self._service.store.get_validation(job_id)
        before = (
            PreviewArtifact(
                kind="active_document",
                artifact_ref=f"{snapshot.document_id}@{snapshot.revision}",
            ),
        )
        after: tuple[PreviewArtifact, ...] = ()
        semantic_diff = None
        if plan is not None:
            if terminal_job:
                # Reopening a terminal job must not drive the write state machine
                # through PREVIEWED again. Existing artifacts may have expired, but
                # the immutable plan and semantic diff remain valid review evidence.
                after = ()
            else:
                preview = self._service.preview(job_id)
                after = tuple(PreviewArtifact.model_validate(item) for item in preview["artifacts"])
            semantic_diff = build_semantic_diff(plan, snapshot)
            if plan.plan_hash is not None:
                self._displayed_scope = _DisplayedScope(
                    job_id=job_id,
                    plan_hash=plan.plan_hash,
                    expected_revision=job.expected_revision,
                )
        else:
            self._displayed_scope = None

        return build_approval_view_model(
            ApprovalViewInputs(
                job=job,
                spec=spec,
                plan=plan,
                current_revision=snapshot.revision,
                missing_inputs=tuple(compilation.missing_inputs),
                defaults_applied=tuple(compilation.defaults_applied),
                assumptions=tuple(compilation.assumptions),
                before_artifacts=before,
                after_artifacts=after,
                semantic_diff=semantic_diff,
                validation_report=report,
            )
        )

    def approve(
        self,
        *,
        approved_by: str,
        acknowledged_warning_rule_ids: frozenset[str],
    ) -> ApprovalOutcome:
        """Re-read mutable state, run the gate, then issue one scoped approval."""
        eligibility = self.eligibility(acknowledged_warning_rule_ids)
        if not eligibility.can_approve:
            return ApprovalOutcome(
                approved=False,
                reasons=eligibility.reasons,
            )

        scope = self._displayed_scope
        assert scope is not None
        approval_id, token = self._service.approve(
            scope.job_id,
            approved_by,
            tuple(sorted(acknowledged_warning_rule_ids)),
            displayed_plan_hash=scope.plan_hash,
            displayed_revision=scope.expected_revision,
        )
        self._approval_token = SecretStr(token)
        self._approved_scope = scope
        return ApprovalOutcome(approved=True, approval_id=approval_id)

    def eligibility(
        self,
        acknowledged_warning_rule_ids: frozenset[str],
    ) -> ApprovalEligibility:
        """Poll mutable plan/revision state without replacing the displayed preview."""
        scope = self._displayed_scope
        if scope is None:
            return ApprovalEligibility(can_approve=False, reasons=("preview_not_ready",))
        job = self._service.store.get_job(scope.job_id)
        plan = self._service.store.get_plan(scope.job_id)
        report = self._service.store.get_validation(scope.job_id)
        if job is None or plan is None or plan.plan_hash is None:
            return ApprovalEligibility(can_approve=False, reasons=("preview_not_ready",))
        if report is None:
            return ApprovalEligibility(can_approve=False, reasons=("validation_missing",))
        revision_is_current = self._service.validate_displayed_revision(
            job.document_id, scope.expected_revision
        )
        if (
            job.state not in {JobState.PREVIEWED, JobState.VALIDATED}
            and plan.plan_hash == scope.plan_hash
            and revision_is_current
        ):
            return ApprovalEligibility(can_approve=False, reasons=("job_not_approvable",))
        decision = can_approve(
            displayed_plan_hash=scope.plan_hash,
            current_plan_hash=plan.plan_hash,
            displayed_revision=scope.expected_revision,
            current_revision=(
                scope.expected_revision if revision_is_current else "revision_changed"
            ),
            report=report,
            acknowledged_warning_rule_ids=acknowledged_warning_rule_ids,
        )
        reasons = [reason.value for reason in decision.reasons]
        # HarnessService uses the stricter default policy where errors also block.
        # Reflect it here so the UI never enables an action the service must reject.
        if report.error_count and "error_findings" not in reasons:
            reasons.append("error_findings")
        return ApprovalEligibility(
            can_approve=decision.can_approve and report.error_count == 0,
            reasons=tuple(reasons),
        )

    def approval_scope_is_current(self) -> bool:
        """Return whether the in-memory credential still targets live service state."""
        scope = self._approved_scope
        if scope is None or self._approval_token is None:
            return False
        job = self._service.store.get_job(scope.job_id)
        plan = self._service.store.get_plan(scope.job_id)
        return bool(
            job is not None
            and plan is not None
            and plan.plan_hash == scope.plan_hash
            and job.expected_revision == scope.expected_revision
            and self._service.validate_displayed_revision(job.document_id, scope.expected_revision)
        )

    def commit_approved(self, *, idempotency_key: str) -> CommitResult:
        """Consume the private token without returning or rendering it."""
        token = self._approval_token
        scope = self._approved_scope
        if token is None or scope is None:
            raise ApprovalRequiredError(
                "No in-memory engineer approval is available",
                required_action="Review and approve the current preview",
            )
        try:
            return self._service.commit(
                scope.job_id,
                idempotency_key=idempotency_key,
                expected_revision=scope.expected_revision,
                plan_hash=scope.plan_hash,
                approval_token=token.get_secret_value(),
            )
        finally:
            self.clear_approval()

    def clear_approval(self) -> None:
        self._approval_token = None
        self._approved_scope = None

    def prepare_rollback(self, job_id: str) -> RollbackReviewScope:
        """Capture the exact destructive scope for display in a human-only surface."""
        self.clear_rollback_approval()
        scope = self._service.rollback_scope(job_id)
        reviewed = RollbackReviewScope(**scope)
        self._rollback_scope = reviewed
        return reviewed

    def rollback_is_available(self, job_id: str) -> bool:
        """Cheap UI predicate; issuance still re-reads and validates the live scope."""
        job = self._service.store.get_job(job_id)
        return bool(
            job is not None
            and job.state in {JobState.COMMITTED, JobState.FAILED}
            and job.checkpoint_id is not None
        )

    def approve_rollback(self, *, approved_by: str) -> ApprovalOutcome:
        """Issue and retain a rollback token only after a scope was displayed."""
        scope = self._rollback_scope
        if scope is None:
            return ApprovalOutcome(approved=False, reasons=("rollback_scope_not_reviewed",))
        approval, token = self._service.approve_rollback(
            scope.job_id,
            approved_by,
            displayed_checkpoint_id=scope.checkpoint_id,
            displayed_current_revision=scope.current_revision,
        )
        self._rollback_approval_token = SecretStr(token)
        return ApprovalOutcome(approved=True, approval_id=approval.approval_id)

    def rollback_approved(self) -> RollbackResult:
        """Consume the memory-only rollback credential for its displayed scope."""
        scope = self._rollback_scope
        token = self._rollback_approval_token
        if scope is None or token is None:
            raise ApprovalRequiredError(
                "No in-memory rollback approval is available",
                required_action="Review and approve the exact rollback checkpoint",
            )
        try:
            return self._service.rollback(
                scope.job_id,
                checkpoint_id=scope.checkpoint_id,
                current_revision=scope.current_revision,
                rollback_approval_token=token.get_secret_value(),
            )
        finally:
            self.clear_rollback_approval()

    def clear_rollback_approval(self) -> None:
        self._rollback_approval_token = None
        self._rollback_scope = None


__all__ = [
    "ApprovalEligibility",
    "ApprovalOutcome",
    "ApprovalReason",
    "EngineerDesktopController",
    "RollbackReviewScope",
]
