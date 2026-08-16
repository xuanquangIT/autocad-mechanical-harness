from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.check_codex_mcp_installation import inspect_installation

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "check_codex_mcp_installation.py"
CONFIRMATIONS = (
    "open_target_drawing,load_company_standards,install_bridge_bundle,"
    "grant_named_pipe_acl,confirm_autocad_version"
)


def _write_runtime_config(
    repository: Path,
    *,
    adapter: str = "dotnet_bridge",
    local_only: bool = True,
    launch_autocad_if_missing: bool = False,
) -> None:
    config = repository / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "live-r26-acceptance.yaml").write_text(
        "\n".join(
            (
                "app:",
                f"  local_only: {str(local_only).lower()}",
                "adapter:",
                f"  type: {adapter}",
                f"  launch_autocad_if_missing: {str(launch_autocad_if_missing).lower()}",
            )
        ),
        encoding="utf-8",
    )


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _write_codex_config(
    path: Path,
    repository: Path,
    *,
    secret: str | None = "doctor-test-secret",
    confirmations: str | None = CONFIRMATIONS,
    environment_overrides: dict[str, str] | None = None,
    config_path_override: Path | None = None,
) -> None:
    config_path = config_path_override or repository / "config" / "live-r26-acceptance.yaml"
    lines = [
        "[mcp_servers.autocad-mechanical-harness]",
        f"command = {_toml_string(r'C:\\Tools\\uv.exe')}",
        "args = ["
        + ", ".join(
            _toml_string(value)
            for value in ("--directory", str(repository), "run", "cad-harness-mcp")
        )
        + "]",
        "enabled = true",
        "",
        "[mcp_servers.autocad-mechanical-harness.env]",
        f"CAD_HARNESS_CONFIG = {_toml_string(str(config_path))}",
    ]
    if confirmations is not None:
        lines.append(f"CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS = {_toml_string(confirmations)}")
    if secret is not None:
        lines.append(f"CAD_HARNESS_APPROVAL_SECRET = {_toml_string(secret)}")
    for name, value in (environment_overrides or {}).items():
        lines.append(f"{name} = {_toml_string(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_bundle(
    root: Path, name: str, *, version: str, series: str, unsigned: bool = False
) -> Path:
    bundle = root / name
    bundle.mkdir(parents=True)
    (bundle / "PackageContents.xml").write_text(
        (
            f'<ApplicationPackage AppVersion="{version}" '
            'Name="AutoCAD Mechanical Harness Bridge" '
            'ProductCode="{246FD1B2-83D8-4AAB-9EA4-C86AB9ECCDF2}" '
            'UpgradeCode="{FA1366B0-8CAB-42B6-B5A2-66D3EF37F0A5}"><Components>'
            f'<RuntimeRequirements SeriesMin="{series}" SeriesMax="{series}" />'
            '<ComponentEntry AppName="AutoCADHarnessBridge" '
            'ModuleName="./Contents/Windows/AutoCADHarness.dll" />'
            "</Components></ApplicationPackage>"
        ),
        encoding="utf-8",
    )
    module = bundle / "Contents" / "Windows" / "AutoCADHarness.dll"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"test-bridge-module")
    if unsigned:
        (bundle / "DEVELOPMENT-UNSIGNED.txt").write_text("DEVELOPMENT-UNSIGNED\n", encoding="utf-8")
    return bundle


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repository = tmp_path / "private-repository-path"
    global_root = tmp_path / "private-global-plugin-path"
    workspace_root = tmp_path / "private-workspace-bundle-path"
    global_root.mkdir()
    workspace_root.mkdir()
    _write_runtime_config(repository)
    codex_config = tmp_path / "private-codex-config.toml"
    _write_codex_config(codex_config, repository)
    return repository, global_root, workspace_root, codex_config


def _inspect(
    repository: Path, global_root: Path, workspace_root: Path, codex_config: Path
) -> dict[str, object]:
    return inspect_installation(
        codex_config=codex_config,
        repository_root=repository,
        application_plugins_root=global_root,
        workspace_bundle_root=workspace_root,
    )


