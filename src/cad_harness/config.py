"""Configuration loading. Infrastructure settings only - never engineering values."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path("./config/base.yaml")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSettings(_Section):
    environment: Literal["development", "pilot", "production"] = "development"
    #: Blocks any outbound network call from the harness.
    local_only: bool = True


#: Permission modes a client may be granted. Ordered least to most privileged.
PermissionMode = Literal["read_only", "approval_required", "full"]


class ClientProfileSettings(_Section):
    """One client's permission profile, declared in `config/clients.yaml`."""

    mode: PermissionMode = "read_only"
    #: Empty means "use the tool set implied by `mode`", not "no tools".
    allowed_tools: tuple[str, ...] = ()


class ClientProfilesSettings(_Section):
    #: Applied to any client without an entry in `clients`. Fail closed.
    default: Literal["read_only"] = "read_only"
    clients: dict[str, ClientProfileSettings] = Field(default_factory=dict)


class McpSettings(_Section):
    transport: Literal["stdio", "http"] = "stdio"
    server_name: str = "cad-harness"
    protocol_compatibility_baseline: str = "2025-11-25"
    enable_newer_protocol_by_feature_flag: bool = True
    client_profiles: ClientProfilesSettings = Field(default_factory=ClientProfilesSettings)


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
    preview_retention_days: int = Field(default=14, ge=1, le=3_650)
    preview_max_total_bytes: int = Field(default=1_073_741_824, ge=0, le=10_995_116_277_760)
    checkpoint_retention_days: int = Field(default=30, ge=1, le=3_650)
    checkpoint_max_total_bytes: int = Field(default=10_737_418_240, ge=0, le=10_995_116_277_760)


class SecuritySettings(_Section):
    require_commit_approval: bool = True
    require_export_approval: bool = True
    allow_arbitrary_export_path: bool = False
    redact_document_paths: bool = True
    approval_ttl_minutes: int = 15
    rollback_approval_ttl_minutes: int = Field(default=15, ge=1, le=15)
    #: Directories exports, previews and checkpoints may be written to.
    export_path_allowlist: tuple[Path, ...] = (Path("./data/exports"),)


class GeometrySettings(_Section):
    canonical_unit: Literal["mm"] = "mm"
    tolerance_profile: str = "demo-mechanical-mm@1.0"


class StandardsSettings(_Section):
    company_profile: str = "demo-profile@1.0"


class ReadSettings(_Section):
    """Limits on the read direction. Operational bounds, not engineering values."""

    #: A scope wider than this is rejected outright; partial geometry is never returned.
    max_entities: int = Field(default=20_000, ge=1, le=200_000)
    #: How deep a block reference is followed. Children past this depth are counted only.
    max_block_nesting_depth: int = Field(default=3, ge=1, le=10)
    read_timeout_seconds: float = Field(default=20.0, gt=0.0, le=600.0)


class TakeoffSettings(_Section):
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=600.0)
    #: Material table reference. Not company approved until a real table replaces it.
    material_profile: str = "demo-materials@1.0"


class MeasureSettings(_Section):
    timeout_seconds: float = Field(default=1.0, gt=0.0, le=60.0)


class RasterSettings(_Section):
    """Resource and review bounds for the local-only raster intake."""

    max_bytes: int = Field(default=16_777_216, ge=1_024, le=67_108_864)
    max_pixels: int = Field(default=20_000_000, ge=1, le=100_000_000)
    max_dimension_px: int = Field(default=20_000, ge=1, le=100_000)
    confidence_threshold: float = Field(default=0.72, ge=0.0, le=1.0)
    acceptance_ttl_minutes: int = Field(default=15, ge=1, le=15)


class LeaseSettings(_Section):
    ttl_seconds: int = Field(default=30, ge=5, le=3_600)
    heartbeat_interval_seconds: int = Field(default=5, ge=1, le=600)
    #: The renewer keeps at least this much of the TTL in hand while a commit runs.
    minimum_remaining_seconds: int = Field(default=15, ge=1, le=3_600)

    @model_validator(mode="after")
    def _renewal_budget_is_satisfiable(self) -> LeaseSettings:
        # Immediately before an on-time heartbeat, the remaining TTL is
        # ttl - interval. That value, not merely the ordering of the three settings,
        # must satisfy Requirement 2.4's floor.
        remaining_before_heartbeat = self.ttl_seconds - self.heartbeat_interval_seconds
        if remaining_before_heartbeat < self.minimum_remaining_seconds:
            raise ValueError(
                "lease.ttl_seconds - lease.heartbeat_interval_seconds must be at least "
                "lease.minimum_remaining_seconds"
            )
        return self


class BridgeSettings(_Section):
    #: Per-user pipe name. The placeholder is what keeps the ACL scoped to one account.
    pipe_name_template: str = "cadharness.{user_sid}"
    max_request_bytes: int = Field(default=1_048_576, ge=1_024, le=16_777_216)
    max_request_depth: int = Field(default=32, ge=1, le=128)
    ipc_timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)

    @field_validator("pipe_name_template")
    @classmethod
    def _template_is_per_user(cls, value: str) -> str:
        if "{user_sid}" not in value:
            raise ValueError("bridge.pipe_name_template must contain '{user_sid}'")
        return value


class CompatibilitySettings(_Section):
    #: Published AutoCAD / .NET / bundle matrix. Absence blocks write adapter selection.
    matrix_path: Path = Path("./config/compatibility.yaml")


class PilotSettings(_Section):
    #: Acceptance thresholds live in data, never hard-coded in Metrics_Collector.
    thresholds_path: Path = Path("pilot.yaml")
    run_id: str = Field(default="development", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


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
    read: ReadSettings = Field(default_factory=ReadSettings)
    takeoff: TakeoffSettings = Field(default_factory=TakeoffSettings)
    measure: MeasureSettings = Field(default_factory=MeasureSettings)
    raster: RasterSettings = Field(default_factory=RasterSettings)
    lease: LeaseSettings = Field(default_factory=LeaseSettings)
    bridge: BridgeSettings = Field(default_factory=BridgeSettings)
    compatibility: CompatibilitySettings = Field(default_factory=CompatibilitySettings)
    pilot: PilotSettings = Field(default_factory=PilotSettings)

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
    "CAD_HARNESS_BRIDGE_PIPE_NAME_TEMPLATE": ("bridge", "pipe_name_template"),
    "CAD_HARNESS_BRIDGE_MAX_REQUEST_BYTES": ("bridge", "max_request_bytes"),
    "CAD_HARNESS_BRIDGE_MAX_REQUEST_DEPTH": ("bridge", "max_request_depth"),
    "CAD_HARNESS_BRIDGE_IPC_TIMEOUT_SECONDS": ("bridge", "ipc_timeout_seconds"),
    "CAD_HARNESS_LOG_LEVEL": ("observability", "log_level"),
    "CAD_HARNESS_PILOT_RUN_ID": ("pilot", "run_id"),
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


def resolve_config_relative_path(value: Path, config_path: Path | None = None) -> Path:
    """Resolve a referenced config artifact relative to the loaded YAML file."""
    if value.is_absolute():
        return value
    source = config_path or Path(os.environ.get("CAD_HARNESS_CONFIG", DEFAULT_CONFIG_PATH))
    candidate = (source.resolve().parent / value).resolve()
    if candidate.is_file():
        return candidate
    if value in {Path("pilot.yaml"), Path("config/compatibility.yaml")}:
        return (DEFAULT_CONFIG_PATH.resolve().parent / value.name).resolve()
    return candidate
