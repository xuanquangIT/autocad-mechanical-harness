"""Fail-closed MCP client permission profiles."""

from __future__ import annotations

from typing import Literal

from cad_harness.config import Settings
from cad_harness.domain.models.base import ContractModel

PermissionMode = Literal["read_only", "approval_required", "full"]

TOOL_NAMES: tuple[str, ...] = (
    "cad_status",
    "cad_document_inspect",
    "cad_selection_inspect",
    "cad_feature_catalog_search",
    "cad_job_create",
    "cad_spec_submit",
    "cad_change_submit",
    "cad_preview",
    "cad_validate",
    "cad_diff_get",
    "cad_commit",
    "cad_rollback",
    "cad_export",
    "cad_drawing_read",
    "cad_feature_recognize",
    "cad_takeoff",
    "cad_takeoff_export",
    "cad_audit",
    "cad_measure",
    "cad_image_inspect",
    "cad_image_trace",
    "cad_image_draft",
)

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "cad_status",
        "cad_document_inspect",
        "cad_selection_inspect",
        "cad_feature_catalog_search",
        "cad_validate",
        "cad_diff_get",
        "cad_drawing_read",
        "cad_feature_recognize",
        "cad_takeoff",
        "cad_audit",
        "cad_measure",
        "cad_image_inspect",
        "cad_image_trace",
        "cad_image_draft",
    }
)

APPROVAL_REQUIRED_TOOLS: frozenset[str] = frozenset(
    {
        "cad_job_create",
        "cad_spec_submit",
        "cad_change_submit",
        "cad_preview",
        "cad_commit",
        "cad_rollback",
        "cad_export",
        "cad_takeoff_export",
    }
)

# Filesystem export and internal workflow state may also be approval-gated, but only
# these two public tools can change live DWG content.
DWG_MUTATING_TOOLS: frozenset[str] = frozenset({"cad_commit", "cad_rollback"})


class ClientPermissionProfile(ContractModel):
    """Resolved permissions used for one MCP tool call."""

    client_id: str
    mode: PermissionMode
    allowed_tools: frozenset[str]


def _tools_for_mode(mode: PermissionMode) -> frozenset[str]:
    if mode == "read_only":
        return READ_ONLY_TOOLS
    # Approval remains mandatory in the application service. A profile only decides
    # whether the client can reach that approval-gated operation at all.
    return frozenset(TOOL_NAMES)


def resolve_profile(client_id: str | None, settings: Settings) -> ClientPermissionProfile:
    """Resolve a configured client; anonymous and undeclared clients are read-only."""
    resolved_client_id = client_id or "anonymous"
    # STDIO clients do not always transmit an identity. They remain read-only unless
    # the deployment explicitly configures the literal ``anonymous`` profile.
    configured = settings.mcp.client_profiles.clients.get(resolved_client_id)
    if configured is None:
        return ClientPermissionProfile(
            client_id=resolved_client_id,
            mode="read_only",
            allowed_tools=READ_ONLY_TOOLS,
        )

    implied = _tools_for_mode(configured.mode)
    allowed = implied
    if configured.mode != "read_only" and configured.allowed_tools:
        allowed = implied.intersection(configured.allowed_tools)
    return ClientPermissionProfile(
        client_id=resolved_client_id,
        mode=configured.mode,
        allowed_tools=allowed,
    )


__all__ = [
    "APPROVAL_REQUIRED_TOOLS",
    "DWG_MUTATING_TOOLS",
    "READ_ONLY_TOOLS",
    "TOOL_NAMES",
    "ClientPermissionProfile",
    "resolve_profile",
]
