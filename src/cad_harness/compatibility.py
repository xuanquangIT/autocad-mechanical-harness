"""Fail-closed AutoCAD writer compatibility policy.

The published matrix identifies AutoCAD's COM version prefix independently of
the longer product strings returned by ``Application.Version``.  Writer paths
must call :func:`require_writer_compatible` before mutating a drawing.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cad_harness.domain.errors import AdapterCapabilityMissingError
from cad_harness.domain.ports.autocad_adapter import AdapterStatus

DEFAULT_COMPATIBILITY_PATH = Path(__file__).resolve().parents[2] / "config" / "compatibility.yaml"
# AutoCAD's COM ``Application.Version`` is a compact version followed by an
# optional build-channel suffix/label (for example ``24.3s (LMS Tech)``).  Keep
# this grammar anchored: finding a supported-looking substring inside arbitrary
# text would turn untrusted status text into a writer capability bypass.
_COM_VERSION_PATTERN = re.compile(
    r"^(?:AutoCAD\s+)?(?P<version>\d{2}\.\d)(?:[A-Za-z])?"
    r"(?:\s+\([^()\r\n]+\))?$",
    re.IGNORECASE,
)


class CompatibilityPolicy(StrEnum):
    """Whether a release is allowed to use the writer bridge."""

    TARGET = "target"
    PROVISIONAL = "provisional"


class VerificationStatus(StrEnum):
    """Live verification state, kept separate from the declared policy."""

    PLANNED = "planned"
    PROVISIONAL = "provisional"
    VERIFIED = "verified"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompatibilityTarget(_FrozenModel):
    """One AutoCAD release/runtime target from the compatibility policy."""

    autocad_release: int = Field(ge=2000, le=9999)
    com_version_prefix: str = Field(min_length=4, max_length=8)
    dotnet_runtime: str = Field(min_length=1)
    policy: CompatibilityPolicy
    verification_status: VerificationStatus

    @field_validator("com_version_prefix")
    @classmethod
    def _require_normalized_prefix(cls, value: str) -> str:
        normalized = normalize_autocad_version(value)
        if normalized != value:
            raise ValueError("com_version_prefix must be a normalized major.minor value")
        return value

    @property
    def display_version(self) -> str:
        return f"AutoCAD {self.autocad_release} ({self.com_version_prefix})"

    @property
    def writer_supported(self) -> bool:
        """Policy support does not imply that live verification has happened."""
        return self.policy is CompatibilityPolicy.TARGET


class CompatibilityMatrix(_FrozenModel):
    """Strict YAML-backed compatibility policy for the bridge bundle."""

    schema_version: str = Field(min_length=1)
    bridge_bundle_version: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    targets: tuple[CompatibilityTarget, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_unique_targets(self) -> CompatibilityMatrix:
        releases = [target.autocad_release for target in self.targets]
        prefixes = [target.com_version_prefix for target in self.targets]
        if len(releases) != len(set(releases)):
            raise ValueError("autocad_release values must be unique")
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("com_version_prefix values must be unique")
        return self

    @property
    def supported_versions(self) -> tuple[str, ...]:
        return tuple(target.display_version for target in self.targets if target.writer_supported)

    def target_for_version(self, detected_version: str | None) -> CompatibilityTarget | None:
        normalized = normalize_autocad_version(detected_version)
        if normalized is None:
            return None
        return next(
            (target for target in self.targets if target.com_version_prefix == normalized),
            None,
        )

    def is_writer_supported(self, detected_version: str | None) -> bool:
        target = self.target_for_version(detected_version)
        return target is not None and target.writer_supported

    def evaluate_status(self, status: AdapterStatus) -> AdapterStatus:
        """Return an immutable status copy with the matrix decision attached."""
        supported = (
            None if status.cad_version is None else self.is_writer_supported(status.cad_version)
        )
        return status.model_copy(update={"version_supported": supported})

    def require_writer_compatible(
        self, detected: AdapterStatus | str | None
    ) -> CompatibilityTarget:
        detected_version = detected.cad_version if isinstance(detected, AdapterStatus) else detected
        target = self.target_for_version(detected_version)
        if target is not None and target.writer_supported:
            return target
        raise AdapterCapabilityMissingError(
            "The detected AutoCAD version is not supported by the writer bridge",
            required_action="Use a supported AutoCAD release or install a compatible bridge bundle",
            details={
                "detected_version": detected_version,
                "supported_versions": list(self.supported_versions),
            },
        )


def normalize_autocad_version(detected_version: str | None) -> str | None:
    """Extract ``major.minor`` from real AutoCAD COM version strings.

    Examples include ``24.3s (LMS Tech)`` and ``AutoCAD 25.0``.  The complete
    value must match the documented grammar; a version-like substring is not
    sufficient.  A
    missing or unrecognizable value returns ``None`` so callers fail closed.
    """
    if detected_version is None:
        return None
    if not isinstance(detected_version, str):
        raise TypeError("detected_version must be a string or None")
    match = _COM_VERSION_PATTERN.fullmatch(detected_version.strip())
    return match.group("version") if match else None


def load_compatibility_matrix(
    path: Path | str = DEFAULT_COMPATIBILITY_PATH,
) -> CompatibilityMatrix:
    """Load and validate the immutable compatibility matrix."""
    raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return CompatibilityMatrix.model_validate(raw)


def evaluate_adapter_status(
    status: AdapterStatus,
    matrix: CompatibilityMatrix | None = None,
) -> AdapterStatus:
    """Evaluate ``AdapterStatus.version_supported`` against the matrix."""
    policy = matrix or load_compatibility_matrix()
    return policy.evaluate_status(status)


def require_writer_compatible(
    detected: AdapterStatus | str | None,
    matrix: CompatibilityMatrix | None = None,
) -> CompatibilityTarget:
    """Return the matching target or raise a client-safe capability error."""
    policy = matrix or load_compatibility_matrix()
    return policy.require_writer_compatible(detected)
