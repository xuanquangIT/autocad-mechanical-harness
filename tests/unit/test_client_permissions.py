"""Examples for fail-closed client tool permissions."""

from cad_harness.config import Settings
from cad_harness.security.client_profiles import (
    APPROVAL_REQUIRED_TOOLS,
    READ_ONLY_TOOLS,
    resolve_profile,
)


def test_read_only_and_approval_required_tools_are_disjoint() -> None:
    """**Validates: Requirements 3.6**"""
    assert READ_ONLY_TOOLS.isdisjoint(APPROVAL_REQUIRED_TOOLS)


def test_anonymous_client_resolves_to_read_only() -> None:
    """**Validates: Requirements 3.5**"""
    profile = resolve_profile(None, Settings())
    assert profile.mode == "read_only"
    assert profile.allowed_tools == READ_ONLY_TOOLS


def test_undeclared_named_client_resolves_to_read_only() -> None:
    profile = resolve_profile("not-in-config", Settings())
    assert profile.mode == "read_only"
    assert profile.allowed_tools == READ_ONLY_TOOLS


def test_identityless_stdio_client_can_be_explicitly_granted_preview_access() -> None:
    settings = Settings.model_validate(
        {
            "adapter": {"type": "fake"},
            "mcp": {"client_profiles": {"clients": {"anonymous": {"mode": "approval_required"}}}},
        }
    )
    profile = resolve_profile(None, settings)
    assert profile.mode == "approval_required"
    assert profile.allowed_tools == READ_ONLY_TOOLS | APPROVAL_REQUIRED_TOOLS


def test_declared_read_only_client_cannot_expand_its_allowlist() -> None:
    settings = Settings.model_validate(
        {
            "mcp": {
                "client_profiles": {
                    "clients": {"reader": {"mode": "read_only", "allowed_tools": ["cad_commit"]}}
                }
            }
        }
    )
    assert resolve_profile("reader", settings).allowed_tools == READ_ONLY_TOOLS
