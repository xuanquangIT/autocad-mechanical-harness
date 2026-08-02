"""MCP server construction.

STDIO is the compatibility baseline so the same server works with Codex, Claude Code,
Kiro and Zed. On STDIO, stdout is the protocol channel; logging goes to stderr.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from apps.mcp_server.context import ServerContext, build_context
from apps.mcp_server.tools import register_all
from cad_harness import __version__

INSTRUCTIONS = """\
AutoCAD Mechanical Harness. Turns engineering requirements into verifiable 2D
mechanical drawings.

You describe intent; this server computes geometry. Do not compute coordinates,
intersections, pattern positions or tolerances yourself.

Workflow:
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


def create_server(config_path: Path | None = None) -> tuple[FastMCP, ServerContext]:
    """Build the server and its context."""
    context = build_context(config_path)
    # FastMCP reports the SDK version in serverInfo and takes no override, so the
    # harness version is surfaced through cad_status instead.
    mcp = FastMCP(
        name=f"{context.settings.mcp.server_name} {__version__}",
        instructions=INSTRUCTIONS,
    )
    register_all(mcp, context)
    return mcp, context


def run_stdio(config_path: Path | None = None) -> None:
    mcp, _ = create_server(config_path)
    mcp.run(transport="stdio")
