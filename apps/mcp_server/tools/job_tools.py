"""Job and specification tools. These write to the internal database only."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from apps.mcp_server.context import ServerContext, failure, ok
from cad_harness.domain.models.drawing_spec import MissingInput
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus


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


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool()
    def cad_job_create(document_id: str | None = None) -> dict[str, Any]:
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

    @mcp.tool()
    def cad_spec_submit(job_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Validate and compile a DrawingSpec into a deterministic operation plan.

        On success you get a ``plan_hash``; that hash is what preview, approval and
        commit all key off.

        If required engineering inputs are missing, the response is ``needs_input``
        with a field path for each one. Supply them from the user rather than choosing
        values yourself: sizes, datums, hole counts, diameters, PCDs and tolerance
        classes must never be guessed.
        """
        try:
            return _spec_response(context.service.submit_spec(job_id, spec), job_id)
        except Exception as exc:
            return failure(exc, job_id=job_id)

    @mcp.tool()
    def cad_change_submit(job_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Submit a revised spec for an existing job.

        This creates a new spec version and recompiles the plan. Any prior approval is
        revoked, because it applied to a plan that no longer describes the change.
        """
        try:
            result = context.service.submit_spec(job_id, spec)
            response = _spec_response(result, job_id)
            if response.get("status") == ToolStatus.OK.value:
                response.setdefault("warnings", [])
                response["warnings"] = [
                    *response["warnings"],
                    "Previous approval was revoked. Re-preview and approve the new plan.",
                ]
            return response
        except Exception as exc:
            return failure(exc, job_id=job_id)
