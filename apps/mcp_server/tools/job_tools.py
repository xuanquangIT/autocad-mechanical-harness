"""Job and specification orchestration tools. These never modify the live drawing."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context as McpContext
from mcp.server.fastmcp import FastMCP
from pydantic import Field, ValidationError

from apps.mcp_server.context import ServerContext, failure, ok
from apps.mcp_server.tools.permissions import ToolPermissionGuard
from cad_harness.domain.errors import InvalidFeatureParametersError
from cad_harness.domain.models.base import ContractModel
from cad_harness.domain.models.drawing_spec import MissingInput
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.validation import ValidationReport, ValidationStage


class RemediationFindingInput(ContractModel):
    """One exact persisted audit finding selected by an engineer."""

    rule_id: str = Field(min_length=1, max_length=128)
    entity_ref: str = Field(min_length=1, max_length=512)


class RemediationSelectionInput(ContractModel):
    """Untrusted MCP selection; geometry and operation plans are intentionally absent."""

    audit_id: str = Field(min_length=1, max_length=64)
    selected_findings: tuple[RemediationFindingInput, ...] = Field(min_length=1, max_length=500)
    technical_inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


def _parse_remediation(payload: dict[str, Any]) -> RemediationSelectionInput:
    try:
        return RemediationSelectionInput.model_validate(payload)
    except ValidationError as exc:
        raise InvalidFeatureParametersError(
            "The remediation selection does not match the public contract",
            required_action=(
                "Supply an audit_id, one or more exact rule_id/entity_ref findings, "
                "and only documented technical_inputs"
            ),
            details={"invalid_fields": [".".join(map(str, item["loc"])) for item in exc.errors()]},
        ) from exc


def _spec_response(result: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Translate the service result into the envelope, including needs_input."""

    if result.get("status") == "needs_input":
        missing = tuple(MissingInput.model_validate(m) for m in result["missing_inputs"])

        return ToolResponse.needs_input(missing, job_id=job_id).model_dump(
            mode="json", exclude_none=True
        )

    payload = {k: v for k, v in result.items() if k not in {"status", "job_id"}}

    warnings: tuple[str, ...] = ()

    if payload.get("assumptions"):
        warnings = (
            "The spec contains assumptions that require engineer confirmation before commit.",
        )

    return ok(payload, job_id=job_id, warnings=warnings)


