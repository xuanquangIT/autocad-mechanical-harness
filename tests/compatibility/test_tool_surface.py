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

    def test_change_submit_exposes_spec_and_remediation_variants(self, server) -> None:
        mcp, _ = server
        change = next(tool for tool in _tools(mcp) if tool.name == "cad_change_submit")
        properties = change.inputSchema["properties"]
        assert {"job_id", "spec", "remediation"} <= set(properties)
        assert change.inputSchema["required"] == ["job_id"]

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
    @staticmethod
    def _approval_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[object, object]:
        monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "test-secret")
        config = tmp_path / "approval-server.yaml"
        config.write_text(
            "\n".join(
                [
                    "adapter:",
                    "  type: fake",
                    "mcp:",
                    "  client_profiles:",
                    "    clients:",
                    "      anonymous:",
                    "        mode: approval_required",
                    "storage:",
                    f"  sqlite_path: '{(tmp_path / 'approval.db').as_posix()}'",
                    f"  preview_directory: '{(tmp_path / 'previews').as_posix()}'",
                    f"  checkpoint_directory: '{(tmp_path / 'checkpoints').as_posix()}'",
                    f"  export_directory: '{(tmp_path / 'exports').as_posix()}'",
                    "observability:",
                    "  log_level: ERROR",
                ]
            ),
            encoding="utf-8",
        )
        return create_server(config)

    def test_change_submit_routes_only_a_finding_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp, context = self._approval_server(tmp_path, monkeypatch)
        captured: dict[str, object] = {}

        def submit(job_id, audit_id, selected_findings, technical_inputs):
            captured.update(
                job_id=job_id,
                audit_id=audit_id,
                selected_findings=selected_findings,
                technical_inputs=technical_inputs,
            )
            return {
                "status": "ok",
                "job_id": job_id,
                "plan_hash": "sha256:trusted",
                "operation_count": 1,
                "selected_finding_count": 1,
            }

        monkeypatch.setattr(context.service, "submit_remediation_selection", submit)
        result = asyncio.run(
            mcp.call_tool(
                "cad_change_submit",
                {
                    "job_id": "job_remediation",
                    "remediation": {
                        "audit_id": "audit_current",
                        "selected_findings": [
                            {"rule_id": "DUPLICATE_ENTITY", "entity_ref": "acad:handle:2A"}
                        ],
                    },
                },
            )
        )
        payload = result[1] if isinstance(result, tuple) else result
        assert payload["status"] == "ok"
        assert captured == {
            "job_id": "job_remediation",
            "audit_id": "audit_current",
            "selected_findings": (("DUPLICATE_ENTITY", "acad:handle:2A"),),
            "technical_inputs": {},
        }

    def test_change_submit_rejects_both_or_untrusted_plan_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp, _ = self._approval_server(tmp_path, monkeypatch)
        both = asyncio.run(
            mcp.call_tool(
                "cad_change_submit",
                {"job_id": "job_both", "spec": {}, "remediation": {}},
            )
        )
        both_payload = both[1] if isinstance(both, tuple) else both
        assert both_payload["status"] == "rejected"
        assert both_payload["error"]["code"] == "INVALID_FEATURE_PARAMETERS"

        untrusted = asyncio.run(
            mcp.call_tool(
                "cad_change_submit",
                {
                    "job_id": "job_plan",
                    "remediation": {
                        "audit_id": "audit_current",
                        "selected_findings": [{"rule_id": "DUPLICATE_ENTITY", "entity_ref": "ref"}],
                        "plan": {"operations": []},
                    },
                },
            )
        )
        untrusted_payload = untrusted[1] if isinstance(untrusted, tuple) else untrusted
        assert untrusted_payload["status"] == "rejected"
        assert untrusted_payload["error"]["code"] == "INVALID_FEATURE_PARAMETERS"

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
