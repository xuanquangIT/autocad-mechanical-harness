"""MCP tool surface. Guards the contract every AI client discovers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from apps.mcp_server.server import create_server
from apps.mcp_server.tools import APPROVAL_REQUIRED_TOOLS, READ_ONLY_TOOLS, TOOL_NAMES

#: Primitive drawing tools must never appear: exposing them would let a model assemble
#: geometry itself, which is the failure mode this architecture exists to prevent.
FORBIDDEN_TOOL_NAMES = frozenset(
    {
        "draw_line",
        "draw_circle",
        "draw_arc",
        "draw_polyline",
        "draw_rectangle",
        "draw_text",
        "draw_hatch",
        "add_dimension",
        "trim",
        "offset",
        "process_command",
    }
)


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CAD_HARNESS_ADAPTER", "fake")
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "test-secret")
    monkeypatch.setenv("CAD_HARNESS_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("CAD_HARNESS_LOG_LEVEL", "ERROR")
    mcp, context = create_server(tmp_path / "missing-config.yaml")
    return mcp, context


def _tools(mcp):
    return asyncio.run(mcp.list_tools())


class TestToolSurface:
    def test_exactly_thirteen_tools(self, server) -> None:
        mcp, _ = server
        assert len(_tools(mcp)) == 13

    def test_tool_names_match_the_contract(self, server) -> None:
        mcp, _ = server
        assert sorted(tool.name for tool in _tools(mcp)) == sorted(TOOL_NAMES)

    def test_no_primitive_drawing_tools_are_exposed(self, server) -> None:
        mcp, _ = server
        assert not {tool.name for tool in _tools(mcp)} & FORBIDDEN_TOOL_NAMES

    def test_every_tool_has_a_description(self, server) -> None:
        """AI clients rely on descriptions to choose correctly."""
        mcp, _ = server
        for tool in _tools(mcp):
            assert tool.description and len(tool.description) > 40

    def test_every_tool_declares_an_input_schema(self, server) -> None:
        mcp, _ = server
        for tool in _tools(mcp):
            assert tool.inputSchema["type"] == "object"

    def test_commit_requires_all_four_bindings(self, server) -> None:
        mcp, _ = server
        commit = next(tool for tool in _tools(mcp) if tool.name == "cad_commit")
        required = set(commit.inputSchema.get("required", []))
        assert required == {
            "job_id",
            "idempotency_key",
            "expected_revision",
            "plan_hash",
            "approval_token",
        }

    def test_read_and_write_tools_are_disjoint(self) -> None:
        assert not READ_ONLY_TOOLS & APPROVAL_REQUIRED_TOOLS

    def test_approval_covers_every_destructive_tool(self) -> None:
        assert {"cad_commit", "cad_rollback", "cad_export"} == APPROVAL_REQUIRED_TOOLS

    def test_instructions_forbid_inventing_values(self, server) -> None:
        from apps.mcp_server.server import INSTRUCTIONS

        assert "substitute a plausible number" in INSTRUCTIONS
        assert "needs_input" in INSTRUCTIONS
        assert "Do not compute coordinates" in INSTRUCTIONS


class TestToolInvocation:
    def test_status_returns_the_envelope(self, server) -> None:
        mcp, _ = server
        result = asyncio.run(mcp.call_tool("cad_status", {}))
        payload = result[1] if isinstance(result, tuple) else result
        assert payload["status"] == "ok"
        assert payload["data"]["adapter"]["adapter_type"] == "fake"

    def test_unknown_job_returns_a_rejected_envelope_not_a_crash(self, server) -> None:
        mcp, _ = server
        result = asyncio.run(mcp.call_tool("cad_preview", {"job_id": "job_missing"}))
        payload = result[1] if isinstance(result, tuple) else result
        assert payload["status"] == "rejected"
        assert payload["error"]["code"] == "DOCUMENT_NOT_FOUND"

    def test_feature_catalog_lists_only_implemented_features(self, server) -> None:
        mcp, _ = server
        result = asyncio.run(mcp.call_tool("cad_feature_catalog_search", {"query": ""}))
        payload = result[1] if isinstance(result, tuple) else result
        types = {entry["type"] for entry in payload["data"]["features"]}
        assert types == {"rectangular_plate", "rectangular_hole_pattern", "bolt_circle_pattern"}
