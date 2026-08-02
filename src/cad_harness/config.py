"""Configuration loading. Infrastructure settings only - never engineering values."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_PATH = Path("./config/base.yaml")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSettings(_Section):
    environment: Literal["development", "pilot", "production"] = "development"
    #: Blocks any outbound network call from the harness.
    local_only: bool = True


class McpSettings(_Section):
    transport: Literal["stdio", "http"] = "stdio"
    server_name: str = "cad-harness"
    protocol_compatibility_baseline: str = "2025-11-25"
    enable_newer_protocol_by_feature_flag: bool = True


class AdapterSettings(_Section):
    type: Literal["fake", "dxf_preview", "com", "dotnet_bridge"] = "fake"
    autocad_prog_id: str = "autocad"
    #: Opt-in. A background server should not start AutoCAD behind the user's back.
    launch_autocad_if_missing: bool = False
    inspect_timeout_seconds: float = 15.0
    preview_timeout_seconds: float = 60.0
    commit_timeout_seconds: float = 120.0
    export_timeout_seconds: float = 120.0


class StorageSettings(_Section):
    sqlite_path: Path = Path("./data/harness.db")
    preview_directory: Path = Path("./data/previews")
    checkpoint_directory: Path = Path("./data/checkpoints")
    export_directory: Path = Path("./data/exports")
    preview_retention_days: int = 14


class SecuritySettings(_Section):
    require_commit_approval: bool = True
    require_rollback_approval: bool = True
    require_export_approval: bool = True
    allow_arbitrary_export_path: bool = False
    redact_document_paths: bool = True
    approval_ttl_minutes: int = 15
    #: Directories exports, previews and checkpoints may be written to.
    export_path_allowlist: tuple[Path, ...] = (Path("./data/exports"),)


class GeometrySettings(_Section):
    canonical_unit: Literal["mm"] = "mm"
    tolerance_profile: str = "demo-mechanical-mm@1.0"


class StandardsSettings(_Section):
    company_profile: str = "demo-profile@1.0"


class ObservabilitySettings(_Section):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    #: Off by default. Prompts can contain customer data.
    log_prompts: bool = False


class Settings(_Section):
    app: AppSettings = Field(default_factory=AppSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    adapter: AdapterSettings = Field(default_factory=AdapterSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    geometry: GeometrySettings = Field(default_factory=GeometrySettings)
    standards: StandardsSettings = Field(default_factory=StandardsSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    def approval_secret(self) -> str:
        """Read the signing secret from the environment, never from a config file."""
        return os.environ.get("CAD_HARNESS_APPROVAL_SECRET", "")


#: Environment variables that override file settings, for container deployments.
_ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "CAD_HARNESS_ENVIRONMENT": ("app", "environment"),
    "CAD_HARNESS_LOCAL_ONLY": ("app", "local_only"),
    "CAD_HARNESS_ADAPTER": ("adapter", "type"),
    "CAD_HARNESS_SQLITE_PATH": ("storage", "sqlite_path"),
    "CAD_HARNESS_PREVIEW_DIR": ("storage", "preview_directory"),
    "CAD_HARNESS_CHECKPOINT_DIR": ("storage", "checkpoint_directory"),
    "CAD_HARNESS_LOG_LEVEL": ("observability", "log_level"),
}


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    return value


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from YAML, then apply environment overrides."""
    config_path = path or Path(os.environ.get("CAD_HARNESS_CONFIG", DEFAULT_CONFIG_PATH))
    data: dict[str, Any] = {}
    if config_path.is_file():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    for env_name, (section, key) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is not None:
            data.setdefault(section, {})[key] = _coerce(raw)

    return Settings.model_validate(data)
