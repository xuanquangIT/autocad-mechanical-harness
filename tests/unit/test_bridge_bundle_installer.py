from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPOSITORY_ROOT / "dotnet" / "AutoCADBridge" / "Install-BridgeBundle.ps1"
POWERSHELL = shutil.which("pwsh")
BUNDLE_NAME = "AutoCADHarness.bundle"
CHECKSUM_NAME = "SHA256SUMS.ps1"
RECEIPT_NAME = "CAD-HARNESS-INSTALL-RECEIPT.json"
LOCK_NAME = ".cad-harness-installer.lock"
JOURNAL_NAME = ".cad-harness-installer-journal.json"
JOURNAL_KEY_NAME = ".cad-harness-installer-journal.key"
UPGRADE_CODE = "{FA1366B0-8CAB-42B6-B5A2-66D3EF37F0A5}"
REQUIRED_ASSEMBLIES = (
    "AutoCADHarness.dll",
    "CadBridge.Contracts.dll",
    "CadBridge.Execution.dll",
    "CadBridge.Hosting.dll",
    "CadBridge.Inspection.dll",
    "CadBridge.Ipc.dll",
    "CadBridge.Metadata.dll",
)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="The bridge bundle installer requires PowerShell 7.2 or newer on Windows.",
)


def _product_code(version: str) -> str:
    values = {
        "1.0.0.0": "{11111111-1111-4111-8111-111111111111}",
        "2.0.0.0": "{22222222-2222-4222-8222-222222222222}",
    }
    return values[version]


def _manifest(*, version: str, series: str, product_code: str) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<ApplicationPackage SchemaVersion="1.0" AppVersion="{version}"
  Author="AutoCAD Mechanical Harness Team"
  Name="AutoCAD Mechanical Harness Bridge"
  Description="Local per-user Named Pipe bridge for atomic mechanical drawing jobs."
  ProductCode="{product_code}" UpgradeCode="{UPGRADE_CODE}">
  <CompanyDetails Name="AutoCAD Mechanical Harness Team"
    Email="maintainers@cad-harness.local" />
  <Components>
    <RuntimeRequirements OS="Win64" Platform="AutoCAD*"
      SeriesMin="{series}" SeriesMax="{series}" />
    <ComponentEntry AppName="AutoCADHarnessBridge"
      AppDescription="Atomic local bridge for the AutoCAD Mechanical Harness"
      AppType=".Net" ModuleName="./Contents/Windows/AutoCADHarness.dll"
      PerDocument="False" LoadReasons="LoadOnAutoCADStartup">
      <Commands GroupName="CADHARNESS">
        <Command Global="CADHARNESSSTATUS" Local="CADHARNESSSTATUS" />
      </Commands>
    </ComponentEntry>
  </Components>
