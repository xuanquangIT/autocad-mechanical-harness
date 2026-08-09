"""Application facade orchestrating the whole workflow.

Every MCP tool is a thin wrapper over one method here. The state machine, the gates and
the audit trail live in this layer so the interface layer stays free of policy.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import timedelta
from pathlib import Path
from typing import Any

from cad_harness import __version__
from cad_harness.adapters.base import BaseAdapter
from cad_harness.application.services.lease_service import LeaseService
from cad_harness.application.services.plan_compiler import PlanCompilerService
from cad_harness.application.services.raster_trace_service import RasterTraceService
from cad_harness.application.services.remediation_service import (
    RemediationResult,
    RemediationService,
)
from cad_harness.company_rules.loader import load_profile
from cad_harness.compatibility import CompatibilityMatrix, load_compatibility_matrix
from cad_harness.config import Settings
from cad_harness.diff.semantic_diff import build_semantic_diff
from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.errors import (
    AdapterCapabilityMissingError,
    ApprovalRequiredError,
    DocumentNotFoundError,
    IdempotencyKeyReusedError,
    PlanHashMismatchError,
    PostCommitValidationFailedError,
    RollbackNotAvailableError,
    StaleDocumentRevisionError,
    UnknownCommitStateError,
)
from cad_harness.domain.models.approval import RollbackApprovalRecord
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.document import DocumentSnapshot
from cad_harness.domain.models.drawing_model import DrawingModel
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.job import CadJob, JobState
from cad_harness.domain.models.operation_plan import OperationPlan, OperationType
from cad_harness.domain.models.result import (
    Checkpoint,
    CommitResult,
    EntityMappingRecord,
    ExportResult,
    RollbackResult,
)
from cad_harness.domain.models.validation import Severity, ValidationReport, ValidationStage
from cad_harness.domain.ports.autocad_adapter import (
    CommitRequest,
    ExportRequest,
    InspectRequest,
    RollbackRequest,
    SelectionRequest,
)
from cad_harness.domain.ports.repositories import AuditSink, DrawingAuditStore, JobStore
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.feature_catalog import registry
from cad_harness.metrics.collector import OperationName
from cad_harness.metrics.recorder import OperationMeasurement, OperationMetricsRecorder
from cad_harness.observability.audit import AuditEventType, InMemoryAuditSink
from cad_harness.observability.logging import get_logger
from cad_harness.persistence.memory_lease_store import InMemoryLeaseStore
from cad_harness.persistence.memory_store import InMemoryJobStore
from cad_harness.security.approval import issue_approval, verify_approval_token
from cad_harness.security.paths import ensure_path_allowed
from cad_harness.security.rollback_approval import (
    issue_rollback_approval,
    verify_rollback_approval_token,
)
from cad_harness.validation.engine import RuleContext, ValidationEngine, default_engine


class HarnessService:
    """Single entry point for jobs, previews, validation, approval and commit."""

    def __init__(
        self,
        settings: Settings,
        adapter: BaseAdapter,
        *,
        store: JobStore | None = None,
        audit: AuditSink | None = None,
        engine: ValidationEngine | None = None,
        lease_service: LeaseService | None = None,
        drawing_model_reader: Callable[[str], DrawingModel] | None = None,
        drawing_audit_store: DrawingAuditStore | None = None,
        operation_metrics: OperationMetricsRecorder | None = None,
        compatibility_matrix: CompatibilityMatrix | None = None,
        retention_cleanup: Callable[[], Any] | None = None,
        raster_trace_service: RasterTraceService | None = None,
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        #: Port, not a concrete store: the wiring layer decides which implementation runs.
        #: The in-memory fallback keeps the development default working; production wiring
        #: passes the SQLite-backed store explicitly.
        self.store: JobStore = store or InMemoryJobStore()
        self.adapter.bind_job_store(self.store)
        self.audit = audit or InMemoryAuditSink()
        self.engine = engine or default_engine()
        self.lease_service = lease_service or LeaseService(
            InMemoryLeaseStore(),
            ttl_seconds=settings.lease.ttl_seconds,
            heartbeat_interval_seconds=settings.lease.heartbeat_interval_seconds,
            minimum_remaining_seconds=settings.lease.minimum_remaining_seconds,
        )
        self.profile = load_profile(settings.standards.company_profile)
        self.tolerance = self.profile.tolerance()
        self.compiler = PlanCompilerService(
            self.profile,
            self.tolerance,
            self.adapter,
            raster_trace_service=raster_trace_service,
        )
        self._drawing_model_reader = drawing_model_reader
        self._drawing_audit_store = drawing_audit_store
        self._operation_metrics = operation_metrics
        self._compatibility_matrix = compatibility_matrix or load_compatibility_matrix()
        self._retention_cleanup = retention_cleanup
        self._remediation_jobs: dict[str, RemediationResult] = {}
        #: document_id -> latest snapshot, so commit can re-verify without re-inspecting.
        self._snapshots: dict[str, DocumentSnapshot] = {}
        #: job_id -> (undo_group, exact post-commit revision).  Undo-group rollback is
        #: intentionally process-local: a restarted bridge cannot prove that the same
        #: AutoCAD undo entry is still active, so no receipt is reconstructed from SQLite.
        self._undo_rollback_receipts: dict[str, tuple[str, str]] = {}

    def _measure(
        self, operation_name: OperationName
    ) -> AbstractContextManager[OperationMeasurement]:
        if self._operation_metrics is None:
            return nullcontext(OperationMeasurement())
        return self._operation_metrics.measure(operation_name)

    def _require_writer_compatible(self) -> None:
        """Fail closed at every drawing-mutation boundary for live adapters."""
        adapter_status = self.adapter.status()
        if adapter_status.adapter_type in {"com", "dotnet_bridge"}:
            self._compatibility_matrix.require_writer_compatible(adapter_status)

    def _apply_retention(self) -> None:
        """Run bounded best-effort cleanup without masking the primary operation."""
        if self._retention_cleanup is None:
            return
        try:
            self._retention_cleanup()
        except Exception as exc:  # cleanup failure must not replace CAD operation outcome
            get_logger(__name__).warning("retention_cleanup_failed", error_type=type(exc).__name__)

    # ------------------------------------------------------------------ #
    # Read-only
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        self._apply_retention()
        adapter_status = self.adapter.status()
        if adapter_status.adapter_type in {"com", "dotnet_bridge"}:
            adapter_status = self._compatibility_matrix.evaluate_status(adapter_status)
        writer_targets = tuple(
            target for target in self._compatibility_matrix.targets if target.writer_supported
        )
        verified_targets = tuple(
            target.display_version
            for target in writer_targets
            if target.verification_status.value == "verified"
        )
        if writer_targets and len(verified_targets) == len(writer_targets):
            verification_status = "verified"
        elif verified_targets:
            verification_status = "mixed"
        else:
            verification_status = "provisional"
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
            "compatibility": {
                "bridge_bundle_version": self._compatibility_matrix.bridge_bundle_version,
                "supported_versions": self._compatibility_matrix.supported_versions,
                "verification_status": verification_status,
                "verified_targets": list(verified_targets),
            },
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
        prior_spec_version = job.spec_version

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
        if prior_spec_version is not None:
            self.audit.append(
                event_type=AuditEventType.SPEC_CHANGED.value,
                job_id=job_id,
                actor_type="ai_client",
                actor_id="unknown",
                payload={
                    "previous_spec_version": prior_spec_version,
                    "spec_version": version,
                    "spec_id": spec.spec_id,
                },
            )
        self.audit.append(
            event_type=AuditEventType.SPEC_SUBMITTED.value,
            job_id=job_id,
            actor_type="ai_client",
            actor_id="unknown",
            payload={"spec_id": spec.spec_id, "spec_version": version},
        )

        with self._measure("compile") as metric:
            result = self.compiler.compile(
                spec, job_id=job_id, expected_revision=job.expected_revision
            )
            metric.entity_count = len(result.plan.operations) if result.plan is not None else 0
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

    def register_remediation_plan(self, remediation: RemediationResult) -> dict[str, Any]:
        """Attach a selected-finding plan at the normal pipeline's PLANNED gate."""
        plan = remediation.plan
        job = self._require_job(plan.job_id)
        if self._drawing_model_reader is None:
            raise AdapterCapabilityMissingError(
                "A remediation workflow requires post-commit structured drawing readback",
                required_action="Configure a DrawingModel reader before registering remediation",
                details={"missing_capability": "drawing_model_readback"},
            )
        if self._drawing_audit_store is None:
            raise AdapterCapabilityMissingError(
                "A remediation workflow requires persisted drawing-audit evidence",
                required_action="Configure the DrawingAuditStore used by DrawingAuditService",
                details={"missing_capability": "drawing_audit_store"},
            )
        evidence = self._drawing_audit_store.get_drawing_audit(remediation.audit_id)
        if evidence is None:
            raise DocumentNotFoundError(
                "The remediation plan's persisted audit evidence does not exist",
                required_action="Run and persist a new drawing audit before remediation",
                details={"audit_id": remediation.audit_id},
            )
        report_refs = {
            (finding.rule_id, finding.entity_ref)
            for finding in evidence.report.findings
            if finding.entity_ref is not None
        }
        if (
            evidence.document_id != plan.document_id
            or evidence.revision != plan.expected_revision
            or evidence.report.profile_ref != plan.profile_ref
            or evidence.report.profile_ref != self.profile.as_ref()
            or any(item not in report_refs for item in remediation.selected_findings)
        ):
            raise StaleDocumentRevisionError(
                "The remediation plan does not match its persisted audit evidence",
                required_action="Compile a new remediation plan from the persisted current audit",
                details={"audit_id": remediation.audit_id},
            )
        if plan.document_id != job.document_id or plan.expected_revision != job.expected_revision:
            raise StaleDocumentRevisionError(
                "The remediation plan is not pinned to the job's inspected document revision",
                required_action="Create a new job from the audited drawing revision",
                details={
                    "job_document_id": job.document_id,
                    "plan_document_id": plan.document_id,
                    "job_expected_revision": job.expected_revision,
                    "plan_expected_revision": plan.expected_revision,
                },
            )
        if job.state is not JobState.CREATED:
            raise StaleDocumentRevisionError(
                "A remediation plan can only be registered on a fresh job",
                required_action="Create a new job and compile remediation from the current audit",
                details={"job_state": job.state.value},
            )
        current_model = self._drawing_model_reader(job.document_id)
        trusted = RemediationService(self.tolerance, self._drawing_audit_store).compile_plan(
            job_id=job.job_id,
            model=current_model,
            audit_id=remediation.audit_id,
            selected_rule_findings=remediation.selected_findings,
            technical_inputs=remediation.technical_inputs,
        )
        # Registration never trusts a caller-provided plan body. It recompiles from
        # persisted audit evidence and the freshly read structured drawing.
        remediation = trusted
        plan = trusted.plan
        self.compiler.preflight(plan)
        accepted = job.transition_to(JobState.SPEC_ACCEPTED)
        self.audit.append(
            event_type=AuditEventType.SPEC_SUBMITTED.value,
            job_id=job.job_id,
            actor_type="ai_client",
            actor_id="unknown",
            payload={
                "kind": "remediation_selection",
                "selected_findings": len(remediation.selected_findings),
            },
        )
        self.store.save_plan(plan)
        planned = accepted.transition_to(
            JobState.PLANNED,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
        )
        self.store.save_job(planned)
        self._remediation_jobs[job.job_id] = remediation
        self.audit.append(
            event_type=AuditEventType.PLAN_COMPILED.value,
            job_id=job.job_id,
            actor_type="system",
            actor_id="harness",
            payload={
                "plan_hash": plan.plan_hash,
                "operations": len(plan.operations),
                "kind": "remediation",
            },
        )
        return {
            "status": "ok",
            "job_id": job.job_id,
            "plan_hash": plan.plan_hash,
            "operation_count": len(plan.operations),
            "selected_finding_count": len(remediation.selected_findings),
        }

    # ------------------------------------------------------------------ #
    # Preview and validation
    # ------------------------------------------------------------------ #

    def preview(self, job_id: str) -> dict[str, Any]:
        """Measure and render one end-to-end preview operation."""
        try:
            with self._measure("preview") as metric:
                result = self._preview(job_id)
                diff = result.get("semantic_diff") or {}
                metric.entity_count = len(diff.get("entries", ()))
                return result
        finally:
            # Enforce quota/TTL immediately after any artifact creation attempt.
            self._apply_retention()

    def _preview(self, job_id: str) -> dict[str, Any]:
        """Generate preview artifacts. The live drawing is never touched here."""
        job = self._require_job(job_id)
        plan = self._require_plan(job_id)
        self.compiler.preflight(plan)

        from cad_harness.adapters.dxf_preview import DxfPreviewAdapter

        # Preview always uses the file-based renderer, whatever the write adapter is.
        preview_adapter = DxfPreviewAdapter(
            Path(self.settings.storage.preview_directory),
            company_approved=self.profile.company_approved,
        )
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
            "company_approved": result.company_approved,
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

    def validate_displayed_revision(self, document_id: str, revision: str) -> bool:
        """Check a desktop scope without creating inspection/audit side effects."""
        return self.adapter.validate_revision(document_id, revision)

    def approve(
        self,
        job_id: str,
        approved_by: str,
        warnings_acknowledged: tuple[str, ...] = (),
        *,
        displayed_plan_hash: str | None = None,
        displayed_revision: str | None = None,
    ) -> tuple[str, str]:
        """Grant approval for the current plan. Returns ``(approval_id, token)``.

        Not an MCP tool: approval is a human action taken in a surface the AI client
        cannot drive.
        """
        job = self._require_job(job_id)
        plan = self._require_plan(job_id)
        if displayed_plan_hash is not None and plan.plan_hash != displayed_plan_hash:
            raise PlanHashMismatchError(
                "The plan changed after the approval screen was opened",
                required_action="Regenerate the preview and review the new plan",
                details={
                    "displayed_plan_hash": displayed_plan_hash,
                    "current_plan_hash": plan.plan_hash,
                },
            )
        if displayed_revision is not None and (
            job.expected_revision != displayed_revision
            or not self.adapter.validate_revision(job.document_id, displayed_revision)
        ):
            raise StaleDocumentRevisionError(
                "The drawing revision changed after the approval screen was opened",
                required_action="Regenerate the preview from the current drawing revision",
                details={"displayed_revision": displayed_revision},
            )
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

        warning_rule_ids = {
            finding.rule_id for finding in report.findings if finding.severity is Severity.WARNING
        }
        acknowledged = set(warnings_acknowledged)
        missing_warning_acknowledgements = sorted(warning_rule_ids - acknowledged)
        if missing_warning_acknowledgements:
            raise ApprovalRequiredError(
                "Every validation warning must be acknowledged before approval",
                required_action="Acknowledge each listed warning rule in the engineer desktop",
                details={"missing_warning_rule_ids": missing_warning_acknowledgements},
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
            warnings_acknowledged=tuple(sorted(acknowledged)),
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
        """Measure and execute one end-to-end commit operation."""
        try:
            with self._measure("commit") as metric:
                result = self._commit(
                    job_id,
                    idempotency_key=idempotency_key,
                    expected_revision=expected_revision,
                    plan_hash=plan_hash,
                    approval_token=approval_token,
                )
                metric.entity_count = len(result.entity_results)
                return result
        finally:
            # A live adapter may have created a checkpoint even if post-validation fails.
            self._apply_retention()

    def _commit(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        expected_revision: str,
        plan_hash: str,
        approval_token: str,
    ) -> CommitResult:
        """Commit an approved plan.

        Gate order is deliberate: idempotency conflict detection, then exact plan and
        approval authorization, then replay or a fresh revision check before a write.

        A successful replay must not query the adapter because the original commit has
        already advanced the drawing revision.  It must still authenticate the caller:
        possession of an idempotency key alone is not authority to retrieve a receipt.
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
        # 1. Detect a conflicting reuse before checking authorization.  Do not disclose
        # a prior receipt for either a mismatched digest or an unauthorised exact retry.
        previous = self.store.find_execution(job_id=job_id, idempotency_key=idempotency_key)
        if previous is not None:
            stored_digest, _ = previous
            if stored_digest != request_digest:
                raise IdempotencyKeyReusedError(
                    "Idempotency key was already used for a different request",
                    required_action="Generate a new idempotency key for this request",
                    details={"idempotency_key": idempotency_key},
                )
        # 2. The submitted plan must be exactly the stored one, including on replay.
        if plan.plan_hash != plan_hash:
            raise PlanHashMismatchError(
                "The approved preview does not match the submitted commit plan",
                required_action="Generate a new preview and request approval",
                details={"approved_plan_hash": plan.plan_hash, "submitted_plan_hash": plan_hash},
            )

        # 3. Approval must exist, have a valid signature, remain unexpired, and cover
        # this exact scope.  This gate also applies to receipt replay so an idempotency
        # key cannot become a bearer credential.  Explicit approval-disabled test/dev
        # configurations retain their configured behaviour.
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

        if previous is not None:
            _, stored_result = previous
            return CommitResult.model_validate(stored_result)

        if job.state is JobState.UNKNOWN_COMMIT_STATE:
            raise UnknownCommitStateError(
                "This job has an unknown commit outcome and cannot be committed automatically",
                required_action="Reconcile stored mappings against the drawing; do not retry",
                details={"job_id": job_id, "document_id": job.document_id},
            )

        self._require_writer_compatible()

        # 4. Re-read the latest validation. Approval cannot freeze or bypass a later
        # blocking report, and a report for another plan/stage is not commit evidence.
        report = self.store.get_validation(job_id)
        if (
            report is None
            or report.stage is not ValidationStage.PRE_COMMIT
            or report.plan_hash != plan_hash
            or not report.gate_allows_commit()
        ):
            raise ApprovalRequiredError(
                "Current validation evidence does not allow this commit",
                required_action="Resolve findings, re-validate and approve the exact current plan",
                details={
                    "validation_present": report is not None,
                    "stage": report.stage.value if report is not None else None,
                    "blocking": report.blocking_count if report is not None else None,
                    "errors": report.error_count if report is not None else None,
                },
            )

        # 5. Re-check the revision as close to the write as possible.
        if not self.adapter.validate_revision(job.document_id, expected_revision):
            raise StaleDocumentRevisionError(
                "Document changed since the plan was approved",
                required_action="Re-inspect, regenerate preview, validate and approve again",
                details={"expected_revision": expected_revision},
            )

        with self.lease_service.hold(job.document_id, owner_id=job_id) as lease:
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
                if result.undo_group is not None:
                    self._undo_rollback_receipts[job_id] = (
                        result.undo_group,
                        result.new_revision,
                    )
            except Exception as exc:
                now = self.lease_service.clock()
                lease_unknown = not lease.is_valid_at(now)
                outcome_unknown = isinstance(exc, UnknownCommitStateError) or lease_unknown
                target = JobState.UNKNOWN_COMMIT_STATE if outcome_unknown else JobState.FAILED
                terminal = job.transition_to(target)
                released = self.store.finalize_job(
                    terminal,
                    lease_id=lease.lease.lease_id,
                    now=now,
                )
                if released:
                    lease.mark_released()
                self.audit.append(
                    event_type=AuditEventType.COMMIT_FAILED.value,
                    job_id=job_id,
                    actor_type="system",
                    actor_id="harness",
                    payload={"error": type(exc).__name__, "outcome": target.value},
                )
                if lease_unknown and not isinstance(exc, UnknownCommitStateError):
                    raise UnknownCommitStateError(
                        "Writer lease was lost before the commit outcome was known",
                        required_action=(
                            "Reconcile stored mappings against the drawing; do not retry"
                        ),
                        details={"job_id": job_id, "document_id": job.document_id},
                    ) from exc
                raise

            # Read back while ownership is still held. A mismatch is a failed commit,
            # but its terminal state and lease release still share one transaction.
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
            operation_types = {
                operation.operation_id: operation.type for operation in plan.operations
            }
            mappings = tuple(
                EntityMappingRecord(
                    document_id=job.document_id,
                    feature_id=entity.feature_id,
                    operation_id=entity.operation_id,
                    entity_ref=entity.entity_ref,
                    last_revision=result.new_revision,
                )
                for entity in result.entity_results
                if operation_types.get(entity.operation_id) is not OperationType.DELETE_ENTITY
            )
            checkpoint = (
                Checkpoint(
                    checkpoint_id=result.checkpoint_id,
                    job_id=job_id,
                    revision=result.previous_revision,
                    artifact_ref=f"adapter-checkpoint://{result.checkpoint_id}",
                )
                if result.checkpoint_id is not None
                else None
            )

            if post_report.has_blocking:
                terminal = job.transition_to(
                    JobState.FAILED,
                    checkpoint_id=result.checkpoint_id,
                    expected_revision=result.new_revision,
                )
                released = self.store.finalize_commit(
                    terminal,
                    lease_id=lease.lease.lease_id,
                    now=self.lease_service.clock(),
                    mappings=mappings,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    result=result.model_dump(mode="json"),
                    checkpoint=checkpoint,
                )
                if released:
                    lease.mark_released()
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

            remediation = self._remediation_jobs.get(job_id)
            if remediation is not None:
                selected_set = set(remediation.selected_findings)
                readback_error: str | None = None
                try:
                    assert self._drawing_model_reader is not None
                    # Full structured readback is deliberate: replacements and adjacent
                    # geometry can introduce defects outside the original selected refs.
                    readback = self._drawing_model_reader(job.document_id)
                    if readback.revision != result.new_revision:
                        raise StaleDocumentRevisionError(
                            "Post-remediation readback did not match the committed revision",
                            details={
                                "committed_revision": result.new_revision,
                                "readback_revision": readback.revision,
                            },
                        )
                    from cad_harness.comprehension.auditor import audit_drawing

                    remediation_report = audit_drawing(
                        readback,
                        profile=self.profile,
                        tolerance=self.tolerance,
                        job_id=job_id,
                    )
                    self.store.save_validation(remediation_report)
                    current_set = {
                        (finding.rule_id, finding.entity_ref)
                        for finding in remediation_report.findings
                    }
                    remaining = tuple(
                        item for item in remediation.selected_findings if item in current_set
                    )
                    assert self._drawing_audit_store is not None
                    baseline = self._drawing_audit_store.get_drawing_audit(remediation.audit_id)
                    if baseline is None:
                        raise DocumentNotFoundError("Persisted remediation audit disappeared")
                    baseline_set = {
                        (finding.rule_id, finding.entity_ref)
                        for finding in baseline.report.findings
                    }
                    introduced = tuple(
                        sorted(
                            current_set - baseline_set,
                            key=lambda item: (item[0], item[1] or ""),
                        )
                    )
                except Exception as exc:  # commit happened; unreadable outcome must fail safe
                    remaining = remediation.selected_findings
                    introduced = ()
                    readback_error = type(exc).__name__
                resolved = tuple(
                    item for item in remediation.selected_findings if item not in remaining
                )
                self.audit.append(
                    event_type=AuditEventType.DRAWING_AUDITED.value,
                    job_id=job_id,
                    actor_type="system",
                    actor_id="harness",
                    payload={
                        "document_id": job.document_id,
                        "revision": result.new_revision,
                        "selected_count": len(selected_set),
                        "resolved_count": len(resolved),
                        "remaining_count": len(remaining),
                        "introduced_count": len(introduced),
                        "readback_error": readback_error,
                    },
                )
                if remaining or introduced:
                    terminal = job.transition_to(
                        JobState.FAILED,
                        checkpoint_id=result.checkpoint_id,
                        expected_revision=result.new_revision,
                    )
                    released = self.store.finalize_commit(
                        terminal,
                        lease_id=lease.lease.lease_id,
                        now=self.lease_service.clock(),
                        mappings=mappings,
                        idempotency_key=idempotency_key,
                        request_digest=request_digest,
                        result=result.model_dump(mode="json"),
                        checkpoint=checkpoint,
                    )
                    if released:
                        lease.mark_released()
                    self.audit.append(
                        event_type=AuditEventType.COMMIT_FAILED.value,
                        job_id=job_id,
                        actor_type="system",
                        actor_id="harness",
                        payload={
                            "reason": "remediation_reaudit",
                            "remaining_findings": len(remaining),
                            "introduced_findings": len(introduced),
                            "readback_error": readback_error,
                        },
                    )
                    raise PostCommitValidationFailedError(
                        "One or more selected audit findings remain after remediation",
                        required_action="Roll back to the checkpoint, audit again and re-plan",
                        details={
                            "resolved_findings": [list(item) for item in resolved],
                            "remaining_findings": [list(item) for item in remaining],
                            "introduced_findings": [list(item) for item in introduced],
                            "remaining_rule_ids": sorted(
                                {item[0] for item in (*remaining, *introduced)}
                            ),
                            "checkpoint_id": result.checkpoint_id,
                            "readback_error": readback_error,
                        },
                    )

            now = self.lease_service.clock()
            if not lease.is_valid_at(now):
                terminal = job.transition_to(JobState.UNKNOWN_COMMIT_STATE)
                released = self.store.finalize_job(
                    terminal,
                    lease_id=lease.lease.lease_id,
                    now=now,
                    mappings=mappings,
                )
                if released:
                    lease.mark_released()
                raise UnknownCommitStateError(
                    "Writer lease expired or could not be renewed while commit was running",
                    required_action="Reconcile stored mappings against the drawing; do not retry",
                    details={"job_id": job_id, "document_id": job.document_id},
                )

            committed = job.transition_to(
                JobState.COMMITTED,
                checkpoint_id=result.checkpoint_id,
                expected_revision=result.new_revision,
            )
            released = self.store.finalize_commit(
                committed,
                lease_id=lease.lease.lease_id,
                now=now,
                mappings=mappings,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                result=result.model_dump(mode="json"),
                checkpoint=checkpoint,
            )
            if released:
                lease.mark_released()
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

    def rollback_scope(self, job_id: str) -> dict[str, str]:
        """Return the exact current scope a human must review before rollback."""
        job = self._require_job(job_id)
        if job.state not in {JobState.COMMITTED, JobState.FAILED} or job.checkpoint_id is None:
            raise RollbackNotAvailableError(
                "This job has no proven post-commit checkpoint to restore",
                required_action="Reconcile the commit outcome or choose a committed job checkpoint",
                details={"job_state": job.state.value},
            )
        undo_receipt = self._undo_rollback_receipts.get(job_id)
        current_revision = (
            undo_receipt[1]
            if undo_receipt is not None
            else self.inspect_document(job.document_id).revision
        )
        return {
            "job_id": job.job_id,
            "document_id": job.document_id,
            "checkpoint_id": job.checkpoint_id,
            "current_revision": current_revision,
        }

    def approve_rollback(
        self,
        job_id: str,
        approved_by: str,
        *,
        displayed_checkpoint_id: str,
        displayed_current_revision: str,
    ) -> tuple[RollbackApprovalRecord, str]:
        """Human-only issuance boundary; no MCP tool calls this method."""
        scope = self.rollback_scope(job_id)
        if (
            scope["checkpoint_id"] != displayed_checkpoint_id
            or scope["current_revision"] != displayed_current_revision
        ):
            raise StaleDocumentRevisionError(
                "The rollback scope changed after it was displayed",
                required_action="Review the current revision and checkpoint again",
                details={
                    "displayed_checkpoint_id": displayed_checkpoint_id,
                    "current_checkpoint_id": scope["checkpoint_id"],
                    "displayed_revision": displayed_current_revision,
                    "current_revision": scope["current_revision"],
                },
            )
        approval, token = issue_rollback_approval(
            job_id=scope["job_id"],
            document_id=scope["document_id"],
            checkpoint_id=scope["checkpoint_id"],
            current_revision=scope["current_revision"],
            approved_by=approved_by,
            secret=self.settings.approval_secret(),
            ttl=timedelta(minutes=self.settings.security.rollback_approval_ttl_minutes),
        )
        self.audit.append(
            event_type=AuditEventType.ROLLBACK_APPROVAL_GRANTED.value,
            job_id=job_id,
            actor_type="engineer",
            actor_id=approved_by,
            payload={
                "approval_id": approval.approval_id,
                "document_id": approval.document_id,
                "checkpoint_id": approval.checkpoint_id,
                "current_revision": approval.current_revision,
                "expires_at": approval.expires_at.isoformat(),
            },
        )
        return approval, token

    def rollback(
        self,
        job_id: str,
        *,
        checkpoint_id: str,
        current_revision: str,
        rollback_approval_token: str,
    ) -> RollbackResult:
        job = self._require_job(job_id)
        # Authenticate the caller-supplied scope before disclosing whether this job
        # has a checkpoint or which checkpoint the server recorded.
        approval = verify_rollback_approval_token(
            rollback_approval_token,
            self.settings.approval_secret(),
            job_id=job.job_id,
            document_id=job.document_id,
            checkpoint_id=checkpoint_id,
            current_revision=current_revision,
        )
        if job.state not in {JobState.COMMITTED, JobState.FAILED} or job.checkpoint_id is None:
            raise RollbackNotAvailableError(
                "This job has no proven post-commit checkpoint to restore",
                required_action="Reconcile the commit outcome or choose a committed job checkpoint",
                details={"job_state": job.state.value},
            )
        if job.checkpoint_id != checkpoint_id:
            raise RollbackNotAvailableError(
                "The requested checkpoint is not the checkpoint recorded for this job",
                required_action="Review and approve the job's exact checkpoint",
                details={"job_checkpoint_id": job.checkpoint_id},
            )
        undo_receipt = self._undo_rollback_receipts.get(job_id)
        if undo_receipt is not None and undo_receipt[1] != current_revision:
            raise StaleDocumentRevisionError(
                "The session undo receipt does not match the approved revision",
                required_action="Review the current rollback scope again",
                details={"approved_revision": current_revision},
            )
        # A process-local undo receipt was created only after the same service already
        # passed the live writer compatibility gate.  Calling status/validate_revision
        # here would itself create an AutoCAD command and invalidate the one-step undo
        # fence, so the bridge performs the revision check atomically with rollback.
        if undo_receipt is None:
            self._require_writer_compatible()
        with self.lease_service.hold(job.document_id, owner_id=f"rollback:{job_id}"):
            if undo_receipt is None and not self.adapter.validate_revision(
                job.document_id, current_revision
            ):
                raise StaleDocumentRevisionError(
                    "Document changed after rollback approval",
                    required_action="Review the current revision and approve rollback again",
                    details={"approved_revision": current_revision},
                )
            self.audit.append(
                event_type=AuditEventType.ROLLBACK_STARTED.value,
                job_id=job_id,
                actor_type="engineer",
                actor_id=approval.approved_by,
                payload={
                    "approval_id": approval.approval_id,
                    "checkpoint_id": checkpoint_id,
                    "current_revision": current_revision,
                },
            )
            result = self.adapter.rollback(
                RollbackRequest(
                    job_id=job_id,
                    document_id=job.document_id,
                    checkpoint_id=checkpoint_id,
                    current_revision=current_revision,
                    rollback_approval_token=rollback_approval_token,
                    undo_group=undo_receipt[0] if undo_receipt is not None else None,
                )
            )
            self.store.save_job(job.transition_to(JobState.ROLLED_BACK))
            self._undo_rollback_receipts.pop(job_id, None)
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
        self._require_writer_compatible()
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
