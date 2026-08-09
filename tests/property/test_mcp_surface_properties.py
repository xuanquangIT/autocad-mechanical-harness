"""Properties 56-57 for the complete public MCP surface."""

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
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cad_harness.domain.models.envelope import ToolResponse


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CAD_HARNESS_ADAPTER", "fake")
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "test-secret")
    monkeypatch.setenv("CAD_HARNESS_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("CAD_HARNESS_SQLITE_PATH", str(tmp_path / "harness.db"))
    monkeypatch.setenv("CAD_HARNESS_LOG_LEVEL", "ERROR")
    return create_server(tmp_path / "missing-config.yaml")


# Feature: cad-ai-production-roadmap, Property 56: exact tool partition/no primitives
@given(tool_name=st.sampled_from(TOOL_NAMES))
def test_each_tool_belongs_to_exactly_one_permission_set(tool_name: str) -> None:
    """**Validates: Requirements 19.2, 19.3, 19.7**"""
    assert (tool_name in READ_ONLY_TOOLS) != (tool_name in APPROVAL_REQUIRED_TOOLS)
    lowered = tool_name.casefold()
    primitives = ("draw_line", "draw_arc", "draw_circle", "trim", "offset", "extend")
    assert not any(token in lowered for token in primitives)
    assert {"cad_commit", "cad_rollback"} == DWG_MUTATING_TOOLS


# Feature: cad-ai-production-roadmap, Property 57: schemas/envelopes/no leaked exceptions
@given(tool_name=st.sampled_from(TOOL_NAMES))
@settings(
    max_examples=len(TOOL_NAMES),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_missing_or_empty_input_never_escapes_as_an_exception(server, tool_name: str) -> None:
    """**Validates: Requirements 19.5, 19.6, 19.8**"""
    result = asyncio.run(server[0].call_tool(tool_name, {}))
    payload = result[1] if isinstance(result, tuple) else result
    response = ToolResponse.model_validate(payload)
    assert response.request_id is not None
