"""Shared fixtures. Nothing here requires AutoCAD."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.company_rules.loader import CompanyProfile, load_profile
from cad_harness.config import Settings
from cad_harness.geometry.tolerance import ToleranceProfile

APPROVAL_SECRET = "test-secret"


@pytest.fixture
def profile() -> CompanyProfile:
    return load_profile("demo-profile")


@pytest.fixture
def tolerance(profile: CompanyProfile) -> ToleranceProfile:
    return profile.tolerance()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings pointed at a temporary directory so tests never write into the repo."""
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", APPROVAL_SECRET)
    return Settings.model_validate(
        {
            "storage": {
                "sqlite_path": str(tmp_path / "harness.db"),
                "preview_directory": str(tmp_path / "previews"),
                "checkpoint_directory": str(tmp_path / "checkpoints"),
                "export_directory": str(tmp_path / "exports"),
            },
            "security": {"export_path_allowlist": [str(tmp_path / "exports")]},
            "observability": {"log_level": "WARNING"},
        }
    )


@pytest.fixture
def adapter() -> FakeAutoCADAdapter:
    return FakeAutoCADAdapter()


@pytest.fixture
def service(settings: Settings, adapter: FakeAutoCADAdapter) -> HarnessService:
    return HarnessService(settings, adapter)


@pytest.fixture
def base_plate_spec() -> dict[str, Any]:
    """The reference case from architecture section 32: 160x100x12 plate, 4x Ø14 holes."""
    return {
        "units": "mm",
        "drawing": {
            "projection": "orthographic",
            "view": "top",
            "datum": {"type": "point", "point_mm": [0.0, 0.0]},
        },
        "features": [
            {
                "feature_id": "base-plate-001",
                "type": "rectangular_plate",
                "parameters": {
                    "width_mm": 160.0,
                    "height_mm": 100.0,
                    "thickness_mm": 12.0,
                    "material": "SS400",
                    "origin_mm": [0.0, 0.0],
                },
                "children": [
                    {
                        "feature_id": "base-plate-001-holes",
                        "type": "rectangular_hole_pattern",
                        "parameters": {
                            "hole_diameter_mm": 14.0,
                            "edge_offset_x_mm": 20.0,
                            "edge_offset_y_mm": 20.0,
                            "count_x": 2,
                            "count_y": 2,
                        },
                    }
                ],
            }
        ],
        "annotations": {"general_tolerance": "ISO 2768-m", "dimensions": "auto_required"},
    }
