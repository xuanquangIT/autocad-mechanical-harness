"""Security controls: approval tokens, path allowlists, redaction."""

from cad_harness.security.approval import (
    issue_approval,
    make_approval_token,
    verify_approval_token,
)
from cad_harness.security.client_profiles import (
    APPROVAL_REQUIRED_TOOLS,
    PLANNING_TOOLS,
    READ_ONLY_TOOLS,
    TOOL_NAMES,
    ClientPermissionProfile,
    resolve_profile,
)
from cad_harness.security.paths import ensure_path_allowed, is_path_allowed
from cad_harness.security.redaction import redact_path, redact_payload
from cad_harness.security.rollback_approval import (
    issue_rollback_approval,
    make_rollback_approval_token,
    verify_rollback_approval_token,
)

__all__ = [
    "APPROVAL_REQUIRED_TOOLS",
    "PLANNING_TOOLS",
    "READ_ONLY_TOOLS",
    "TOOL_NAMES",
    "ClientPermissionProfile",
    "ensure_path_allowed",
    "is_path_allowed",
    "issue_approval",
    "issue_rollback_approval",
    "make_approval_token",
    "make_rollback_approval_token",
    "redact_path",
    "redact_payload",
    "resolve_profile",
    "verify_approval_token",
    "verify_rollback_approval_token",
]
