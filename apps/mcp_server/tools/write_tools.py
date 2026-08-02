"""Write tools. Each one modifies the drawing or the filesystem and needs approval."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from apps.mcp_server.context import ServerContext, failure, ok


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool()
    def cad_commit(
        job_id: str,
        idempotency_key: str,
        expected_revision: str,
        plan_hash: str,
        approval_token: str,
    ) -> dict[str, Any]:
        """Commit an approved plan to the drawing.

        Requires all four bindings, and all four are checked:

        * ``plan_hash`` must equal the plan the engineer approved
        * ``approval_token`` must be unexpired and cover this job, plan and revision
        * ``expected_revision`` must still match the document
        * ``idempotency_key`` must be a fresh, stable key you generated for this attempt

        Do not obtain the approval token yourself. It is issued to the engineer through
        the approval UI. If a commit fails with an unknown outcome, do not retry: report
        it and let the job be reconciled.
        """
        try:
            result = context.service.commit(
                job_id,
                idempotency_key=idempotency_key,
                expected_revision=expected_revision,
                plan_hash=plan_hash,
                approval_token=approval_token,
            )
            return ok(result.model_dump(mode="json", exclude_none=True), job_id=job_id)
        except Exception as exc:
            return failure(exc, job_id=job_id)

    @mcp.tool()
    def cad_rollback(job_id: str) -> dict[str, Any]:
        """Restore the document to the job's checkpoint or undo group.

        Destructive: it discards work done after the checkpoint. Confirm with the
        engineer before calling.
        """
        try:
            result = context.service.rollback(job_id)
            return ok(result.model_dump(mode="json", exclude_none=True), job_id=job_id)
        except Exception as exc:
            return failure(exc, job_id=job_id)

    @mcp.tool()
    def cad_export(
        document_id: str,
        target_path: str,
        export_format: str = "dxf",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Export the document to DWG, DXF or PDF.

        The target must be inside a configured allowlisted directory, and an existing
        file is never overwritten unless ``overwrite`` is explicitly true.
        """
        try:
            result = context.service.export(
                document_id, target_path, export_format, overwrite=overwrite
            )
            return ok(result.model_dump(mode="json", exclude_none=True))
        except Exception as exc:
            return failure(exc)
