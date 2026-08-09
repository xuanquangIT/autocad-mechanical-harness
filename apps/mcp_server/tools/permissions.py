"""Registration-time guard for every MCP tool."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast

from mcp.server.fastmcp import Context, FastMCP

from apps.mcp_server.context import ServerContext
from cad_harness.domain.errors import ToolNotAllowedError
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.observability.audit import AuditEventType
from cad_harness.observability.logging import get_logger
from cad_harness.security.client_profiles import ClientPermissionProfile, resolve_profile

P = ParamSpec("P")
R = TypeVar("R")
ToolBody = Callable[[], R]


class ToolPermissionGuard:
    """Resolve a profile and reject disallowed calls before their body executes."""

    def __init__(self, context: ServerContext) -> None:
        self._context = context

    def invoke(
        self,
        tool_name: str,
        profile: ClientPermissionProfile,
        body: ToolBody[R],
        *,
        request_id: str | None = None,
    ) -> R | dict[str, Any]:
        """Run ``body`` exactly when ``tool_name`` belongs to the profile."""
        if tool_name in profile.allowed_tools:
            return body()

        allowed_tools = sorted(profile.allowed_tools)
        error = ToolNotAllowedError(
            f"Tool '{tool_name}' is not allowed for client '{profile.client_id}'",
            details={
                "requested_tool": tool_name,
                "allowed_tools": allowed_tools,
                "profile_mode": profile.mode,
            },
        )
        audit_event_id = self._context.service.audit.append(
            event_type=AuditEventType.TOOL_CALL_REJECTED.value,
            job_id=None,
            actor_type="ai_client",
            actor_id=profile.client_id,
            payload={
                "tool": tool_name,
                "client": profile.client_id,
                "profile": profile.mode,
            },
        )
        get_logger(__name__).warning(
            "tool_call_rejected",
            tool=tool_name,
            client=profile.client_id,
            profile=profile.mode,
            error_code=error.code.value,
            outcome=ToolStatus.REJECTED.value,
        )
        response = ToolResponse.from_error(
            error,
            status=ToolStatus.REJECTED,
            request_id=request_id or new_id(IdPrefix.REQUEST),
        ).model_copy(update={"audit_event_id": audit_event_id})
        return response.model_dump(mode="json", exclude_none=True)

    def tool(self, mcp: FastMCP) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """Register a function only after wrapping it with this guard."""

        def register(fn: Callable[P, R]) -> Callable[P, R]:
            tool_name = fn.__name__

            @wraps(fn)
            def guarded_call(*args: P.args, **kwargs: P.kwargs) -> R | dict[str, Any]:
                request_context = next(
                    (value for value in (*args, *kwargs.values()) if isinstance(value, Context)),
                    None,
                )
                if request_context is None:
                    client_id = None
                else:
                    try:
                        client_id = request_context.client_id
                    except ValueError:
                        # FastMCP's direct-call test helper supplies a detached Context.
                        # Treat it exactly like an anonymous transport request.
                        client_id = None
                profile = resolve_profile(client_id, self._context.settings)
                supplied_request_id = kwargs.get("request_id")
                request_id = supplied_request_id if isinstance(supplied_request_id, str) else None
                return self.invoke(
                    tool_name,
                    profile,
                    lambda: fn(*args, **kwargs),
                    request_id=request_id,
                )

            return cast(Callable[P, R], mcp.tool()(guarded_call))

        return register


def guarded[R](
    tool_name: str,
    context: ServerContext,
    fn: ToolBody[R],
    *,
    client_id: str | None = None,
) -> R | dict[str, Any]:
    """Direct guard entry point for non-FastMCP callers and focused tests."""
    profile = resolve_profile(client_id, context.settings)
    return ToolPermissionGuard(context).invoke(tool_name, profile, fn)


__all__ = ["ToolPermissionGuard", "guarded"]