def test_marker_absence_is_not_misreported_as_authenticode_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, global_root, workspace_root, codex_config = _fixture(tmp_path)
    _write_codex_config(codex_config, repository, secret=None, confirmations=None)
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "inherited-doctor-test-secret")
    _make_bundle(global_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")
    _make_bundle(workspace_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")

    report = _inspect(repository, global_root, workspace_root, codex_config)

    assert report["ok"] is False
    assert report["codes"] == [
        "GLOBAL_BUNDLE_AUTHENTICODE_UNVERIFIED",
        "WORKSPACE_BUNDLE_AUTHENTICODE_UNVERIFIED",
    ]
    assert report["registration"] == {
        "found": True,
        "enabled": True,
        "command_exact": True,
        "args_exact": True,
        "config_env_exact": True,
        "static_manual_confirmations_present": False,
        "manual_confirmations_exact": False,
        "live_write_requested": False,
        "live_session_proof_registered": False,
        "approval_secret_registered": False,
        "approval_secret_inherited": True,
    }
    serialized = json.dumps(report, sort_keys=True)
    assert report["bundles"]["global"]["unsigned_marker_absent_count"] == 1
    assert report["bundles"]["global"]["authenticode_verification_performed"] is False
    assert "signed_like" not in serialized
    assert "inherited-doctor-test-secret" not in serialized
    assert str(tmp_path) not in serialized


def test_duplicate_unsigned_and_workspace_drift_fail_closed(tmp_path: Path) -> None:
    repository, global_root, workspace_root, codex_config = _fixture(tmp_path)
    _make_bundle(global_root, "first.bundle", version="2.0.0.0", series="R26.0")
    _make_bundle(global_root, "second.bundle", version="2.1.0.0", series="R26.0", unsigned=True)
    _make_bundle(workspace_root, "source.bundle", version="1.0.0.0", series="R25.0")

    report = _inspect(repository, global_root, workspace_root, codex_config)

    assert report["ok"] is False
    assert {
        "GLOBAL_BUNDLE_MULTIPLE",
        "GLOBAL_BUNDLE_DEVELOPMENT_UNSIGNED",
        "BUNDLE_VERSION_DRIFT",
        "BUNDLE_SERIES_DRIFT",
    }.issubset(set(report["codes"]))


def test_static_confirmations_and_persisted_secret_are_not_session_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, global_root, workspace_root, codex_config = _fixture(tmp_path)
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "inherited-secret")
    _make_bundle(global_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")
    _make_bundle(workspace_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")

    report = _inspect(repository, global_root, workspace_root, codex_config)

    assert report["ok"] is False
    assert "MANUAL_CONFIRMATIONS_STATIC_UNBOUND" in report["codes"]
    assert "APPROVAL_SECRET_STORED_IN_CLIENT_CONFIG" in report["codes"]


def test_fake_runtime_config_fails_closed_without_requiring_a_read_only_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, global_root, workspace_root, codex_config = _fixture(tmp_path)
    _write_runtime_config(repository, adapter="fake")
    _write_codex_config(codex_config, repository, secret=None)
    monkeypatch.delenv("CAD_HARNESS_APPROVAL_SECRET", raising=False)
    _make_bundle(global_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")
    _make_bundle(workspace_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")

    report = _inspect(repository, global_root, workspace_root, codex_config)

    assert report["ok"] is False
    assert "DOTNET_BRIDGE_REQUIRED" in report["codes"]
    assert "APPROVAL_SECRET_MISSING" not in report["codes"]
    assert report["runtime_config"] == {
        "loaded": True,
        "effective_loaded": True,
        "local_only": True,
        "dotnet_bridge": False,
        "launch_autocad_if_missing_disabled": True,
        "safety_override_present": False,
        "safety_override_changed": False,
    }


def test_registered_safety_overrides_are_applied_to_effective_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, global_root, workspace_root, codex_config = _fixture(tmp_path)
    _write_codex_config(
        codex_config,
        repository,
        secret=None,
        confirmations=None,
        environment_overrides={
            "CAD_HARNESS_ADAPTER": "fake",
            "CAD_HARNESS_LOCAL_ONLY": "false",
        },
    )
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "inherited-secret")
    _make_bundle(global_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")
    _make_bundle(workspace_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")

    report = _inspect(repository, global_root, workspace_root, codex_config)

    assert report["runtime_config"] == {
        "loaded": True,
        "effective_loaded": True,
        "local_only": False,
        "dotnet_bridge": False,
        "launch_autocad_if_missing_disabled": True,
        "safety_override_present": True,
        "safety_override_changed": True,
    }
    assert {
        "LOCAL_ONLY_REQUIRED",
        "DOTNET_BRIDGE_REQUIRED",
        "RUNTIME_SAFETY_OVERRIDE_CHANGED",
    }.issubset(set(report["codes"]))


def test_registered_write_or_session_proof_is_rejected_as_stale_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, global_root, workspace_root, codex_config = _fixture(tmp_path)
    _write_codex_config(
        codex_config,
        repository,
        secret=None,
        confirmations=None,
        environment_overrides={
            "CAD_HARNESS_LIVE_WRITE_VERIFIED": "1",
            "CAD_HARNESS_LIVE_SESSION_PROOF": "lsp2.persisted.invalid",
        },
    )
    monkeypatch.delenv("CAD_HARNESS_APPROVAL_SECRET", raising=False)
    _make_bundle(global_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")
    _make_bundle(workspace_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")

    report = _inspect(repository, global_root, workspace_root, codex_config)

    assert {
        "REGISTERED_MCP_WRITE_REQUIRES_EPHEMERAL_LAUNCHER",
        "LIVE_SESSION_PROOF_STORED_IN_CLIENT_CONFIG",
        "APPROVAL_SECRET_MISSING",
    }.issubset(set(report["codes"]))
    assert report["registration"]["live_write_requested"] is True
    assert report["registration"]["live_session_proof_registered"] is True


def test_wrong_registered_config_path_and_launch_enabled_fail_closed(tmp_path: Path) -> None:
    repository, global_root, workspace_root, codex_config = _fixture(tmp_path)
    _write_runtime_config(repository, launch_autocad_if_missing=True)
    _write_codex_config(
        codex_config,
        repository,
        config_path_override=repository / "config" / "wrong.yaml",
    )
    _make_bundle(global_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")
    _make_bundle(workspace_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")

    report = _inspect(repository, global_root, workspace_root, codex_config)

    assert report["registration"]["config_env_exact"] is False
    assert report["runtime_config"]["launch_autocad_if_missing_disabled"] is False
    assert "MCP_CONFIG_ENV_MISMATCH" in report["codes"]
    assert "AUTOCAD_LAUNCH_MUST_BE_DISABLED" in report["codes"]


def test_unrelated_autodesk_bundle_is_ignored(tmp_path: Path) -> None:
    repository, global_root, workspace_root, codex_config = _fixture(tmp_path)
    _make_bundle(global_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")
    _make_bundle(workspace_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")
    unrelated = global_root / "Autodesk.Sample.bundle"
    unrelated.mkdir()
    (unrelated / "PackageContents.xml").write_text(
        (
            '<ApplicationPackage AppVersion="99.0.0" Name="Autodesk Sample" '
            'ProductCode="{A62D4D0A-269A-4F84-A5F6-32BE1C553CE2}" '
            'UpgradeCode="{20C7D79D-D464-42F9-9345-F40A51D06B10}"><Components>'
            '<RuntimeRequirements SeriesMin="R99.0" SeriesMax="R99.0" />'
            '<ComponentEntry AppName="AutodeskSample" ModuleName="./sample.dll" />'
            "</Components></ApplicationPackage>"
        ),
        encoding="utf-8",
    )

    report = _inspect(repository, global_root, workspace_root, codex_config)

    global_inventory = report["bundles"]["global"]
    assert global_inventory["candidate_count"] == 2
    assert global_inventory["active_count"] == 1
    assert global_inventory["unrelated_ignored_count"] == 1
    assert "GLOBAL_BUNDLE_MULTIPLE" not in report["codes"]


def test_harness_manifest_without_expected_module_is_not_active(tmp_path: Path) -> None:
    repository, global_root, workspace_root, codex_config = _fixture(tmp_path)
    bundle = _make_bundle(global_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")
    (bundle / "Contents" / "Windows" / "AutoCADHarness.dll").unlink()
    _make_bundle(workspace_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")

    report = _inspect(repository, global_root, workspace_root, codex_config)

    assert report["bundles"]["global"]["active_count"] == 0
    assert report["bundles"]["global"]["module_missing_count"] == 1
    assert "GLOBAL_BUNDLE_MODULE_MISSING" in report["codes"]
    assert "GLOBAL_BUNDLE_NONE" in report["codes"]


def test_reparse_bundle_is_rejected(tmp_path: Path) -> None:
    repository, global_root, workspace_root, codex_config = _fixture(tmp_path)
    target = tmp_path / "outside-bundle"
    _make_bundle(tmp_path, target.name, version="2.4.0.0", series="R26.0")
    alias = global_root / "linked.bundle"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip("directory reparse points are unavailable")
    _make_bundle(workspace_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")
    try:
        report = _inspect(repository, global_root, workspace_root, codex_config)
        assert report["ok"] is False
        assert "GLOBAL_BUNDLE_REPARSE_REJECTED" in report["codes"]
        assert report["bundles"]["global"]["active_count"] == 0
    finally:
        alias.rmdir() if not alias.is_symlink() else alias.unlink()


def test_malformed_toml_and_xml_return_stable_codes_without_leaks(tmp_path: Path) -> None:
    repository, global_root, workspace_root, codex_config = _fixture(tmp_path)
    secret = "must-never-appear"
    codex_config.write_text(f"broken = [{secret}\n", encoding="utf-8")
    malformed = global_root / "malformed.bundle"
    malformed.mkdir()
    (malformed / "PackageContents.xml").write_text("<private-path", encoding="utf-8")
    _make_bundle(workspace_root, "AutoCADHarness.bundle", version="2.4.0.0", series="R26.0")

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--codex-config",
            str(codex_config),
            "--repository-root",
            str(repository),
            "--application-plugins-root",
            str(global_root),
            "--workspace-bundle-root",
            str(workspace_root),
        ],
        cwd=REPOSITORY_ROOT,
        env={
            key: value for key, value in os.environ.items() if key != "CAD_HARNESS_APPROVAL_SECRET"
        },
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert "CODEX_CONFIG_MALFORMED" in report["codes"]
    assert "GLOBAL_BUNDLE_MANIFEST_MALFORMED" in report["codes"]
    assert completed.stderr == ""
    assert secret not in completed.stdout
    assert str(tmp_path) not in completed.stdout
