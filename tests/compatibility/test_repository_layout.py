"""Release workflow structure must preserve the bounded preview evidence lane."""

from __future__ import annotations

from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def _windows_steps() -> list[dict[str, object]]:
    payload = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    return payload["jobs"]["windows-quality"]["steps"]


def test_ci_exercises_exact_bounded_mcp_integrations() -> None:
    commands = "\n".join(str(step.get("run", "")) for step in _windows_steps())

    assert "tests/integration/test_mcp_stdio_process_broker.py" in commands
    assert "tests/integration/test_reference_circle_mcp_workflow.py" in commands


def test_ci_packages_and_validates_both_unsigned_bridge_targets() -> None:
    commands = "\n".join(str(step.get("run", "")) for step in _windows_steps())

    assert "scripts/package_release.py" in commands
    assert "15AD106E-4705-4CB7-9538-1621587CF860" in commands
    assert "AutoCADHarness-R25.0-0.3.0.0-DEVELOPMENT-UNSIGNED.zip" in commands
    assert "AutoCADHarness-R26.0-0.3.0.0-DEVELOPMENT-UNSIGNED.zip" in commands
    assert commands.count("-Action Validate") == 2
    assert "SHA256SUMS.txt" in commands


def test_ci_uploads_one_short_lived_versioned_artifact() -> None:
    upload_steps = [
        step
        for step in _windows_steps()
        if step.get("uses") == "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0"
    ]

    assert len(upload_steps) == 1
    release_condition = (
        "github.event_name == 'pull_request' && github.head_ref == 'codex/v0.3.0-quick-edit'"
    )
    package_step = next(
        step
        for step in _windows_steps()
        if step.get("name") == "Package and validate engineering preview"
    )
    assert package_step["if"] == release_condition
    assert upload_steps[0]["if"] == release_condition
    settings = upload_steps[0]["with"]
    assert settings["name"] == "v0.3.0-engineering-preview"
    assert settings["retention-days"] == 3
    assert settings["if-no-files-found"] == "error"
