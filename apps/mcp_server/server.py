"""MCP server construction.

STDIO is the compatibility baseline so the same server works with Codex, Claude Code,
Kiro and Zed. On STDIO, stdout is the protocol channel; logging goes to stderr.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent
from pydantic import ValidationError

from apps.mcp_server.context import ServerContext, build_context, failure
from apps.mcp_server.tools import register_all
from cad_harness import __version__
from cad_harness.domain.models.drawing_spec import MissingInput
from cad_harness.domain.models.envelope import ToolResponse
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id

INSTRUCTIONS = """\
AutoCAD Mechanical Harness. Turns engineering requirements into verifiable 2D
mechanical drawings.

You describe intent; this server computes geometry. Do not compute coordinates,
intersections, pattern positions or tolerances yourself.

Workflow:
  0. cad_image_inspect/trace     - optional local calibrated image intake
     cad_image_draft             - optional signed, review-only draft DrawingSpec
  1. cad_status                  - check the adapter and which features exist
  2. cad_document_inspect        - read the document and pin its revision
  3. cad_job_create              - open a change job
  4. cad_spec_submit             - submit a DrawingSpec; get a plan_hash
  5. cad_preview                 - render artifacts and a semantic diff
  6. cad_validate                - run rules; read the findings
  7. (engineer approves in their own UI, not through you)
  8. cad_commit                  - commit with plan_hash, revision and approval token

Rules:
  - If cad_spec_submit returns needs_input, ask the user for those exact fields. Never
    substitute a plausible number for a missing size, datum, hole count, diameter, PCD
    or tolerance class.
  - Never claim a drawing meets a company standard when cad_status reports the profile
    is not company approved.
  - Report validation findings to the user; do not work around them.
  - If a commit outcome is unknown, stop and report it. Do not retry.
"""


class EnvelopeFastMCP(FastMCP):
    """Keep argument-validation and dispatch failures inside the tool envelope."""

    @staticmethod
    def _finalize_envelope(result: Any, request_id: str) -> Any:
        """Ensure normal and rejected results carry one transport correlation id."""
        if isinstance(result, dict):
            finalized = dict(result)
            finalized.setdefault("request_id", request_id)
            return finalized
        if not (isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict)):
            return result
        content, structured = result
        finalized = dict(structured)
        finalized.setdefault("request_id", request_id)
        if isinstance(content, list):
            rendered = json.dumps(finalized, ensure_ascii=False, indent=2)
            content = [
                block.model_copy(update={"text": rendered})
                if isinstance(block, TextContent)
                else block
                for block in content
            ]
        return content, finalized

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        request_id = arguments.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            request_id = new_id(IdPrefix.REQUEST)
        job_id = arguments.get("job_id")
        if not isinstance(job_id, str):
            job_id = None
        try:
            result = await super().call_tool(name, arguments)
            return self._finalize_envelope(result, request_id)
        except ToolError as exc:
            validation_error = exc.__cause__
            if isinstance(validation_error, ValidationError):
                missing = tuple(
                    MissingInput(
                        path=".".join(str(part) for part in item["loc"]),
                        reason="Required tool input is missing or invalid",
                        accepted_formats=(str(item["type"]),),
                    )
                    for item in validation_error.errors(include_url=False)
                )
                return ToolResponse.needs_input(
                    missing,
                    job_id=job_id,
                    request_id=request_id,
                ).model_dump(mode="json", exclude_none=True)
            return failure(
                RuntimeError("MCP tool dispatch failed"),
                job_id=job_id,
                request_id=request_id,
            )


def create_server(config_path: Path | None = None) -> tuple[FastMCP, ServerContext]:
    """Build the server and its context."""
    context = build_context(config_path)
    # FastMCP reports the SDK version in serverInfo and takes no override, so the
    # harness version is surfaced through cad_status instead.
    mcp = EnvelopeFastMCP(
        name=f"{context.settings.mcp.server_name} {__version__}",
        instructions=INSTRUCTIONS,
    )
    register_all(mcp, context)
    return mcp, context


def run_stdio(config_path: Path | None = None) -> None:
    mcp, _ = create_server(config_path)
    mcp.run(transport="stdio")
