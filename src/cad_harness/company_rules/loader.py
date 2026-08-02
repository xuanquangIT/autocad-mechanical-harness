"""Load and validate company standard profiles.

A profile is the only legitimate source of a default. Anything it does not declare
must be asked of the engineer rather than guessed (architecture section 12).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from cad_harness.domain.errors import StandardProfileNotFoundError
from cad_harness.domain.models.base import ContractModel
from cad_harness.domain.models.drawing_spec import DefaultRecord
from cad_harness.geometry.tolerance import ToleranceProfile

PROFILES_DIR = Path(__file__).parent / "profiles"


class LayerRule(ContractModel):
    name: str
    purpose: str
    color_index: int | None = None
    linetype: str = "Continuous"
    lineweight: int | None = None


class AllowedDefault(ContractModel):
    """An explicitly permitted default, with the provenance the audit trail needs."""

    path: str
    value: Any
    reason: str
    impact: str
    override_allowed: bool = True


class CompanyProfile(ContractModel):
    profile_id: str
    version: str
    #: False for demo profiles. Only an engineer-signed profile may claim approval.
    company_approved: bool = False
    canonical_unit: str = "mm"
    general_tolerance: str | None = None
    dimension_style: str | None = None
    text_style: str | None = None
    title_block: str | None = None
    annotation_scale: str | None = None
    layers: tuple[LayerRule, ...] = ()
    #: Feature purpose -> layer name, e.g. ``{"outline": "OBJECT"}``.
    layer_map: dict[str, str] = Field(default_factory=dict)
    minimum_hole_edge_distance_mm: float | None = None
    minimum_hole_ligament_mm: float | None = None
    tolerance_profile_ref: str = "demo-mechanical-mm@1.0"
    allowed_defaults: tuple[AllowedDefault, ...] = ()

    def as_ref(self) -> str:
        return f"{self.profile_id}@{self.version}"

    def layer_for(self, purpose: str) -> str:
        """Resolve a layer by purpose. Unmapped purposes fall back to ``0``.

        A validation rule reports the fallback, so it never silently ships.
        """
        return self.layer_map.get(purpose, "0")

    def layer_names(self) -> frozenset[str]:
        return frozenset(layer.name for layer in self.layers)

    def default_for(self, path: str) -> DefaultRecord | None:
        """Return a provenance-carrying default, or ``None`` if not permitted."""
        for candidate in self.allowed_defaults:
            if candidate.path == path:
                return DefaultRecord(
                    path=candidate.path,
                    value=candidate.value,
                    source=self.profile_id,
                    source_version=self.version,
                    reason=candidate.reason,
                    impact=candidate.impact,
                    override_allowed=candidate.override_allowed,
                )
        return None

    def tolerance(self) -> ToleranceProfile:
        """Resolve the referenced computational tolerance profile."""
        profile_id, _, version = self.tolerance_profile_ref.partition("@")
        return ToleranceProfile(id=profile_id, version=version or "1.0")


def available_profiles(directory: Path | None = None) -> list[str]:
    root = directory or PROFILES_DIR
    return sorted(path.stem for path in root.glob("*.yaml"))


def load_profile(profile_ref: str, directory: Path | None = None) -> CompanyProfile:
    """Load ``profile_id`` or ``profile_id@version`` from the profiles directory."""
    root = directory or PROFILES_DIR
    profile_id, _, requested_version = profile_ref.partition("@")
    path = root / f"{profile_id}.yaml"
    if not path.is_file():
        raise StandardProfileNotFoundError(
            f"Standard profile '{profile_id}' is not installed",
            required_action="Install the company profile or select an available one",
            details={"requested": profile_ref, "available": available_profiles(root)},
        )

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profile = CompanyProfile.model_validate(data)
    if requested_version and profile.version != requested_version:
        raise StandardProfileNotFoundError(
            f"Standard profile '{profile_id}' version mismatch",
            required_action="Request the installed version or update the profile",
            details={"requested_version": requested_version, "installed_version": profile.version},
        )
    return profile
