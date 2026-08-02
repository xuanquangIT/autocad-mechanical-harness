"""Spec -> OperationPlan compilation.

This is where "no silent defaults" is enforced: inputs are checked across the whole
feature tree first, and compilation is refused while anything required is absent.
"""

from __future__ import annotations

from dataclasses import dataclass

from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.models.drawing_spec import (
    Assumption,
    DefaultRecord,
    DrawingSpec,
    FeatureSpec,
    MissingInput,
)
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, ValidationExpectation
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.domain.value_objects.units import CANONICAL_UNIT
from cad_harness.feature_catalog.base import CompileContext
from cad_harness.feature_catalog.registry import get_compiler
from cad_harness.geometry.primitives import Point2D
from cad_harness.geometry.tolerance import ToleranceProfile


@dataclass(slots=True)
class CompilationResult:
    """Either a plan, or the list of inputs still needed. Never both empty."""

    plan: OperationPlan | None
    missing_inputs: list[MissingInput]
    defaults_applied: list[DefaultRecord]
    assumptions: list[Assumption]

    @property
    def needs_input(self) -> bool:
        return self.plan is None


class PlanCompilerService:
    """Compiles a normalized spec into a hashed, deterministic plan."""

    def __init__(self, profile: CompanyProfile, tolerance: ToleranceProfile) -> None:
        self.profile = profile
        self.tolerance = tolerance

    def compile(
        self, spec: DrawingSpec, *, job_id: str, expected_revision: str
    ) -> CompilationResult:
        context = CompileContext(
            profile=self.profile,
            tolerance=self.tolerance,
            datum=self._resolve_datum(spec),
        )

        # Pass 1: collect every missing input so the client can fix them in one round trip.
        missing: list[MissingInput] = []
        for feature in spec.features:
            missing.extend(self._collect_missing(feature, context))
        if missing:
            return CompilationResult(
                plan=None,
                missing_inputs=missing,
                defaults_applied=list(spec.explicit_defaults),
                assumptions=list(spec.assumptions),
            )

        # Pass 2: compile. Order is preserved because the plan hash depends on it.
        operations: list[Operation] = []
        expectations: list[ValidationExpectation] = []
        defaults: list[DefaultRecord] = list(spec.explicit_defaults)
        assumptions: list[Assumption] = list(spec.assumptions)

        for feature in spec.features:
            compiled = get_compiler(feature.type).compile(feature, context)
            operations.extend(compiled.operations)
            expectations.extend(compiled.expectations)
            defaults.extend(compiled.defaults_applied)
            assumptions.extend(compiled.assumptions)

        plan = OperationPlan(
            plan_id=new_id(IdPrefix.PLAN),
            job_id=job_id,
            document_id=spec.document_id,
            expected_revision=expected_revision,
            canonical_units=CANONICAL_UNIT,
            profile_ref=self.profile.as_ref(),
            operations=tuple(operations),
            validation_expectations=tuple(expectations),
        ).with_hash()

        return CompilationResult(
            plan=plan,
            missing_inputs=[],
            defaults_applied=defaults,
            assumptions=assumptions,
        )

    def _collect_missing(self, feature: FeatureSpec, context: CompileContext) -> list[MissingInput]:
        """Walk the feature tree gathering missing inputs.

        Children are checked against a placeholder parent box: a child's own required
        parameters do not depend on the parent's exact size, only on its presence.
        """
        report = get_compiler(feature.type).validate_inputs(feature, context)
        missing = list(report.missing)

        if feature.children:
            from cad_harness.geometry.primitives import BoundingBox

            child_context = context.for_child(feature.feature_id, BoundingBox(0.0, 0.0, 1.0, 1.0))
            for child in feature.children:
                missing.extend(self._collect_missing(child, child_context))
        return missing

    @staticmethod
    def _resolve_datum(spec: DrawingSpec) -> Point2D | None:
        datum = spec.drawing.datum
        if datum is None or datum.point_mm is None:
            # A named or interactively selected datum must be resolved by the caller
            # before submission; the compiler will not guess coordinates for it.
            return None
        return Point2D(float(datum.point_mm[0]), float(datum.point_mm[1]))
