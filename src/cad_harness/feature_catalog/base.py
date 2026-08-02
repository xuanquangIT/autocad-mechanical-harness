"""Feature compiler contract (architecture section 7.3).

A compiler turns one :class:`FeatureSpec` into operations plus the measurable
expectations validation will later check. It never calls AutoCAD and never invents
an engineering value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.models.drawing_spec import (
    Assumption,
    DefaultRecord,
    FeatureSpec,
    MissingInput,
)
from cad_harness.domain.models.operation_plan import Operation, ValidationExpectation
from cad_harness.geometry.primitives import BoundingBox, Point2D
from cad_harness.geometry.tolerance import ToleranceProfile


@dataclass(frozen=True, slots=True)
class CompileContext:
    """Everything a compiler is allowed to rely on besides the feature itself."""

    profile: CompanyProfile
    tolerance: ToleranceProfile
    #: Resolved placement origin. ``None`` means the spec did not supply a datum,
    #: which is a missing input for any feature whose placement depends on it.
    datum: Point2D | None = None
    #: Set by a parent compiler when compiling child features, so a hole pattern can
    #: resolve edge offsets against the real outline instead of assuming one.
    parent_feature_id: str | None = None
    parent_box: BoundingBox | None = None

    def layer_for(self, purpose: str) -> str:
        return self.profile.layer_for(purpose)

    def for_child(self, parent_feature_id: str, parent_box: BoundingBox) -> CompileContext:
        return CompileContext(
            profile=self.profile,
            tolerance=self.tolerance,
            datum=self.datum,
            parent_feature_id=parent_feature_id,
            parent_box=parent_box,
        )


@dataclass(slots=True)
class InputReport:
    """Result of ``validate_inputs``. Compilation is refused while it is not clean."""

    missing: list[MissingInput] = field(default_factory=list)
    defaults_applied: list[DefaultRecord] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.missing

    def require(self, present: bool, path: str, reason: str, *formats: str) -> None:
        """Record a missing input unless ``present``."""
        if not present:
            self.missing.append(
                MissingInput(path=path, reason=reason, accepted_formats=tuple(formats))
            )

    def merge(self, other: InputReport) -> None:
        self.missing.extend(other.missing)
        self.defaults_applied.extend(other.defaults_applied)
        self.assumptions.extend(other.assumptions)


@dataclass(slots=True)
class CompiledFeature:
    """Deterministic output of a compiler."""

    feature_id: str
    operations: list[Operation] = field(default_factory=list)
    expectations: list[ValidationExpectation] = field(default_factory=list)
    defaults_applied: list[DefaultRecord] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)

    def merge(self, other: CompiledFeature) -> None:
        self.operations.extend(other.operations)
        self.expectations.extend(other.expectations)
        self.defaults_applied.extend(other.defaults_applied)
        self.assumptions.extend(other.assumptions)


@runtime_checkable
class FeatureCompiler(Protocol):
    """Implemented once per feature type. See the Definition of Done, section 29.

    Members are read-only properties so an implementation can declare them as plain
    class attributes with literal tuples.
    """

    @property
    def feature_type(self) -> str: ...

    @property
    def schema_version(self) -> str: ...

    @property
    def description(self) -> str:
        """Human-readable summary surfaced by ``cad_feature_catalog_search``."""
        ...

    @property
    def required_parameters(self) -> tuple[str, ...]:
        """Parameters that must be supplied. Missing ones are asked for, never defaulted."""
        ...

    @property
    def optional_parameters(self) -> tuple[str, ...]: ...

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport: ...

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature: ...


def operation_id(feature_id: str, suffix: str) -> str:
    """Stable, readable operation id. Stability matters: it lands in the plan hash."""
    return f"op:{feature_id}:{suffix}"
