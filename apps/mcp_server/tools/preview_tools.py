"""Preview, validation and diff tools. These never modify the live drawing."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context as McpContext
from mcp.server.fastmcp import FastMCP

from apps.mcp_server.context import ServerContext, failure, ok
from apps.mcp_server.tools.permissions import ToolPermissionGuard
from cad_harness.domain.models.validation import ValidationStage


def register(mcp: FastMCP, context: ServerContext, guard: ToolPermissionGuard) -> None:

    @guard.tool(mcp)
    def cad_preview(request_context: McpContext[Any, Any, Any], job_id: str) -> dict[str, Any]:
        """Render the compiled plan to temporary DXF/SVG files and build a semantic diff.


        The active drawing is untouched. Show the artifacts and the diff to the engineer;

        approval is given against this preview's ``plan_hash``.
        """

        try:
            return ok(context.service.preview(job_id), job_id=job_id)

        except Exception as exc:
            return failure(exc, job_id=job_id)

    @guard.tool(mcp)
    def cad_validate(
        request_context: McpContext[Any, Any, Any], job_id: str, stage: str = "pre_commit"
    ) -> dict[str, Any]:
        """Run validation for a stage and return findings with expected/actual/tolerance.


        Stages: ``plan``, ``preview_geometry``, ``company_standard``, ``pre_commit``.

        Blocking findings always stop a commit; errors stop it under the default policy.

        Report findings to the user rather than working around them.
        """

        try:
            try:
                parsed_stage = ValidationStage(stage)

            except ValueError:
                return ok(
                    {
                        "error": f"Unknown validation stage '{stage}'",
                        "valid_stages": [s.value for s in ValidationStage],
                    },
                    job_id=job_id,
                )

            report = context.service.validate(job_id, parsed_stage)

            return ok(
                {
                    "stage": report.stage.value,
                    "plan_hash": report.plan_hash,
                    "blocking_count": report.blocking_count,
                    "error_count": report.error_count,
                    "warning_count": report.warning_count,
                    "commit_allowed": (
                        parsed_stage is ValidationStage.PRE_COMMIT and report.gate_allows_commit()
                    ),
                    "findings": [
                        f.model_dump(mode="json", exclude_none=True) for f in report.findings
                    ],
                },
                job_id=job_id,
            )

        except Exception as exc:
            return failure(exc, job_id=job_id)

    @guard.tool(mcp)
    def cad_diff_get(request_context: McpContext[Any, Any, Any], job_id: str) -> dict[str, Any]:
        """Return the semantic diff for the job's current plan.


        Describes what will change in engineering terms: entities added, modified or

        deleted, with their layers and expected measurements.
        """

        try:
            return ok(context.service.get_diff(job_id), job_id=job_id)

        except Exception as exc:
            return failure(exc, job_id=job_id)
