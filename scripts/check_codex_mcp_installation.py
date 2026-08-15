"""Read-only, redacted doctor for the local Codex AutoCAD MCP installation."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import stat
import tomllib
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from cad_harness.config import Settings

_SERVER_NAME = "autocad-mechanical-harness"
_CONFIG_RELATIVE_PATH = Path("config/live-r26-acceptance.yaml")
_MANUAL_CONFIRMATIONS = (
    "open_target_drawing",
    "load_company_standards",
    "install_bridge_bundle",
    "grant_named_pipe_acl",
    "confirm_autocad_version",
)
_PACKAGE_NAME = "AutoCAD Mechanical Harness Bridge"
_UPGRADE_CODE = "FA1366B0-8CAB-42B6-B5A2-66D3EF37F0A5"
_APP_NAME = "AutoCADHarnessBridge"
_MODULE_NAME = "./Contents/Windows/AutoCADHarness.dll"
_PACKAGE_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
_AUTOCAD_SERIES_PATTERN = re.compile(r"^R[0-9]+\.[0-9]+$")
_SAFETY_OVERRIDES = {
    "CAD_HARNESS_LOCAL_ONLY": ("app", "local_only"),
    "CAD_HARNESS_ADAPTER": ("adapter", "type"),
}


@dataclass(frozen=True, slots=True)
class BundleInventory:
    candidate_count: int
    active_count: int
    reparse_rejected_count: int
    malformed_count: int
    unrelated_ignored_count: int
    module_missing_count: int
    development_unsigned_count: int
    unsigned_marker_absent_count: int
    authenticode_verification_performed: bool
    versions: tuple[str, ...]
    series: tuple[str, ...]


def _default_repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_application_plugins_root() -> Path:
    roaming = os.environ.get("APPDATA")
    root = Path(roaming) if roaming else Path.home() / "AppData" / "Roaming"
    return root / "Autodesk" / "ApplicationPlugins"


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _path_matches(value: object, expected: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return _normalized_path(Path(value)) == _normalized_path(expected)
    except (OSError, ValueError):
        return False


def _command_basename(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\\", "/").rsplit("/", maxsplit=1)[-1].casefold()


def _is_reparse_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _direct_bundle_candidates(root: Path) -> tuple[Path, ...]:
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return ()
    return tuple(
        sorted(
            (
                entry
                for entry in entries
                if entry.name.casefold().endswith(".bundle")
                and (entry.is_dir() or _is_reparse_directory(entry))
            ),
            key=lambda item: item.name.casefold(),
        )
    )


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", maxsplit=1)[-1]


def _valid_product_code(value: str) -> bool:
    try:
        uuid.UUID(value.strip("{}"))
    except (AttributeError, ValueError):
        return False
    return value.startswith("{") and value.endswith("}")


def _manifest_identity(bundle: Path) -> tuple[str, str, tuple[str, ...]]:
    manifest = bundle / "PackageContents.xml"
    try:
        root = ET.parse(manifest).getroot()
    except (ET.ParseError, OSError):
        return "malformed", "", ()
    entries = tuple(element for element in root.iter() if _local_name(element) == "ComponentEntry")
    runtime_requirements = tuple(
        element for element in root.iter() if _local_name(element) == "RuntimeRequirements"
    )
    expected_hallmarks = (
        root.attrib.get("Name") == _PACKAGE_NAME,
        root.attrib.get("UpgradeCode", "").strip("{}").upper() == _UPGRADE_CODE,
        any(entry.attrib.get("AppName") == _APP_NAME for entry in entries),
        any(entry.attrib.get("ModuleName") == _MODULE_NAME for entry in entries),
    )
    if not any(expected_hallmarks):
        return "unrelated", "", ()
    if (
        _local_name(root) != "ApplicationPackage"
        or not all(expected_hallmarks)
        or not _valid_product_code(root.attrib.get("ProductCode", ""))
        or len(entries) != 1
        or len(runtime_requirements) != 1
    ):
        return "malformed", "", ()
    version = root.attrib.get("AppVersion", "").strip()
    series_values: set[str] = set()
    for element in runtime_requirements:
        for attribute in ("SeriesMin", "SeriesMax"):
            value = element.attrib.get(attribute, "").strip()
            if value:
                series_values.add(value)
    if (
        _PACKAGE_VERSION_PATTERN.fullmatch(version) is None
        or not series_values
        or any(_AUTOCAD_SERIES_PATTERN.fullmatch(value) is None for value in series_values)
    ):
        return "malformed", "", ()
    if not (bundle / "Contents" / "Windows" / "AutoCADHarness.dll").is_file():
        return "module_missing", version, tuple(sorted(series_values))
    return "harness", version, tuple(sorted(series_values))


def _inventory(root: Path) -> BundleInventory:
    candidates = _direct_bundle_candidates(root)
    active = 0
    reparse = 0
    malformed = 0
    unrelated = 0
    module_missing = 0
    unsigned = 0
    versions: set[str] = set()
    series: set[str] = set()
    for bundle in candidates:
        if _is_reparse_directory(bundle):
            reparse += 1
            continue
        kind, version, bundle_series = _manifest_identity(bundle)
        if kind == "unrelated":
            unrelated += 1
            continue
        if kind == "malformed":
            malformed += 1
            continue
        if kind == "module_missing":
            module_missing += 1
            continue
        active += 1
        versions.add(version)
        series.update(bundle_series)
        if (bundle / "DEVELOPMENT-UNSIGNED.txt").is_file():
            unsigned += 1
    return BundleInventory(
        candidate_count=len(candidates),
        active_count=active,
        reparse_rejected_count=reparse,
        malformed_count=malformed,
        unrelated_ignored_count=unrelated,
        module_missing_count=module_missing,
        development_unsigned_count=unsigned,
        unsigned_marker_absent_count=max(0, active - unsigned),
        authenticode_verification_performed=False,
        versions=tuple(sorted(versions)),
        series=tuple(sorted(series)),
    )


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _inspect_registration(
    codex_config: Path, repository_root: Path, expected_config: Path, codes: set[str]
) -> tuple[dict[str, bool], dict[str, object]]:
    data: dict[str, Any] = {}
    if not codex_config.is_file():
        codes.add("CODEX_CONFIG_MISSING")
    else:
        try:
            data = tomllib.loads(codex_config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            codes.add("CODEX_CONFIG_MALFORMED")

    servers = _mapping(data.get("mcp_servers"))
    server = _mapping(servers.get(_SERVER_NAME))
    found = bool(server)
    if not found:
        codes.add("MCP_REGISTRATION_MISSING")

    enabled_raw = server.get("enabled", True)
    enabled = enabled_raw is True
    if found and not enabled:
        codes.add("MCP_REGISTRATION_DISABLED")

    command_exact = _command_basename(server.get("command")) == "uv.exe"
    if not command_exact:
        codes.add("MCP_COMMAND_MISMATCH")

    args = server.get("args")
    args_exact = (
        isinstance(args, list)
        and len(args) == 4
        and args[0] == "--directory"
        and _path_matches(args[1], repository_root)
        and args[2:] == ["run", "cad-harness-mcp"]
    )
    if not args_exact:
        codes.add("MCP_ARGS_MISMATCH")

    environment = _mapping(server.get("env"))
    config_exact = _path_matches(environment.get("CAD_HARNESS_CONFIG"), expected_config)
    if not config_exact:
        codes.add("MCP_CONFIG_ENV_MISMATCH")

    raw_confirmations = environment.get("CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS")
    confirmations_present = isinstance(raw_confirmations, str) and bool(raw_confirmations.strip())
    confirmations_exact = (
        isinstance(raw_confirmations, str)
        and tuple(item.strip() for item in raw_confirmations.split(",")) == _MANUAL_CONFIRMATIONS
    )
    if confirmations_present:
        codes.add("MANUAL_CONFIRMATIONS_STATIC_UNBOUND")
        if not confirmations_exact:
            codes.add("MANUAL_CONFIRMATIONS_MISMATCH")

    live_write_requested = environment.get("CAD_HARNESS_LIVE_WRITE_VERIFIED") == "1"
    raw_session_proof = environment.get("CAD_HARNESS_LIVE_SESSION_PROOF")
    live_session_proof_registered = isinstance(raw_session_proof, str) and bool(
        raw_session_proof.strip()
    )
    if live_write_requested:
        codes.add("REGISTERED_MCP_WRITE_REQUIRES_EPHEMERAL_LAUNCHER")
    if live_session_proof_registered:
        codes.add("LIVE_SESSION_PROOF_STORED_IN_CLIENT_CONFIG")

    registered_secret = environment.get("CAD_HARNESS_APPROVAL_SECRET")
    inherited_secret = os.environ.get("CAD_HARNESS_APPROVAL_SECRET")
    approval_secret_registered = isinstance(registered_secret, str) and bool(
        registered_secret.strip()
    )
    approval_secret_inherited = bool(inherited_secret and inherited_secret.strip())
    if approval_secret_registered:
        codes.add("APPROVAL_SECRET_STORED_IN_CLIENT_CONFIG")
    if live_write_requested and not approval_secret_inherited:
        codes.add("APPROVAL_SECRET_MISSING")

    return (
        {
            "found": found,
            "enabled": enabled,
            "command_exact": command_exact,
            "args_exact": args_exact,
            "config_env_exact": config_exact,
            "static_manual_confirmations_present": confirmations_present,
            "manual_confirmations_exact": confirmations_exact,
            "live_write_requested": live_write_requested,
            "live_session_proof_registered": live_session_proof_registered,
            "approval_secret_registered": approval_secret_registered,
            "approval_secret_inherited": approval_secret_inherited,
        },
        environment,
    )


def _coerce_override(value: str) -> object:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    return value


def _inspect_runtime_config(
    config_path: Path, registered_environment: dict[str, object], codes: set[str]
) -> dict[str, bool]:
    source_settings: Settings | None = None
    effective_settings: Settings | None = None
    safety_override_present = False
    safety_override_changed = False
    if not config_path.is_file():
        codes.add("RUNTIME_CONFIG_MISSING")
    else:
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError
            source_settings = Settings.model_validate(raw)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            codes.add("RUNTIME_CONFIG_MALFORMED")

    if source_settings is not None:
        effective_data = copy.deepcopy(raw)
        for environment_name, (section, key) in _SAFETY_OVERRIDES.items():
            registered_value = registered_environment.get(environment_name)
            effective_value: object = (
                registered_value
                if registered_value is not None
                else os.environ.get(environment_name)
            )
            if effective_value is None:
                continue
            safety_override_present = True
            if not isinstance(effective_value, str):
                codes.add("RUNTIME_CONFIG_EFFECTIVE_INVALID")
                effective_settings = None
                break
            effective_data.setdefault(section, {})[key] = _coerce_override(effective_value)
        else:
            try:
                effective_settings = Settings.model_validate(effective_data)
            except ValueError:
                codes.add("RUNTIME_CONFIG_EFFECTIVE_INVALID")

    if source_settings is not None and effective_settings is not None:
        safety_override_changed = (
            source_settings.app.local_only != effective_settings.app.local_only
            or source_settings.adapter.type != effective_settings.adapter.type
        )
        if safety_override_changed:
            codes.add("RUNTIME_SAFETY_OVERRIDE_CHANGED")

    local_only = effective_settings is not None and effective_settings.app.local_only is True
    dotnet_bridge = (
        effective_settings is not None and effective_settings.adapter.type == "dotnet_bridge"
    )
    launch_disabled = (
        effective_settings is not None
        and effective_settings.adapter.launch_autocad_if_missing is False
    )
    if effective_settings is not None:
        if not local_only:
            codes.add("LOCAL_ONLY_REQUIRED")
        if not dotnet_bridge:
            codes.add("DOTNET_BRIDGE_REQUIRED")
        if not launch_disabled:
            codes.add("AUTOCAD_LAUNCH_MUST_BE_DISABLED")
    return {
        "loaded": source_settings is not None,
        "effective_loaded": effective_settings is not None,
        "local_only": local_only,
        "dotnet_bridge": dotnet_bridge,
        "launch_autocad_if_missing_disabled": launch_disabled,
        "safety_override_present": safety_override_present,
        "safety_override_changed": safety_override_changed,
    }


def _add_inventory_codes(label: str, inventory: BundleInventory, codes: set[str]) -> None:
    prefix = label.upper()
    if inventory.active_count == 0:
        codes.add(f"{prefix}_BUNDLE_NONE")
    elif inventory.active_count > 1:
        codes.add(f"{prefix}_BUNDLE_MULTIPLE")
    if inventory.reparse_rejected_count:
        codes.add(f"{prefix}_BUNDLE_REPARSE_REJECTED")
    if inventory.malformed_count:
        codes.add(f"{prefix}_BUNDLE_MANIFEST_MALFORMED")
    if inventory.module_missing_count:
        codes.add(f"{prefix}_BUNDLE_MODULE_MISSING")
    if inventory.development_unsigned_count:
        codes.add(f"{prefix}_BUNDLE_DEVELOPMENT_UNSIGNED")
    if inventory.unsigned_marker_absent_count:
        # Absence of the explicit development marker is not Authenticode evidence.
        # The hardened installer remains the authority for signature/timestamp checks.
        codes.add(f"{prefix}_BUNDLE_AUTHENTICODE_UNVERIFIED")


def inspect_installation(
    *,
    codex_config: Path,
    repository_root: Path,
    application_plugins_root: Path,
    workspace_bundle_root: Path,
) -> dict[str, object]:
    """Return only redacted, deterministic installation evidence."""
    repository_root = repository_root.resolve(strict=False)
    expected_config = repository_root / _CONFIG_RELATIVE_PATH
    codes: set[str] = set()
    registration, registered_environment = _inspect_registration(
        codex_config, repository_root, expected_config, codes
    )
    runtime = _inspect_runtime_config(expected_config, registered_environment, codes)
    global_inventory = _inventory(application_plugins_root)
    workspace_inventory = _inventory(workspace_bundle_root)
    _add_inventory_codes("global", global_inventory, codes)
    _add_inventory_codes("workspace", workspace_inventory, codes)

    version_drift = (
        global_inventory.active_count > 0
        and workspace_inventory.active_count > 0
        and global_inventory.versions != workspace_inventory.versions
    )
    series_drift = (
        global_inventory.active_count > 0
        and workspace_inventory.active_count > 0
        and global_inventory.series != workspace_inventory.series
    )
    if version_drift:
        codes.add("BUNDLE_VERSION_DRIFT")
    if series_drift:
        codes.add("BUNDLE_SERIES_DRIFT")

    return {
        "schema_version": "1.0",
        "ok": not codes,
        "codes": sorted(codes),
        "registration": registration,
        "runtime_config": runtime,
        "bundles": {
            "global": asdict(global_inventory),
            "workspace": asdict(workspace_inventory),
            "version_drift": version_drift,
            "series_drift": series_drift,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex" / "config.toml")
    parser.add_argument("--repository-root", type=Path, default=_default_repository_root())
    parser.add_argument("--application-plugins-root", type=Path)
    parser.add_argument("--workspace-bundle-root", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    repository_root = args.repository_root.resolve(strict=False)
    try:
        report = inspect_installation(
            codex_config=args.codex_config,
            repository_root=repository_root,
            application_plugins_root=(
                args.application_plugins_root or _default_application_plugins_root()
            ),
            workspace_bundle_root=(
                args.workspace_bundle_root
                or repository_root / "data" / "live-r26" / "ApplicationPlugins"
            ),
        )
    except Exception:
        report = {
            "schema_version": "1.0",
            "ok": False,
            "codes": ["DOCTOR_INTERNAL_ERROR"],
            "registration": {},
            "runtime_config": {},
            "bundles": {},
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["ok"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
