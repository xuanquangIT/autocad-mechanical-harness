"""Application facade orchestrating the whole workflow.

Every MCP tool is a thin wrapper over one method here. The state machine, the gates and
the audit trail live in this layer so the interface layer stays free of policy.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from cad_harness import __version__
from cad_harness.adapters.base import BaseAdapter
from cad_harness.application.services.plan_compiler import PlanCompilerService
from cad_harness.company_rules.loader import load_profile
from cad_harness.config import Settings
from cad_harness.diff.semantic_diff import build_semantic_diff
from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.errors import (
    ApprovalRequiredError,
    DocumentNotFoundError,
    IdempotencyKeyReusedError,
    PlanHashMismatchError,
    PostCommitValidationFailedError,
    StaleDocumentRevisionError,
)
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.document import DocumentSnapshot
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.job import CadJob, JobState
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.result import CommitResult, ExportResult, RollbackResult
from cad_harness.domain.models.validation import Severity, ValidationReport, ValidationStage
from cad_harness.domain.ports.autocad_adapter import (
    CommitRequest,
    ExportRequest,
    InspectRequest,
    RollbackRequest,
    SelectionRequest,
)
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.feature_catalog import registry
from cad_harness.observability.audit import AuditEventType, InMemoryAuditSink
from cad_harness.persistence.memory_store import InMemoryJobStore
from cad_harness.security.approval import issue_approval, verify_approval_token
from cad_harness.security.paths import ensure_path_allowed
from cad_harness.validation.engine import RuleContext, ValidationEngine, default_engine


class HarnessService:
    """Single entry point for jobs, previews, validation, approval and commit."""

    def __init__(
        self,
        settings: Settings,
        adapter: BaseAdapter,
        *,
        store: InMemoryJobStore | None = None,
        audit: InMemoryAuditSink | None = None,
        engine: ValidationEngine | None = None,
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        self.store = store or InMemoryJobStore()
        self.audit = audit or InMemoryAuditSink()
        self.engine = engine or default_engine()
        self.profile = load_profile(settings.standards.company_profile)
        self.tolerance = self.profile.tolerance()
        self.compiler = PlanCompilerService(self.profile, self.tolerance)
        #: document_id -> latest snapshot, so commit can re-verify without re-inspecting.
        self._snapshots: dict[str, DocumentSnapshot] = {}

    # ------------------------------------------------------------------ #
    # Read-only
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        adapter_status = self.adapter.status()
        return {
            "harness_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "adapter": adapter_status.model_dump(mode="json"),
            "profile": {
                "ref": self.profile.as_ref(),
                "company_approved": self.profile.company_approved,
            },
            "tolerance_profile": self.tolerance.as_ref(),
            "supported_features": registry.supported_types(),
            "validation_rules": self.engine.rule_ids(),
            "environment": self.settings.app.environment,
            "local_only": self.settings.app.local_only,
        }

    def inspect_document(self, document_id: str | None = None) -> DocumentSnapshot:
        snapshot = self.adapter.inspect_document(InspectRequest(document_id=document_id))
        self._snapshots[snapshot.document_id] = snapshot
        self.audit.append(
            event_type=AuditEventType.DOCUMENT_INSPECTED.value,
            job_id=None,
            actor_type="system",
            actor_id="harness",
            payload={"document_id": snapshot.document_id, "revision": snapshot.revision},
        )
        return snapshot

    def inspect_selection(self, document_id: str, max_entities: int = 200) -> dict[str, Any]:
        snapshot = self.adapter.inspect_selection(
            SelectionRequest(document_id=document_id, max_entities=max_entities)
        )
        return snapshot.model_dump(mode="json")

    def search_features(self, query: str = "") -> list[dict[str, Any]]:
        return registry.search(query)

    # ------------------------------------------------------------------ #
    # Job lifecycle
    # ------------------------------------------------------------------ #

    def create_job(self, document_id: str | None = None) -> CadJob:
        """Create a job and pin the revision it was planned against."""
        snapshot = self.inspect_document(document_id)
        job = CadJob(
            job_id=new_id(IdPrefix.JOB),
            document_id=snapshot.document_id,
            expected_revision=snapshot.revision,
        )
        self.store.save_job(job)
        self.audit.append(
            event_type=AuditEventType.JOB_CREATED.value,
            job_id=job.job_id,
            actor_type="system",
            actor_id="harness",
            payload={"document_id": job.document_id, "expected_revision": job.expected_revision},
        )
        return job

    def submit_spec(self, job_id: str, spec_payload: dict[str, Any]) -> dict[str, Any]:
        """Validate, normalize and compile a spec.

        A resubmission after approval sends the job back to ``SPEC_ACCEPTED`` and
        revokes the approval, because the approved plan no longer describes the change.
        """
        job = self._require_job(job_id)

        payload = dict(spec_payload)
        payload.setdefault("spec_id", new_id(IdPrefix.SPEC))
        payload.setdefault("document_id", job.document_id)
        payload.setdefault(
            "standard_profile",
            {"profile_id": self.profile.profile_id, "version": self.profile.version},
        )
        spec = DrawingSpec.model_validate(payload)

        if job.state not in {JobState.CREATED, JobState.SPEC_ACCEPTED}:
            job = job.transition_to(JobState.SPEC_ACCEPTED)
            self.store.revoke_approvals_for_job(job_id)
            job = job.invalidate_approval()
        elif job.state is JobState.CREATED:
            job = job.transition_to(JobState.SPEC_ACCEPTED)

        version = self.store.save_spec(job_id, spec)
        job = job.model_copy(update={"spec_id": spec.spec_id, "spec_version": version})
        self.store.save_job(job)
        self.audit.append(
            event_type=AuditEventType.SPEC_SUBMITTED.value,
            job_id=job_id,
            actor_type="ai_client",
            actor_id="unknown",
            payload={"spec_id": spec.spec_id, "spec_version": version},
        )

        result = self.compiler.compile(spec, job_id=job_id, expected_revision=job.expected_revision)
        if result.needs_input:
            return {
                "status": "needs_input",
                "job_id": job_id,
                "missing_inputs": [m.model_dump(mode="json") for m in result.missing_inputs],
            }

        assert result.plan is not None
        self.store.save_plan(result.plan)
        job = job.transition_to(
            JobState.PLANNED, plan_id=result.plan.plan_id, plan_hash=result.plan.plan_hash
        )
        self.store.save_job(job)
        self.audit.append(
            event_type=AuditEventType.PLAN_COMPILED.value,
            job_id=job_id,
            actor_type="system",
            actor_id="harness",
            payload={"plan_hash": result.plan.plan_hash, "operations": len(result.plan.operations)},
        )

        return {
            "status": "ok",
            "job_id": job_id,
            "plan_hash": result.plan.plan_hash,
            "operation_count": len(result.plan.operations),
            "defaults_applied": [d.model_dump(mode="json") for d in result.defaults_applied],
            "assumptions": [a.model_dump(mode="json") for a in result.assumptions],
        }

    # ------------------------------------------------------------------ #
    # Preview and validation
    # ------------------------------------------------------------------ #

    def preview(self, job_id: str) -> dict[str, Any]:
        """Generate preview artifacts. The live drawing is never touched here."""
        job = self._require_job(job_id)
        plan = self._require_plan(job_id)

        from cad_harness.adapters.dxf_preview import DxfPreviewAdapter

        # Preview always uses the file-based renderer, whatever the write adapter is.
        preview_adapter = DxfPreviewAdapter(Path(self.settings.storage.preview_directory))
        result = preview_adapter.preview(plan)
        gaps = preview_adapter.preview_gaps(plan)

        snapshot = self._snapshots.get(job.document_id)
        diff = build_semantic_diff(plan, snapshot) if snapshot else None

        if job.state is JobState.PLANNED:
            job = job.transition_to(JobState.PREVIEWED)
            self.store.save_job(job)

        self.audit.append(
            event_type=AuditEventType.PREVIEW_GENERATED.value,
            job_id=job_id,
            actor_type="system",
            actor_id="harness",
            payload={"plan_hash": plan.plan_hash, "artifacts": len(result.artifacts)},
        )

        return {
            "status": "ok",
            "job_id": job_id,
            "plan_hash": plan.plan_hash,
            "artifacts": [a.model_dump(mode="json") for a in result.artifacts],
            "semantic_diff": diff.to_dict() if diff else None,
            "unrenderable_operations": gaps,
        }

    def validate(self, job_id: str, stage: ValidationStage) -> ValidationReport:
        job = self._require_job(job_id)
        plan = self._require_plan(job_id)

        report = self.engine.run(
            stage,
            RuleContext(plan=plan, profile=self.profile, tolerance=self.tolerance),
            job_id=job_id,
        )
        self.store.save_validation(report)

        if (
            stage in {ValidationStage.PRE_COMMIT, ValidationStage.PREVIEW_GEOMETRY}
            and job.state is JobState.PREVIEWED
            and not report.has_blocking
        ):
            self.store.save_job(job.transition_to(JobState.VALIDATED))

        self.audit.append(
            event_type=AuditEventType.VALIDATION_COMPLETED.value,
            job_id=job_id,
            actor_type="system",
            actor_id="harness",
            payload={
                "stage": stage.value,
                "blocking": report.blocking_count,
                "errors": report.error_count,
                "warnings": report.warning_count,
            },
        )
        return report

    def get_diff(self, job_id: str) -> dict[str, Any]:
        job = self._require_job(job_id)
        plan = self._require_plan(job_id)
        snapshot = self._snapshots.get(job.document_id)
        if snapshot is None:
            raise DocumentNotFoundError(
                "No document snapshot available for this job",
                required_action="Run cad_document_inspect before requesting a diff",
            )
        return build_semantic_diff(plan, snapshot).to_dict()

    # ------------------------------------------------------------------ #
    # Approval (engineer surface: CLI / desktop / AutoCAD palette)
    # ------------------------------------------------------------------ #

    def approve(
        self, job_id: str, approved_by: str, warnings_acknowledged: tuple[str, ...] = ()
    ) -> tuple[str, str]:
        """Grant approval for the current plan. Returns ``(approval_id, token)``.

        Not an MCP tool: approval is a human action taken in a surface the AI client
        cannot drive.
        """
        job = self._require_job(job_id)
        plan = self._require_plan(job_id)
        report = self.store.get_validation(job_id)
        if report is None:
            raise ApprovalRequiredError(
                "Cannot approve a plan that has not been validated",
                required_action="Run cad_validate first",
            )
        if not report.gate_allows_commit():
            raise ApprovalRequiredError(
                "Validation findings block this plan",
                required_action="Resolve blocking and error findings, then re-validate",
                details={"blocking": report.blocking_count, "errors": report.error_count},
            )

        assert plan.plan_hash is not None
        approval, token = issue_approval(
            job_id=job_id,
            document_id=job.document_id,
            plan_hash=plan.plan_hash,
            expected_revision=job.expected_revision,
            approved_by=approved_by,
            secret=self.settings.approval_secret(),
            ttl=timedelta(minutes=self.settings.security.approval_ttl_minutes),
            warnings_acknowledged=warnings_acknowledged,
        )
        self.store.save_approval(approval)
        self.store.save_job(job.transition_to(JobState.APPROVED, approval_id=approval.approval_id))
        self.audit.append(
            event_type=AuditEventType.APPROVAL_GRANTED.value,
            job_id=job_id,
            actor_type="engineer",
            actor_id=approved_by,
            payload={"approval_id": approval.approval_id, "plan_hash": plan.plan_hash},
        )
        return approval.approval_id, token

    # ------------------------------------------------------------------ #
    # Commit
    # ------------------------------------------------------------------ #

    def commit(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        expected_revision: str,
        plan_hash: str,
        approval_token: str,
    ) -> CommitResult:
        """Commit an approved plan.

        Gate order is deliberate: idempotency, then hash, then approval, then a fresh
        revision check immediately before the write.
        """
        job = self._require_job(job_id)
        plan = self._require_plan(job_id)
        request_digest = sha256_of(
            {
                "job_id": job_id,
                "plan_hash": plan_hash,
                "expected_revision": expected_revision,
            }
        )

        # 1. Idempotent retry: replay, or reject a reused key with different content.
        previous = self.store.find_execution(job_id=job_id, idempotency_key=idempotency_key)
        if previous is not None:
            stored_digest, stored_result = previous
            if stored_digest != request_digest:
                raise IdempotencyKeyReusedError(
                    "Idempotency key was already used for a different request",
                    required_action="Generate a new idempotency key for this request",
                    details={"idempotency_key": idempotency_key},
                )
            return CommitResult.model_validate(stored_result)

        # 2. The submitted plan must be exactly the approved one.
        if plan.plan_hash != plan_hash:
            raise PlanHashMismatchError(
                "The approved preview does not match the submitted commit plan",
                required_action="Generate a new preview and request approval",
                details={"approved_plan_hash": plan.plan_hash, "submitted_plan_hash": plan_hash},
            )

        # 3. Approval must exist, be unexpired and cover this exact scope.
        if self.settings.security.require_commit_approval:
            approval = self.store.get_approval(job.approval_id) if job.approval_id else None
            if approval is None:
                raise ApprovalRequiredError(
                    "No valid approval recorded for this job",
                    required_action="Have an engineer approve the current preview",
                )
            verify_approval_token(
                approval_token,
                approval,
                self.settings.approval_secret(),
                job_id=job_id,
                plan_hash=plan_hash,
                expected_revision=expected_revision,
            )

        # 4. Re-check the revision as close to the write as possible.
        if not self.adapter.validate_revision(job.document_id, expected_revision):
            raise StaleDocumentRevisionError(
                "Document changed since the plan was approved",
                required_action="Re-inspect, regenerate preview, validate and approve again",
                details={"expected_revision": expected_revision},
            )

        job = job.transition_to(JobState.COMMITTING)
        self.store.save_job(job)
        self.audit.append(
            event_type=AuditEventType.COMMIT_STARTED.value,
            job_id=job_id,
            actor_type="system",
            actor_id="harness",
            payload={"plan_hash": plan_hash, "idempotency_key": idempotency_key},
        )

        try:
            result = self.adapter.commit(
                CommitRequest(
                    plan=plan,
                    idempotency_key=idempotency_key,
                    expected_revision=expected_revision,
                    approval_token=approval_token,
                    create_checkpoint=True,
                )
            )
        except Exception as exc:
            self.store.save_job(job.transition_to(JobState.FAILED))
            self.audit.append(
                event_type=AuditEventType.COMMIT_FAILED.value,
                job_id=job_id,
                actor_type="system",
                actor_id="harness",
                payload={"error": type(exc).__name__},
            )
            raise

        # 5. Read back and measure. A mismatch here is what makes commit trustworthy.
        post_report = self.engine.run(
            ValidationStage.POST_COMMIT,
            RuleContext(
                plan=plan,
                profile=self.profile,
                tolerance=self.tolerance,
                commit_result=result,
            ),
            job_id=job_id,
        )
        self.store.save_validation(post_report)

        if post_report.has_blocking:
            self.store.save_job(job.transition_to(JobState.FAILED))
            self.audit.append(
                event_type=AuditEventType.COMMIT_FAILED.value,
                job_id=job_id,
                actor_type="system",
                actor_id="harness",
                payload={
                    "reason": "post_commit_validation",
                    "findings": post_report.blocking_count,
                },
            )
            raise PostCommitValidationFailedError(
                "Committed geometry does not match the approved plan",
                required_action="Roll back to the checkpoint and re-plan",
                details={
                    "blocking_findings": [
                        finding.model_dump(mode="json")
                        for finding in post_report.findings
                        if finding.severity is Severity.BLOCKING
                    ],
                    "checkpoint_id": result.checkpoint_id,
                },
            )

        for entity in result.entity_results:
            self.store.map_entity(
                document_id=job.document_id,
                feature_id=entity.feature_id,
                operation_id=entity.operation_id,
                entity_ref=entity.entity_ref,
                revision=result.new_revision,
            )

        job = job.transition_to(
            JobState.COMMITTED,
            checkpoint_id=result.checkpoint_id,
            expected_revision=result.new_revision,
        )
        self.store.save_job(job)
        self.store.record_execution(
            job_id=job_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            result=result.model_dump(mode="json"),
        )
        self.audit.append(
            event_type=AuditEventType.COMMIT_SUCCEEDED.value,
            job_id=job_id,
            actor_type="system",
            actor_id="harness",
            payload={
                "plan_hash": plan_hash,
                "entities": len(result.entity_results),
                "new_revision": result.new_revision,
            },
        )
        return result

    # ------------------------------------------------------------------ #
    # Rollback and export
    # ------------------------------------------------------------------ #

    def rollback(self, job_id: str) -> RollbackResult:
        job = self._require_job(job_id)
        self.audit.append(
            event_type=AuditEventType.ROLLBACK_STARTED.value,
            job_id=job_id,
            actor_type="engineer",
            actor_id="unknown",
            payload={"checkpoint_id": job.checkpoint_id},
        )
        result = self.adapter.rollback(
            RollbackRequest(
                job_id=job_id,
                document_id=job.document_id,
                checkpoint_id=job.checkpoint_id,
            )
        )
        self.store.save_job(job.transition_to(JobState.ROLLED_BACK))
        self.audit.append(
            event_type=AuditEventType.ROLLBACK_SUCCEEDED.value,
            job_id=job_id,
            actor_type="system",
            actor_id="harness",
            payload={"restored_revision": result.restored_revision, "method": result.method},
        )
        return result

    def export(
        self, document_id: str, target_path: str, export_format: str, *, overwrite: bool = False
    ) -> ExportResult:
        resolved = ensure_path_allowed(
            Path(target_path),
            self.settings.security.export_path_allowlist,
            allow_arbitrary=self.settings.security.allow_arbitrary_export_path,
            overwrite=overwrite,
        )
        result = self.adapter.export(
            ExportRequest(
                document_id=document_id,
                format=export_format,
                target_path=str(resolved),
                overwrite=overwrite,
            )
        )
        self.audit.append(
            event_type=AuditEventType.EXPORT_CREATED.value,
            job_id=None,
            actor_type="engineer",
            actor_id="unknown",
            payload={"format": export_format, "target_path": str(resolved)},
        )
        return result

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _require_job(self, job_id: str) -> CadJob:
        job = self.store.get_job(job_id)
        if job is None:
            raise DocumentNotFoundError(
                f"Unknown job '{job_id}'",
                required_action="Create a job with cad_job_create first",
            )
        return job

    def _require_plan(self, job_id: str) -> OperationPlan:
        plan = self.store.get_plan(job_id)
        if plan is None:
            raise ApprovalRequiredError(
                "No compiled plan for this job",
                required_action="Submit a spec with cad_spec_submit first",
            )
        return plan
