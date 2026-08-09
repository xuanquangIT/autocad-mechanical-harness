"""MCP tool surface. Guards the contract every AI client discovers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from apps.mcp_server.server import create_server
from apps.mcp_server.tools import (
    APPROVAL_REQUIRED_TOOLS,
    DWG_MUTATING_TOOLS,
    READ_ONLY_TOOLS,
    TOOL_NAMES,
)

from cad_harness.domain.models.envelope import ToolResponse

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
    monkeypatch.setenv("CAD_HARNESS_SQLITE_PATH", str(tmp_path / "harness.db"))
    monkeypatch.setenv("CAD_HARNESS_LOG_LEVEL", "ERROR")
    mcp, context = create_server(tmp_path / "missing-config.yaml")
    return mcp, context


def _tools(mcp):
    return asyncio.run(mcp.list_tools())


class TestToolSurface:
    def test_exactly_twenty_two_tools(self, server) -> None:
        mcp, _ = server
        assert len(_tools(mcp)) == 22

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

    def test_every_tool_declares_an_output_schema(self, server) -> None:
        mcp, _ = server
        for tool in _tools(mcp):
            assert tool.outputSchema is not None
            assert tool.outputSchema["type"] == "object"

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

    def test_permission_sets_are_an_exact_partition(self) -> None:
        assert not READ_ONLY_TOOLS & APPROVAL_REQUIRED_TOOLS
        assert frozenset(TOOL_NAMES) == READ_ONLY_TOOLS | APPROVAL_REQUIRED_TOOLS

    def test_exactly_two_tools_can_mutate_dwg(self) -> None:
        assert {"cad_commit", "cad_rollback"} == DWG_MUTATING_TOOLS
        assert DWG_MUTATING_TOOLS <= APPROVAL_REQUIRED_TOOLS

    def test_new_tools_match_the_public_contract(self) -> None:
        assert {
            "cad_drawing_read",
            "cad_feature_recognize",
            "cad_takeoff",
            "cad_takeoff_export",
            "cad_audit",
            "cad_measure",
            "cad_image_inspect",
            "cad_image_trace",
            "cad_image_draft",
        } <= set(TOOL_NAMES)

    def test_new_tool_output_schemas_name_their_domain_payload(self, server) -> None:
        mcp, _ = server
        expected_payloads = {
            "cad_drawing_read": {"DrawingModel", "DrawingSummary"},
            "cad_feature_recognize": {"RecognitionReport"},
            "cad_takeoff": {"TakeoffReport"},
            "cad_takeoff_export": {"TakeoffExportData"},
            "cad_audit": {"DrawingAuditEvidence"},
            "cad_measure": {"MeasurementResult"},
            "cad_image_inspect": {"RasterTraceReport"},
            "cad_image_trace": {"RasterTraceReport"},
            "cad_image_draft": {"DrawingSpec"},
        }
        by_name = {tool.name: tool for tool in _tools(mcp)}
        for tool_name, expected in expected_payloads.items():
            data_schema = by_name[tool_name].outputSchema["properties"]["data"]
            references = {
                item["$ref"].rsplit("/", 1)[-1] for item in data_schema["anyOf"] if "$ref" in item
            }
            assert expected == references

    def test_instructions_forbid_inventing_values(self, server) -> None:
        from apps.mcp_server.server import INSTRUCTIONS

        assert "substitute a plausible number" in INSTRUCTIONS
        assert "needs_input" in INSTRUCTIONS
        assert "Do not compute coordinates" in INSTRUCTIONS


class TestToolInvocation:
    def test_missing_inputs_return_envelope_instead_of_raising(self, server) -> None:
        mcp, _ = server
        result = asyncio.run(mcp.call_tool("cad_measure", {}))
        payload = result[1] if isinstance(result, tuple) else result
        response = ToolResponse.model_validate(payload)
        assert response.status == "needs_input"
        assert response.request_id is not None
        assert {item.path for item in response.missing_inputs} == {"model", "request"}

    def test_missing_explicit_job_id_returns_standard_envelope(self, server) -> None:
        mcp, _ = server
        result = asyncio.run(mcp.call_tool("cad_validate", {}))
        payload = result[1] if isinstance(result, tuple) else result
        response = ToolResponse.model_validate(payload)
        assert response.status == "needs_input"
        assert response.error is not None
        assert response.error.code == "MISSING_REQUIRED_INPUTS"
        assert {item.path for item in response.missing_inputs} == {"job_id"}

    def test_tool_body_exception_is_redacted_into_failed_envelope(
        self, server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp, context = server

        def explode():
            raise RuntimeError("secret C:/Customers/Acme/project.dwg")

        monkeypatch.setattr(context.service, "status", explode)
        result = asyncio.run(mcp.call_tool("cad_status", {}))
        payload = result[1] if isinstance(result, tuple) else result
        response = ToolResponse.model_validate(payload)
        assert response.status == "failed"
        assert response.error is not None
        assert response.error.code == "INTERNAL_ERROR"
        assert "Customers" not in str(payload)

    def test_status_returns_the_envelope(self, server) -> None:
        mcp, _ = server
        result = asyncio.run(mcp.call_tool("cad_status", {}))
        payload = result[1] if isinstance(result, tuple) else result
        assert payload["status"] == "ok"
        assert payload["data"]["adapter"]["adapter_type"] == "fake"

    def test_anonymous_non_read_tool_is_rejected_before_lookup(self, server) -> None:
        mcp, _ = server
        result = asyncio.run(mcp.call_tool("cad_preview", {"job_id": "job_missing"}))
        payload = result[1] if isinstance(result, tuple) else result
        assert payload["status"] == "rejected"
        assert payload["error"]["code"] == "TOOL_NOT_ALLOWED"
        assert payload["error"]["details"]["requested_tool"] == "cad_preview"

    def test_feature_catalog_lists_only_implemented_features(self, server) -> None:
        mcp, _ = server
        result = asyncio.run(mcp.call_tool("cad_feature_catalog_search", {"query": ""}))
        payload = result[1] if isinstance(result, tuple) else result
        types = {entry["type"] for entry in payload["data"]["features"]}
        assert types == {
            "rectangular_plate",
            "rectangular_hole_pattern",
            "bolt_circle_pattern",
            "flange",
            "slot",
            "l_bracket",
            "corner_notch",
            "edge_cutout",
            "keyway",
            "linear_hole_pattern",
        }