</ApplicationPackage>
'''


def _write_checksums(bundle: Path) -> None:
    files = sorted(
        path
        for path in bundle.rglob("*")
        if path.is_file() and path.name not in {CHECKSUM_NAME, RECEIPT_NAME}
    )
    lines = []
    for path in files:
        relative = path.relative_to(bundle).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"# SHA256 {digest} *{relative}")
    (bundle / CHECKSUM_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _bundle_hashes(bundle: Path) -> dict[str, str]:
    return {
        path.relative_to(bundle).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in bundle.rglob("*")
        if path.is_file()
    }


def _make_bundle(
    parent: Path,
    *,
    version: str = "1.0.0.0",
    series: str = "R26.0",
    development_unsigned: bool = True,
) -> Path:
    bundle = parent / BUNDLE_NAME
    windows = bundle / "Contents" / "Windows"
    windows.mkdir(parents=True)
    (bundle / "PackageContents.xml").write_text(
        _manifest(version=version, series=series, product_code=_product_code(version)),
        encoding="utf-8",
    )
    for index, assembly in enumerate(REQUIRED_ASSEMBLIES):
        (windows / assembly).write_bytes(f"synthetic-{version}-{index}\n".encode())
    if development_unsigned:
        (bundle / "DEVELOPMENT-UNSIGNED.txt").write_text(
            "DEVELOPMENT-UNSIGNED\n"
            "Not a release artifact. Do not deploy outside an isolated test workstation.\n",
            encoding="utf-8",
        )
    _write_checksums(bundle)
    return bundle


def _invoke(
    *,
    action: str,
    bundle: Path | None = None,
    install_root: Path | str | None,
    development_unsigned: bool = True,
    upgrade: bool = False,
    allow_running_autocad: bool = False,
    expected_series: str = "R26.0",
    test_fault: str = "None",
    test_hold_mutex_milliseconds: int = 0,
    test_precommit_barrier_path: Path | None = None,
    test_signature_policy_fixture: Path | None = None,
    expect_json: bool = True,
    timeout: float = 30,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    assert POWERSHELL is not None
    command = [
        POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(INSTALLER),
        "-Action",
        action,
        "-ExpectedAutoCADSeries",
        expected_series,
    ]
    if bundle is not None:
        command.extend(("-BundlePath", str(bundle)))
    if install_root is not None:
        command.extend(("-InstallRoot", str(install_root)))
    if development_unsigned:
        command.append("-DevelopmentUnsigned")
    if upgrade:
        command.append("-Upgrade")
    if allow_running_autocad:
        command.append("-AllowRunningAutoCADForDevelopmentTest")
    if test_fault != "None":
        command.extend(("-DevelopmentTestFault", test_fault))
    if test_hold_mutex_milliseconds:
        command.extend(
            (
                "-DevelopmentTestHoldMutexMilliseconds",
                str(test_hold_mutex_milliseconds),
            )
        )
    if test_precommit_barrier_path is not None:
        command.extend(
            (
                "-DevelopmentTestPreCommitBarrierPath",
                str(test_precommit_barrier_path),
            )
        )
    if test_signature_policy_fixture is not None:
        command.extend(
            (
                "-DevelopmentTestSignaturePolicyFixture",
                str(test_signature_policy_fixture),
            )
        )

    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    if not expect_json:
        return completed, {}
    output = completed.stdout if completed.returncode == 0 else completed.stderr
    lines = [line for line in output.splitlines() if line.strip()]
    assert len(lines) == 1, (
        f"unexpected PowerShell output: rc={completed.returncode}; "
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return completed, payload


def _assert_success(
    result: tuple[subprocess.CompletedProcess[str], dict[str, Any]], action: str
) -> dict[str, Any]:
    completed, payload = result
    assert completed.returncode == 0, completed.stderr
    assert set(payload) == {
        "ok",
        "action",
        "artifact_kind",
        "autocad_series",
        "app_version",
        "product_code",
        "file_count",
        "publication_status",
        "verification_status",
        "cleanup_status",
        "recovery_status",
    }
    assert payload["ok"] is True
    assert payload["action"] == action
    assert payload["artifact_kind"] == "DEVELOPMENT-UNSIGNED"
    assert payload["autocad_series"] == "R26.0"
    if action == "validated":
        assert payload["publication_status"] == "not_applicable"
        assert payload["verification_status"] == "verified"
        assert payload["cleanup_status"] == "not_applicable"
    elif action == "uninstalled":
        assert payload["publication_status"] == "removed"
        assert payload["verification_status"] == "verified_before_commit"
        assert payload["cleanup_status"] == "complete"
    else:
        assert payload["publication_status"] == "published"
        assert payload["verification_status"] == "verified"
        assert payload["cleanup_status"] == "complete"
    assert payload["recovery_status"] in {"none", "completed"}
    return payload


def _assert_failure(
    result: tuple[subprocess.CompletedProcess[str], dict[str, Any]], error: str
) -> None:
    completed, payload = result
    assert completed.returncode == 2
    assert payload == {"ok": False, "error": error}
    assert "/" not in error and "\\" not in error


def _development_install(
    bundle: Path,
    install_root: Path,
    *,
    upgrade: bool = False,
    test_fault: str = "None",
    test_hold_mutex_milliseconds: int = 0,
    test_precommit_barrier_path: Path | None = None,
    test_signature_policy_fixture: Path | None = None,
    expect_json: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    return _invoke(
        action="Install",
        bundle=bundle,
        install_root=install_root,
        upgrade=upgrade,
        allow_running_autocad=True,
        test_fault=test_fault,
        test_hold_mutex_milliseconds=test_hold_mutex_milliseconds,
        test_precommit_barrier_path=test_precommit_barrier_path,
        test_signature_policy_fixture=test_signature_policy_fixture,
        expect_json=expect_json,
    )


def _development_uninstall(
    install_root: Path,
    *,
    test_fault: str = "None",
    test_signature_policy_fixture: Path | None = None,
    expect_json: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    return _invoke(
        action="Uninstall",
        install_root=install_root,
        allow_running_autocad=True,
        test_fault=test_fault,
        test_signature_policy_fixture=test_signature_policy_fixture,
        expect_json=expect_json,
    )


def _write_signature_policy_fixture(
    path: Path,
    *,
    publisher: str = "CN=AutoCAD Mechanical Harness Release, O=Harness Test",
    timestamp: str = "2024-06-01T00:00:00.0000000+00:00",
    timestamp_trusted: bool = True,
    current_chain_trusted: bool = True,
    installer_signer_id: str = "release-2025",
    previous_signer_id: str = "release-2024",
    thumbprint: str = "A1" * 20,
) -> Path:
    public_key = "ab" * 32
    fixture = {
        "Facts": {
            "Status": "Valid",
            "Publisher": publisher,
            "PublicKeySha256": public_key,
            "Thumbprint": thumbprint,
            "CodeSigningEku": True,
            "SignerNotBeforeUtc": "2024-01-01T00:00:00.0000000+00:00",
            "SignerNotAfterUtc": "2025-01-01T00:00:00.0000000+00:00",
            "TimestampUtc": timestamp,
            "TimestampTrusted": timestamp_trusted,
            "CurrentChainTrusted": current_chain_trusted,
        },
        "ApprovedSigners": [
            {
                "Id": "release-2024",
                "Publisher": "CN=AutoCAD Mechanical Harness Legacy, O=Harness Test",
                "PublicKeySha256": "cd" * 32,
                "AllowedPreviousSignerIds": [],
            },
            {
                "Id": "release-2025",
                "Publisher": "CN=AutoCAD Mechanical Harness Release, O=Harness Test",
                "PublicKeySha256": public_key,
                "AllowedPreviousSignerIds": ["release-2024"],
            },
        ],
        "InstallerSignerId": installer_signer_id,
        "PreviousSignerId": previous_signer_id,
    }
    path.write_text(json.dumps(fixture), encoding="utf-8")
    return path


def test_validate_install_no_overwrite_and_uninstall_are_owned(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source")
    source_hashes = _bundle_hashes(bundle)
    install_root = tmp_path / "isolated-install-root"
    sibling = install_root / "unrelated-owner.txt"

    validated = _assert_success(
        _invoke(action="Validate", bundle=bundle, install_root=install_root),
        "validated",
    )
    assert validated["file_count"] == 10

    installed = _assert_success(_development_install(bundle, install_root), "installed")
    assert installed["app_version"] == "1.0.0.0"
    destination = install_root / BUNDLE_NAME
    sibling.write_bytes(b"must-survive")
    assert (destination / RECEIPT_NAME).is_file()
    assert sibling.read_bytes() == b"must-survive"

    _assert_failure(
        _development_install(bundle, install_root),
        "BUNDLE_ALREADY_INSTALLED",
    )
    assert sibling.read_bytes() == b"must-survive"

    _assert_success(_development_uninstall(install_root), "uninstalled")
    assert not destination.exists()
    assert sibling.read_bytes() == b"must-survive"
    assert not list(install_root.glob(".AutoCADHarness.bundle.*"))

    destination.write_bytes(b"unowned-file")
    _assert_failure(
        _development_install(bundle, install_root),
        "BUNDLE_ALREADY_INSTALLED",
    )
    assert destination.read_bytes() == b"unowned-file"
    assert not list(install_root.glob(".AutoCADHarness.bundle.*"))
    assert _bundle_hashes(bundle) == source_hashes


def test_upgrade_requires_a_newer_version_and_new_product_code(tmp_path: Path) -> None:
    first = _make_bundle(tmp_path / "source-v1", version="1.0.0.0")
    second = _make_bundle(tmp_path / "source-v2", version="2.0.0.0")
    install_root = tmp_path / "upgrade-root"
    install_root.mkdir()

    _assert_success(_development_install(first, install_root), "installed")
    _assert_failure(
        _development_install(first, install_root, upgrade=True),
        "UPGRADE_VERSION_NOT_NEWER",
    )
    upgraded = _assert_success(
        _development_install(second, install_root, upgrade=True),
        "upgraded",
    )
    assert upgraded["app_version"] == "2.0.0.0"
    manifest = (install_root / BUNDLE_NAME / "PackageContents.xml").read_text(encoding="utf-8")
    assert 'AppVersion="2.0.0.0"' in manifest
    assert _product_code("2.0.0.0") in manifest
    assert not list(install_root.glob(".AutoCADHarness.bundle.*"))


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    (
        ("tamper", "CHECKSUM_MISMATCH"),
        ("traversal", "CHECKSUM_PATH_INVALID"),
        ("duplicate", "CHECKSUM_DUPLICATE"),
        ("missing", "CHECKSUM_COVERAGE_INVALID"),
    ),
)
def test_checksum_contract_fails_closed(
    tmp_path: Path, corruption: str, expected_error: str
) -> None:
    bundle = _make_bundle(tmp_path / corruption)
    checksum = bundle / CHECKSUM_NAME
    lines = checksum.read_text(encoding="utf-8").splitlines()
    if corruption == "tamper":
        (bundle / "Contents" / "Windows" / REQUIRED_ASSEMBLIES[0]).write_bytes(b"tampered")
    elif corruption == "traversal":
        lines.insert(0, f"# SHA256 {'0' * 64} *../escape.dll")
        checksum.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif corruption == "duplicate":
        lines.append(lines[0])
        checksum.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        checksum.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")

    install_root = tmp_path / "validate-root"
    install_root.mkdir()
    result = _invoke(action="Validate", bundle=bundle, install_root=install_root)
    _assert_failure(result, expected_error)
    assert str(tmp_path) not in result[0].stderr


def test_release_bundle_requires_valid_timestamped_authenticode(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "release", development_unsigned=False)
    install_root = tmp_path / "release-validation-root"
    install_root.mkdir()

    result = _invoke(
        action="Validate",
        bundle=bundle,
        install_root=install_root,
        development_unsigned=False,
    )
    _assert_failure(result, "SIGNATURE_INVALID")


def test_r25_bundle_is_accepted_only_with_an_explicit_r25_target(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "r25", series="R25.0")
    install_root = tmp_path / "r25-validation-root"
    install_root.mkdir()
    completed, payload = _invoke(
        action="Validate",
        bundle=bundle,
        install_root=install_root,
        expected_series="R25.0",
    )
    assert completed.returncode == 0, completed.stderr
    assert payload["ok"] is True
    assert payload["autocad_series"] == "R25.0"


@pytest.mark.parametrize(
    ("defect", "expected_error"),
    (
        ("series", "PACKAGE_SERIES_MISMATCH"),
        ("component", "PACKAGE_COMPONENT_INVALID"),
        ("assembly", "REQUIRED_ASSEMBLY_MISSING"),
        ("extra-component", "PACKAGE_MANIFEST_INVALID"),
    ),
)
def test_manifest_and_required_assembly_contract_is_exact(
    tmp_path: Path, defect: str, expected_error: str
) -> None:
    series = "R25.0" if defect == "series" else "R26.0"
    bundle = _make_bundle(tmp_path / defect, series=series)
    manifest_path = bundle / "PackageContents.xml"
    if defect == "component":
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                'GroupName="CADHARNESS"', 'GroupName="UNOWNED"'
            ),
            encoding="utf-8",
        )
        _write_checksums(bundle)
    elif defect == "assembly":
        (bundle / "Contents" / "Windows" / REQUIRED_ASSEMBLIES[-1]).unlink()
    elif defect == "extra-component":
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                "  </Components>", "    <UnownedComponent />\n  </Components>"
            ),
            encoding="utf-8",
        )
        _write_checksums(bundle)

    install_root = tmp_path / "manifest-root"
    install_root.mkdir()
    _assert_failure(
        _invoke(action="Validate", bundle=bundle, install_root=install_root),
        expected_error,
    )


def test_development_switch_custom_root_and_running_bypass_are_constrained(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    custom_root = tmp_path / "custom-root"
    custom_root.mkdir()

    _assert_failure(
        _invoke(
            action="Validate",
            bundle=bundle,
            install_root=custom_root,
            development_unsigned=False,
        ),
        "DEVELOPMENT_SWITCH_REQUIRED",
    )
    _assert_failure(
        _invoke(action="Validate", bundle=bundle, install_root=None),
        "DEVELOPMENT_CUSTOM_ROOT_REQUIRED",
    )
    _assert_failure(
        _invoke(
            action="Install",
            bundle=bundle,
            install_root=custom_root,
            development_unsigned=False,
            allow_running_autocad=True,
        ),
        "AUTOCAD_BYPASS_NOT_ALLOWED",
    )


@pytest.mark.parametrize(
    "install_root",
    (
        r"\\invalid.example\never-contact",
        r"\\?\C:\never-contact",
        r"\\.\C:\never-contact",
    ),
    ids=("unc", "extended", "device"),
)
def test_unc_and_device_install_roots_are_rejected_without_access(
    tmp_path: Path, install_root: str
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    _assert_failure(
        _invoke(
            action="Validate",
            bundle=bundle,
            install_root=install_root,
        ),
        "INSTALL_ROOT_INVALID",
    )


def test_uninstall_refuses_modified_or_unowned_content(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source")
    install_root = tmp_path / "ownership-root"
    install_root.mkdir()
    sibling = install_root / "another.bundle"
    sibling.mkdir()
    sibling_marker = sibling / "owner.txt"
    sibling_marker.write_bytes(b"other-owner")
    _assert_success(_development_install(bundle, install_root), "installed")

    destination = install_root / BUNDLE_NAME
    receipt_path = destination / RECEIPT_NAME
    original_receipt = receipt_path.read_bytes()
    receipt = json.loads(original_receipt)
    receipt["Owner"] = "different-owner"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    _assert_failure(_development_uninstall(install_root), "INSTALL_RECEIPT_INVALID")
    assert destination.is_dir()
    assert sibling_marker.read_bytes() == b"other-owner"

    receipt_path.write_bytes(original_receipt)
    rogue = destination / "not-in-owned-manifest.bin"
    rogue.write_bytes(b"unowned")
    _assert_failure(_development_uninstall(install_root), "CHECKSUM_COVERAGE_INVALID")
    assert rogue.read_bytes() == b"unowned"
    assert sibling_marker.read_bytes() == b"other-owner"


def test_receipt_binds_directories_and_rejects_ads(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source")
    install_root = tmp_path / "inventory-root"
    _assert_success(_development_install(bundle, install_root), "installed")
    destination = install_root / BUNDLE_NAME

    rogue_directory = destination / "Contents" / "empty-rogue"
    rogue_directory.mkdir()
    _assert_failure(_development_uninstall(install_root), "INSTALL_RECEIPT_INVALID")
    rogue_directory.rmdir()

    assembly = destination / "Contents" / "Windows" / REQUIRED_ASSEMBLIES[0]
    ads_path = Path(f"{assembly}:rogue")
    try:
        ads_path.write_bytes(b"not-owned")
    except OSError as exc:
        pytest.skip(f"NTFS alternate streams unavailable in this temp volume: {exc}")
    _assert_failure(
        _development_uninstall(install_root),
        "ALTERNATE_DATA_STREAM_NOT_ALLOWED",
    )
    ads_path.unlink()
    _assert_success(_development_uninstall(install_root), "uninstalled")


def test_native_ads_inspection_accepts_a_clean_directory(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source")
    (bundle / "Contents" / "clean-empty-directory").mkdir()

    _assert_success(
        _invoke(
            action="Validate",
            bundle=bundle,
            install_root=tmp_path / "validation-root",
        ),
        "validated",
    )


@pytest.mark.parametrize(
    "relative_target",
    (Path("Contents"), Path("Contents") / "Windows" / REQUIRED_ASSEMBLIES[0]),
    ids=("directory", "file"),
)
def test_native_ads_inspection_rejects_directory_and_file_ads(
    tmp_path: Path, relative_target: Path
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    target = bundle / relative_target
    ads_path = Path(f"{target}:rogue")
    try:
        ads_path.write_bytes(b"not-owned")
    except OSError as exc:
        pytest.skip(f"NTFS alternate streams unavailable in this temp volume: {exc}")

    try:
        _assert_failure(
            _invoke(
                action="Validate",
                bundle=bundle,
                install_root=tmp_path / "validation-root",
            ),
            "ALTERNATE_DATA_STREAM_NOT_ALLOWED",
        )
    finally:
        ads_path.unlink()


@pytest.mark.parametrize(
    "fault",
    ("InstallAfterPrepared", "InstallAfterPublishBeforeJournal"),
)
def test_fresh_install_crash_recovery_is_deterministic(tmp_path: Path, fault: str) -> None:
    bundle = _make_bundle(tmp_path / "source")
    install_root = tmp_path / f"crash-{fault}"

    crashed, _ = _development_install(
        bundle,
        install_root,
        test_fault=fault,
        expect_json=False,
    )
    assert crashed.returncode == 91
    assert (install_root / ".cad-harness-installer-journal.json").is_file()

    recovered = _assert_success(
        _development_install(bundle, install_root),
        "installed",
    )
    assert (install_root / BUNDLE_NAME).is_dir()
    assert not (install_root / ".cad-harness-installer-journal.json").exists()
    assert not list(install_root.glob(".AutoCADHarness.bundle.*"))
    expected_recovery = "none" if fault == "InstallAfterPrepared" else "completed"
    assert recovered["recovery_status"] == expected_recovery


@pytest.mark.parametrize(
    "fault",
    (
        "UpgradeAfterOldRenameBeforeJournal",
        "UpgradeAfterPublishBeforeJournal",
    ),
)
def test_upgrade_crash_recovery_preserves_one_verified_active_bundle(
    tmp_path: Path, fault: str
) -> None:
    first = _make_bundle(tmp_path / "source-v1", version="1.0.0.0")
    second = _make_bundle(tmp_path / "source-v2", version="2.0.0.0")
    install_root = tmp_path / f"upgrade-crash-{fault}"
    _assert_success(_development_install(first, install_root), "installed")

    crashed, _ = _development_install(
        second,
        install_root,
        upgrade=True,
        test_fault=fault,
        expect_json=False,
    )
    assert crashed.returncode == 91
    recovered = _assert_success(
        _development_install(second, install_root, upgrade=True),
        "upgraded",
    )
    assert recovered["app_version"] == "2.0.0.0"
    expected_recovery = "none" if fault == "UpgradeAfterOldRenameBeforeJournal" else "completed"
    assert recovered["recovery_status"] == expected_recovery
    assert not list(install_root.glob(".AutoCADHarness.bundle.*"))
    assert not (install_root / ".cad-harness-installer-journal.json").exists()


@pytest.mark.parametrize("fault", ("UninstallAfterRenameBeforeJournal", "CleanupAfterOneDelete"))
def test_uninstall_commit_never_reactivates_partial_quarantine(tmp_path: Path, fault: str) -> None:
    bundle = _make_bundle(tmp_path / "source")
    install_root = tmp_path / f"uninstall-crash-{fault}"
    _assert_success(_development_install(bundle, install_root), "installed")

    crashed, _ = _development_uninstall(
        install_root,
        test_fault=fault,
        expect_json=False,
    )
    assert crashed.returncode == 91
    assert not (install_root / BUNDLE_NAME).exists()
    assert (install_root / ".cad-harness-installer-journal.json").is_file()

    recovered = _assert_success(_development_uninstall(install_root), "uninstalled")
    assert recovered["recovery_status"] == "completed"
    assert not (install_root / BUNDLE_NAME).exists()
    assert not list(install_root.glob(".AutoCADHarness.bundle.uninstall.*"))
    assert not (install_root / ".cad-harness-installer-journal.json").exists()


def test_uninstall_crash_recovery_supports_max_path_quarantine_assets(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    relative_asset = Path("Contents") / "Windows" / max(REQUIRED_ASSEMBLIES, key=len)
    quarantine_template = ".AutoCADHarness.bundle.uninstall." + ("0" * 32)
    one_character_probe = tmp_path / "x" / quarantine_template / relative_asset
    root_component_length = 1 + max(0, 260 - len(str(one_character_probe)))
    install_root = tmp_path / ("x" * root_component_length)
    expected_quarantine_asset = install_root / quarantine_template / relative_asset
    assert len(str(expected_quarantine_asset)) >= 260

    _assert_success(_development_install(bundle, install_root), "installed")
    crashed, _ = _development_uninstall(
        install_root,
        test_fault="UninstallAfterRenameBeforeJournal",
        expect_json=False,
    )
    assert crashed.returncode == 91

    quarantines = list(install_root.glob(".AutoCADHarness.bundle.uninstall.*"))
    assert len(quarantines) == 1
    quarantined_asset = quarantines[0] / relative_asset
    assert len(str(quarantined_asset)) >= 260
    assert quarantined_asset.is_file()

    recovered = _assert_success(_development_uninstall(install_root), "uninstalled")
    assert recovered["recovery_status"] == "completed"
    assert not list(install_root.glob(".AutoCADHarness.bundle.uninstall.*"))
    assert not (install_root / ".cad-harness-installer-journal.json").exists()


def test_authenticated_journal_rejects_tampering(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source")
    install_root = tmp_path / "tampered-journal-root"
    crashed, _ = _development_install(
        bundle,
        install_root,
        test_fault="InstallAfterPublishBeforeJournal",
        expect_json=False,
    )
    assert crashed.returncode == 91
    journal_path = install_root / ".cad-harness-installer-journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["Phase"] = "verified"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    _assert_failure(
        _development_install(bundle, install_root),
        "JOURNAL_AUTHENTICATION_FAILED",
    )
    assert (install_root / BUNDLE_NAME).is_dir()
    assert journal_path.is_file()


def test_canonical_root_mutex_serializes_publishers(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source")
    install_root = tmp_path / "mutex-root"
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            _development_install,
            bundle,
            install_root,
            test_hold_mutex_milliseconds=4000,
        )
        time.sleep(1.0)
        _assert_failure(
            _development_install(bundle, install_root),
            "INSTALL_ROOT_BUSY",
        )
        _assert_success(first.result(timeout=15), "installed")


def test_precommit_barrier_revalidates_bound_existing_inventory(
    tmp_path: Path,
) -> None:
    first_bundle = _make_bundle(tmp_path / "source-v1", version="1.0.0.0")
    second_bundle = _make_bundle(tmp_path / "source-v2", version="2.0.0.0")
    install_root = tmp_path / "precommit-root"
    _assert_success(_development_install(first_bundle, install_root), "installed")
    assembly = install_root / BUNDLE_NAME / "Contents" / "Windows" / REQUIRED_ASSEMBLIES[0]
    original = assembly.read_bytes()
    barrier = install_root / ".cad-harness-installer-test-barrier.0123456789abcdef0123456789abcdef"
    ready = Path(f"{barrier}.ready")
    release = Path(f"{barrier}.release")

    with ThreadPoolExecutor(max_workers=1) as executor:
        attempt = executor.submit(
            _development_install,
            second_bundle,
            install_root,
            upgrade=True,
            test_precommit_barrier_path=barrier,
        )
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.025)
        assert ready.is_file(), "installer did not reach the precommit barrier"
        assembly.write_bytes(b"changed-after-initial-validation")
        release.write_bytes(b"release")
        result = attempt.result(timeout=15)
    _assert_failure(result, "CHECKSUM_MISMATCH")
    assert (install_root / BUNDLE_NAME).is_dir()
    assembly.write_bytes(original)

    upgraded = _assert_success(
        _development_install(second_bundle, install_root, upgrade=True),
        "upgraded",
    )
    assert upgraded["app_version"] == "2.0.0.0"
    assert not ready.exists() and not release.exists()


def test_known_folder_is_not_derived_from_poisoned_appdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    assert POWERSHELL is not None
    known_folder = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Environment]::GetFolderPath([Environment+SpecialFolder]::ApplicationData)",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    default_plugins = Path(known_folder.stdout.strip()) / "Autodesk" / "ApplicationPlugins"
    poisoned = tmp_path / "attacker-appdata"
    poisoned.mkdir()
    monkeypatch.setenv("APPDATA", str(poisoned))

    _assert_failure(
        _invoke(action="Validate", bundle=bundle, install_root=None),
        "DEVELOPMENT_CUSTOM_ROOT_REQUIRED",
    )
    assert not (poisoned / "Autodesk" / "ApplicationPlugins").exists()

    custom_below_default = default_plugins / "unsafe-development"
    _assert_failure(
        _invoke(
            action="Validate",
            bundle=bundle,
            install_root=custom_below_default,
        ),
        "DEVELOPMENT_CUSTOM_ROOT_REQUIRED",
    )


def test_reparse_alias_install_root_is_rejected(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source")
    target = tmp_path / "identity-target"
    target.mkdir()
    alias = tmp_path / "identity-alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        junction = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if junction.returncode != 0:
            pytest.skip("directory symlink and junction creation are unavailable")
    try:
        _assert_failure(
            _invoke(action="Validate", bundle=bundle, install_root=alias),
            "REPARSE_POINT_NOT_ALLOWED",
        )
    finally:
        alias.rmdir()


@pytest.mark.parametrize(
    ("fixture_changes", "expected_error"),
    (
        (
            {"timestamp": "2026-01-01T00:00:00.0000000+00:00"},
            "SIGNATURE_TIMESTAMP_OUTSIDE_VALIDITY",
        ),
        ({"timestamp": ""}, "SIGNATURE_INVALID"),
        ({"publisher": "CN=Unapproved Publisher"}, "SIGNER_NOT_APPROVED"),
        ({"timestamp_trusted": False}, "SIGNATURE_INVALID"),
        ({"current_chain_trusted": False}, "SIGNATURE_INVALID"),
        ({"installer_signer_id": "release-2024"}, "INSTALLER_SIGNER_MISMATCH"),
        ({"previous_signer_id": "unapproved-legacy"}, "UPGRADE_SIGNER_ROTATION_NOT_APPROVED"),
    ),
)
def test_signature_policy_fails_closed(
    tmp_path: Path,
    fixture_changes: dict[str, object],
    expected_error: str,
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    fixture = _write_signature_policy_fixture(
        tmp_path / "signature-policy.json",
        **cast(dict[str, Any], fixture_changes),
    )
    _assert_failure(
        _invoke(
            action="Validate",
            bundle=bundle,
            install_root=tmp_path / "custom-root",
            test_signature_policy_fixture=fixture,
        ),
        expected_error,
    )


def test_signature_policy_allows_expired_signer_only_from_trusted_valid_timestamp(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    fixture = _write_signature_policy_fixture(tmp_path / "signature-policy.json")
    validated = _assert_success(
        _invoke(
            action="Validate",
            bundle=bundle,
            install_root=tmp_path / "custom-root",
            test_signature_policy_fixture=fixture,
        ),
        "validated",
    )
    assert validated["app_version"] == "1.0.0.0"
    source = INSTALLER.read_text(encoding="utf-8")
    assert "Assert-InstallerReleaseIdentity" in source
    assert "INSTALLER_SIGNER_MISMATCH" in source
    assert "$approvedReleaseSigners = @()" in source


def test_invalid_production_install_creates_no_root_or_installer_state(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    install_root = tmp_path / "production-must-not-exist"

    _assert_failure(
        _invoke(
            action="Install",
            bundle=bundle,
            install_root=install_root,
            development_unsigned=False,
        ),
        "SIGNATURE_INVALID",
    )

    assert not install_root.exists()
    assert not (install_root / LOCK_NAME).exists()
    assert not (install_root / JOURNAL_NAME).exists()
    assert not (install_root / JOURNAL_KEY_NAME).exists()


def test_recovery_requires_the_same_authenticated_installer_identity(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    install_root = tmp_path / "signer-bound-recovery"
    signer_a = _write_signature_policy_fixture(tmp_path / "signer-a.json")
    signer_b = _write_signature_policy_fixture(tmp_path / "signer-b.json", thumbprint="B2" * 20)

    crashed, _ = _development_install(
        bundle,
        install_root,
        test_fault="InstallAfterPublishBeforeJournal",
        test_signature_policy_fixture=signer_a,
        expect_json=False,
    )
    assert crashed.returncode == 91
    journal_path = install_root / JOURNAL_NAME
    journal_before = journal_path.read_bytes()
    destination = install_root / BUNDLE_NAME
    destination_before = _bundle_hashes(destination)
    journal = json.loads(journal_before)
    assert journal["SchemaVersion"] == "2.0"
    assert journal["InstallerSignerId"] == "release-2025"
    assert journal["InstallerSignerThumbprint"] == "A1" * 20

    _assert_failure(
        _development_install(
            bundle,
            install_root,
            test_signature_policy_fixture=signer_b,
        ),
        "RECOVERY_INSTALLER_SIGNER_MISMATCH",
    )
    assert journal_path.read_bytes() == journal_before
    assert _bundle_hashes(destination) == destination_before

    recovered = _assert_success(
        _development_install(
            bundle,
            install_root,
            test_signature_policy_fixture=signer_a,
        ),
        "installed",
    )
    assert recovered["recovery_status"] == "completed"
    assert not journal_path.exists()


def test_exclusive_lock_file_serializes_an_independent_process(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    install_root = tmp_path / "external-lock-root"
    install_root.mkdir()
    lock_path = install_root / LOCK_NAME
    assert POWERSHELL is not None
    holder_environment = os.environ.copy()
    holder_environment["CAD_HARNESS_TEST_LOCK_PATH"] = str(lock_path)
    holder = subprocess.Popen(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$path=[Environment]::GetEnvironmentVariable("
                "'CAD_HARNESS_TEST_LOCK_PATH');"
                "$stream=[IO.FileStream]::new($path,"
                "[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,"
                "[IO.FileShare]::None); try {[Console]::Out.WriteLine('ready');"
                "[Console]::Out.Flush(); Start-Sleep -Seconds 30} finally "
                "{$stream.Dispose()}"
            ),
        ],
        cwd=REPOSITORY_ROOT,
        env=holder_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        assert holder.poll() is None
        _assert_failure(
            _development_install(bundle, install_root),
            "INSTALL_ROOT_BUSY",
        )
        assert not (install_root / JOURNAL_NAME).exists()
        assert not (install_root / JOURNAL_KEY_NAME).exists()
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_install_and_uninstall_contend_on_the_same_root_lock(tmp_path: Path) -> None:
    first = _make_bundle(tmp_path / "source-v1", version="1.0.0.0")
    second = _make_bundle(tmp_path / "source-v2", version="2.0.0.0")
    install_root = tmp_path / "install-uninstall-contention"
    _assert_success(_development_install(first, install_root), "installed")

    with ThreadPoolExecutor(max_workers=1) as executor:
        upgrading = executor.submit(
            _development_install,
            second,
            install_root,
            upgrade=True,
            test_hold_mutex_milliseconds=3000,
        )
        time.sleep(0.75)
        _assert_failure(
            _development_uninstall(install_root),
            "INSTALL_ROOT_BUSY",
        )
        _assert_success(upgrading.result(timeout=15), "upgraded")


def test_lock_reparse_point_and_alternate_stream_fail_closed(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source")
    reparse_root = tmp_path / "lock-reparse-root"
    reparse_root.mkdir()
    target = tmp_path / "outside-lock-target"
    target.write_bytes(b"outside")
    lock_path = reparse_root / LOCK_NAME
    reparse_is_directory = False
    try:
        lock_path.symlink_to(target)
    except OSError:
        target.unlink()
        target.mkdir()
        junction = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(lock_path), str(target)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if junction.returncode != 0:
            pytest.fail("neither a file symlink nor directory junction could be created")
        reparse_is_directory = True
    try:
        _assert_failure(
            _development_install(bundle, reparse_root),
            "REPARSE_POINT_NOT_ALLOWED",
        )
        assert not (reparse_root / JOURNAL_NAME).exists()
        assert not (reparse_root / JOURNAL_KEY_NAME).exists()
    finally:
        if reparse_is_directory:
            lock_path.rmdir()
        else:
            lock_path.unlink()

    ads_root = tmp_path / "lock-ads-root"
    ads_root.mkdir()
    ads_lock = ads_root / LOCK_NAME
    ads_lock.write_bytes(b"")
    ads_path = Path(f"{ads_lock}:rogue")
    try:
        ads_path.write_bytes(b"not-owned")
    except OSError as exc:
        pytest.skip(f"NTFS alternate streams unavailable in this temp volume: {exc}")
    try:
        _assert_failure(
            _development_install(bundle, ads_root),
            "ALTERNATE_DATA_STREAM_NOT_ALLOWED",
        )
        assert not (ads_root / JOURNAL_NAME).exists()
        assert not (ads_root / JOURNAL_KEY_NAME).exists()
    finally:
        ads_path.unlink()


def test_recovery_rejects_lock_file_replacement_before_mutation(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source")
    install_root = tmp_path / "replaced-lock-recovery"
    crashed, _ = _development_install(
        bundle,
        install_root,
        test_fault="InstallAfterPublishBeforeJournal",
        expect_json=False,
    )
    assert crashed.returncode == 91
    lock_path = install_root / LOCK_NAME
    parked_lock = install_root / ".original-installer-lock"
    journal_path = install_root / JOURNAL_NAME
    journal_before = journal_path.read_bytes()
    destination = install_root / BUNDLE_NAME
    destination_before = _bundle_hashes(destination)
    lock_path.replace(parked_lock)

    _assert_failure(
        _development_install(bundle, install_root),
        "INSTALL_LOCK_INVALID",
    )
    assert not lock_path.exists()
    assert journal_path.read_bytes() == journal_before
    assert _bundle_hashes(destination) == destination_before

    lock_path.write_bytes(b"")

    _assert_failure(
        _development_install(bundle, install_root),
        "INSTALL_LOCK_IDENTITY_CHANGED",
    )
    assert journal_path.read_bytes() == journal_before
    assert _bundle_hashes(destination) == destination_before

    lock_path.unlink()
    parked_lock.replace(lock_path)
    recovered = _assert_success(
        _development_install(bundle, install_root),
        "installed",
    )
    assert recovered["recovery_status"] == "completed"
