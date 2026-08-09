"""Property 7: client tool allowlist enforcement."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from apps.mcp_server.context import ServerContext
from apps.mcp_server.tools.permissions import ToolPermissionGuard
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.config import Settings
from cad_harness.observability.audit import AuditEventType, InMemoryAuditSink
from cad_harness.security.client_profiles import TOOL_NAMES, ClientPermissionProfile

TOOL_NAMES_WITH_UNKNOWN = (*TOOL_NAMES, "cad_future_tool")


# Feature: cad-ai-production-roadmap, Property 7: Tool allowlist cho phép đúng khi và chỉ khi tool nằm trong profile
@given(
    allowed=st.frozensets(st.sampled_from(TOOL_NAMES_WITH_UNKNOWN)),
    requested=st.sampled_from(TOOL_NAMES_WITH_UNKNOWN),
)
@settings(max_examples=100, deadline=None)
def test_tool_runs_if_and_only_if_it_is_in_the_profile(
    allowed: frozenset[str], requested: str
) -> None:
    """**Validates: Requirements 3.2, 3.3**"""
    audit = InMemoryAuditSink()
    context = cast(
        ServerContext,
        SimpleNamespace(settings=Settings(), service=SimpleNamespace(audit=audit)),
    )
    profile = ClientPermissionProfile(
        client_id="property-client", mode="full", allowed_tools=allowed
    )
    calls: list[str] = []

    result = ToolPermissionGuard(context).invoke(
        requested, profile, lambda: calls.append(requested) or {"executed": True}
    )

    if requested in allowed:
        assert result == {"executed": True}
        assert calls == [requested]
        assert audit.events == []
    else:
        assert calls == []
        assert result["status"] == "rejected"
        assert result["error"]["code"] == "TOOL_NOT_ALLOWED"
        assert result["error"]["details"]["allowed_tools"] == sorted(allowed)
        assert audit.events[-1].event_type == AuditEventType.TOOL_CALL_REJECTED.value
