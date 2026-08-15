"""Bounds on the new configuration keys (Requirements 13.6, 13.13).

A configuration value outside its valid range must be refused while `Settings` is
being built, not when the reader, the lease renewer or the bridge first uses it. A
range that is only documented in a comment is not a range.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cad_harness.config import DEFAULT_CONFIG_PATH, Settings, load_settings


def settings_with(section: str, **values: object) -> Settings:
    return Settings.model_validate({section: values})


class TestReadBounds:
    """`read.max_entities` 1..200000, `read.max_block_nesting_depth` 1..10."""

    @pytest.mark.parametrize("value", [1, 20_000, 200_000])
    def test_entity_budget_inside_the_range_is_accepted(self, value: int) -> None:
        assert settings_with("read", max_entities=value).read.max_entities == value

    @pytest.mark.parametrize("value", [0, -1, 200_001])
    def test_entity_budget_outside_the_range_is_rejected(self, value: int) -> None:
        with pytest.raises(ValidationError):
            settings_with("read", max_entities=value)

    @pytest.mark.parametrize("value", [1, 3, 10])
    def test_nesting_depth_inside_the_range_is_accepted(self, value: int) -> None:
        assert settings_with(
            "read", max_block_nesting_depth=value
        ).read.max_block_nesting_depth == (value)

    @pytest.mark.parametrize("value", [0, -1, 11])
    def test_nesting_depth_outside_the_range_is_rejected(self, value: int) -> None:
        with pytest.raises(ValidationError):
            settings_with("read", max_block_nesting_depth=value)

    @pytest.mark.parametrize("value", [0.0, -1.0, 601.0])
    def test_non_positive_or_excessive_timeout_is_rejected(self, value: float) -> None:
        with pytest.raises(ValidationError):
            settings_with("read", read_timeout_seconds=value)

    def test_the_error_names_the_offending_key(self) -> None:
        # A caller fixing a config file needs the key, not just "validation failed".
        with pytest.raises(ValidationError) as excinfo:
            settings_with("read", max_entities=0)
        assert excinfo.value.errors()[0]["loc"] == ("read", "max_entities")

    @pytest.mark.parametrize("value", ["auto", "dotnet_bridge"])
    def test_semantic_reader_is_closed_to_supported_local_adapters(self, value: str) -> None:
        assert settings_with("read", semantic_adapter=value).read.semantic_adapter == value

    def test_unknown_semantic_reader_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            settings_with("read", semantic_adapter="network_reader")

    def test_semantic_reader_environment_override_is_explicit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CAD_HARNESS_READ_SEMANTIC_ADAPTER", "dotnet_bridge")
        assert load_settings(Path(DEFAULT_CONFIG_PATH)).read.semantic_adapter == "dotnet_bridge"


class TestTimeoutBounds:
    @pytest.mark.parametrize(
        ("section", "key", "value"),
        [
            ("takeoff", "timeout_seconds", 0.0),
            ("takeoff", "timeout_seconds", 601.0),
            ("measure", "timeout_seconds", 0.0),
            ("measure", "timeout_seconds", 61.0),
            ("bridge", "ipc_timeout_seconds", 0.0),
            ("bridge", "ipc_timeout_seconds", 601.0),
        ],
    )
    def test_timeout_outside_the_range_is_rejected(
        self, section: str, key: str, value: float
    ) -> None:
        with pytest.raises(ValidationError):
            settings_with(section, **{key: value})


class TestLeaseBudget:
    """Requirement 2.4 requires ttl - heartbeat interval >= remaining floor."""

    def test_the_shipped_defaults_are_satisfiable(self) -> None:
        lease = Settings().lease
        assert (
            lease.ttl_seconds - lease.heartbeat_interval_seconds >= lease.minimum_remaining_seconds
        )

    @pytest.mark.parametrize("value", [4, 3_601])
    def test_ttl_outside_the_range_is_rejected(self, value: int) -> None:
        with pytest.raises(ValidationError):
            settings_with("lease", ttl_seconds=value)

    def test_exact_floor_immediately_before_heartbeat_is_accepted(self) -> None:
        lease = settings_with(
            "lease",
            ttl_seconds=30,
            heartbeat_interval_seconds=15,
            minimum_remaining_seconds=15,
        ).lease
        assert (
            lease.ttl_seconds - lease.heartbeat_interval_seconds == lease.minimum_remaining_seconds
        )

    def test_floor_at_or_above_the_ttl_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            settings_with(
                "lease",
                ttl_seconds=20,
                heartbeat_interval_seconds=5,
                minimum_remaining_seconds=20,
            )

    def test_floor_that_would_be_crossed_before_heartbeat_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            settings_with(
                "lease",
                ttl_seconds=20,
                heartbeat_interval_seconds=5,
                minimum_remaining_seconds=16,
            )


class TestBridgeBounds:
    def test_pipe_name_must_stay_per_user(self) -> None:
        # Dropping the placeholder would widen the pipe ACL beyond one account.
        with pytest.raises(ValidationError):
            settings_with("bridge", pipe_name_template="cadharness.shared")

    @pytest.mark.parametrize("value", [1_023, 16_777_217])
    def test_request_size_outside_the_range_is_rejected(self, value: int) -> None:
        with pytest.raises(ValidationError):
            settings_with("bridge", max_request_bytes=value)

    @pytest.mark.parametrize("value", [0, 129, 257])
    def test_request_depth_outside_the_range_is_rejected(self, value: int) -> None:
        with pytest.raises(ValidationError):
            settings_with("bridge", max_request_depth=value)


class TestRetentionBounds:
    @pytest.mark.parametrize(
        "key",
        ["preview_retention_days", "checkpoint_retention_days"],
    )
    @pytest.mark.parametrize("value", [0, 3_651])
    def test_retention_days_outside_the_range_are_rejected(self, key: str, value: int) -> None:
        with pytest.raises(ValidationError):
            settings_with("storage", **{key: value})

    @pytest.mark.parametrize(
        "key",
        ["preview_max_total_bytes", "checkpoint_max_total_bytes"],
    )
    def test_negative_storage_quota_is_rejected(self, key: str) -> None:
        with pytest.raises(ValidationError):
            settings_with("storage", **{key: -1})


class TestSectionSurface:
    def test_every_new_section_is_present_with_defaults(self) -> None:
        settings = Settings()
        for section in ("read", "takeoff", "measure", "lease", "bridge", "compatibility", "pilot"):
            assert getattr(settings, section) is not None, section
        assert settings.mcp.client_profiles.default == "read_only"

    def test_unknown_keys_are_rejected_rather_than_ignored(self) -> None:
        # A typo in a limit must fail loudly instead of silently keeping the default.
        with pytest.raises(ValidationError):
            settings_with("read", max_entity=100)

    def test_sections_are_frozen(self) -> None:
        settings = Settings()
        with pytest.raises(ValidationError):
            settings.read.max_entities = 1  # type: ignore[misc]

    def test_unknown_client_permission_mode_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings.model_validate({"mcp": {"client_profiles": {"default": "superuser"}}})


class TestShippedConfigFile:
    def test_base_yaml_loads_and_stays_inside_every_range(self) -> None:
        # The file ships the documented ranges; a drifted value must not reach a pilot.
        settings = load_settings(Path(DEFAULT_CONFIG_PATH))
        assert settings.read.max_entities == 20_000
        assert settings.read.max_block_nesting_depth == 3
        assert settings.compatibility.matrix_path == Path("./config/compatibility.yaml")
        assert settings.pilot.thresholds_path == Path("pilot.yaml")