def _submit_change(
    context: ServerContext,
    *,
    job_id: str,
    spec: dict[str, Any] | None,
    remediation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply the exact public submission rules shared by submit and prepare."""

    current_job = context.service.store.get_job(job_id)
    if current_job is not None and (
        current_job.state is JobState.APPROVED or current_job.approval_id is not None
    ):
        raise InvalidFeatureParametersError(
            "An approved job is immutable through MCP change tools",
            required_action=(
                "Create a new job pinned to the current drawing revision, then prepare the "
                "revised change for a new engineer approval"
            ),
            details={"job_id": job_id, "state": current_job.state.value},
        )

    if spec is None and remediation is None:
        missing = (
            MissingInput(
                path="spec_or_remediation",
                reason="Supply exactly one revised spec or remediation selection",
            ),
        )
        return ToolResponse.needs_input(missing, job_id=job_id).model_dump(
            mode="json", exclude_none=True
        )
    if spec is not None and remediation is not None:
        raise InvalidFeatureParametersError(
            "cad_change_submit accepts exactly one change representation",
            required_action="Supply either spec or remediation, never both",
            details={"mutually_exclusive": ["spec", "remediation"]},
        )

    if remediation is not None:
        selection = _parse_remediation(remediation)
        selected = tuple((item.rule_id, item.entity_ref) for item in selection.selected_findings)
        result = context.service.submit_remediation_selection(
            job_id,
            audit_id=selection.audit_id,
            selected_findings=selected,
            technical_inputs=selection.technical_inputs,
        )
    else:
        assert spec is not None
        result = context.service.submit_spec(job_id, spec)

    return _spec_response(result, job_id)


def _validation_payload(report: ValidationReport) -> dict[str, Any]:
    return {
        "stage": report.stage.value,
        "plan_hash": report.plan_hash,
        "blocking_count": report.blocking_count,
        "error_count": report.error_count,
        "warning_count": report.warning_count,
        "commit_allowed": report.gate_allows_commit(),
        "findings": [
            finding.model_dump(mode="json", exclude_none=True) for finding in report.findings
        ],
    }


def _prepare_change(
    context: ServerContext,
    *,
    job_id: str,
    spec: dict[str, Any] | None,
    remediation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Submit, preview and validate one job without exposing a live-DWG write."""

    submission = _submit_change(
        context,
        job_id=job_id,
        spec=spec,
        remediation=remediation,
    )
    if submission.get("status") != ToolStatus.OK.value:
        return submission

    preview = context.service.preview(job_id)
    report = context.service.validate(job_id, ValidationStage.PRE_COMMIT)
    semantic_diff = context.service.get_diff(job_id)
    job = context.service.store.get_job(job_id)
    if job is None:  # The submission succeeded, so this is a persistence invariant failure.
        raise RuntimeError("Prepared job disappeared from the job store")

    submission_data = submission.get("data")
    if not isinstance(submission_data, dict):
        raise RuntimeError("Prepared submission returned an invalid envelope")
    return ok(
        {
            **submission_data,
            "expected_revision": job.expected_revision,
            "preview": preview,
            "validation": _validation_payload(report),
            "semantic_diff": semantic_diff,
            "approval_required": context.settings.security.require_commit_approval,
        },
        job_id=job_id,
        warnings=tuple(str(item) for item in submission.get("warnings", ())),
    )


def register(mcp: FastMCP, context: ServerContext, guard: ToolPermissionGuard) -> None:

    @guard.tool(mcp)
    def cad_job_create(
        request_context: McpContext[Any, Any, Any], document_id: str | None = None
    ) -> dict[str, Any]:
        """Create a change job and pin the document revision it is planned against.


        Every later step in the workflow takes the returned ``job_id``.
        """

        try:
            job = context.service.create_job(document_id)

            return ok(
                {
                    "job_id": job.job_id,
                    "document_id": job.document_id,
                    "expected_revision": job.expected_revision,
                    "state": job.state.value,
                },
                job_id=job.job_id,
            )

        except Exception as exc:
            return failure(exc)

    @guard.tool(mcp)
    def cad_spec_submit(
        request_context: McpContext[Any, Any, Any], job_id: str, spec: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate and compile a DrawingSpec into a deterministic operation plan.


        On success you get a ``plan_hash``; that hash is what preview, approval and

        commit all key off.


        If required engineering inputs are missing, the response is ``needs_input``

        with a field path for each one. Supply them from the user rather than choosing

        values yourself: sizes, datums, hole counts, diameters, PCDs and tolerance

        classes must never be guessed.
        """

        try:
            return _submit_change(
                context,
                job_id=job_id,
                spec=spec,
                remediation=None,
            )

        except Exception as exc:
            return failure(exc, job_id=job_id)

    @guard.tool(mcp)
    def cad_change_submit(
        request_context: McpContext[Any, Any, Any],
        job_id: str,
        spec: dict[str, Any] | None = None,
        remediation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit one revised spec or one selected-finding remediation request.


        A revised spec creates a new version and recompiles the plan. A remediation

        request names a persisted ``audit_id`` and exact ``rule_id``/``entity_ref``

        findings; the server freshly reads the drawing and compiles the operation plan.

        Caller-supplied models, plans and coordinates are never accepted. An approved

        job is immutable through MCP; create a new job for a revised change.
        """

        try:
            return _submit_change(
                context,
                job_id=job_id,
                spec=spec,
                remediation=remediation,
            )

        except Exception as exc:
            return failure(exc, job_id=job_id)

    @guard.tool(mcp)
    def cad_change_prepare(
        request_context: McpContext[Any, Any, Any],
        job_id: str,
        spec: dict[str, Any] | None = None,
        remediation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit and fully prepare one deterministic change without writing the DWG.


        Supply exactly one typed DrawingSpec or selected-finding remediation. The tool

        stops immediately when required engineering inputs are missing; otherwise it

        renders the preview, runs PRE_COMMIT validation and returns the semantic diff.

        Operation plans and scripts are never accepted. Engineer Desktop approval and

        a separate ``cad_commit`` call remain mandatory before the live DWG can change.
        """

        try:
            return _prepare_change(
                context,
                job_id=job_id,
                spec=spec,
                remediation=remediation,
            )

        except Exception as exc:
            return failure(exc, job_id=job_id)
