"""Spec -> OperationPlan compilation.

This is where "no silent defaults" is enforced: inputs are checked across the whole
feature tree first, and compilation is refused while anything required is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cad_harness.annotation.engine import AnnotationEngine
from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.errors import AdapterCapabilityMissingError
from cad_harness.domain.models.drawing_spec import (
    Assumption,
    DefaultRecord,
    DrawingSpec,
    FeatureSpec,
    MissingInput,
)
from cad_harness.domain.models.operation_plan import (
    Operation,
    OperationPlan,
    OperationType,
    ValidationExpectation,
)
from cad_harness.domain.ports.autocad_adapter import AutoCADAdapter
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.domain.value_objects.units import CANONICAL_UNIT
from cad_harness.feature_catalog.base import CompileContext
from cad_harness.feature_catalog.registry import get_compiler
from cad_harness.feature_catalog.views import place_views
from cad_harness.geometry.primitives import Point2D
from cad_harness.geometry.tolerance import ToleranceProfile

if TYPE_CHECKING:
    from cad_harness.application.services.raster_trace_service import RasterTraceService
    from cad_harness.feature_catalog.base import CompiledFeature, InputReport

_ACCEPTED_RASTER_FEATURE = "_accepted_raster_trace"


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

    def __init__(
        self,
        profile: CompanyProfile,
        tolerance: ToleranceProfile,
        adapter: AutoCADAdapter | None = None,
        raster_trace_service: RasterTraceService | None = None,
    ) -> None:
        self.profile = profile
        self.tolerance = tolerance
        self.adapter = adapter
        self.raster_trace_service = raster_trace_service

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

        # Phase 1: compile geometry in semantic feature order.
        geometry_operations: list[Operation] = []
        expectations: list[ValidationExpectation] = []
        defaults: list[DefaultRecord] = list(spec.explicit_defaults)
        assumptions: list[Assumption] = list(spec.assumptions)

        for feature in spec.features:
            if feature.type == _ACCEPTED_RASTER_FEATURE:
                compiled = self._compile_accepted_raster(feature)
            else:
                compiled = get_compiler(feature.type).compile(feature, context)
            geometry_operations.extend(compiled.operations)
            expectations.extend(compiled.expectations)
            defaults.extend(compiled.defaults_applied)
            assumptions.extend(compiled.assumptions)

        if spec.drawing.views:
            geometry_operations = list(
                place_views(tuple(geometry_operations), spec.drawing.views, self.profile).operations
            )

        # Legacy feature compilers may still request center entities. Phase two owns
        # them now, so remove those requests to avoid duplicate annotation entities.
        geometry_operations = [
            operation
            for operation in geometry_operations
            if operation.type
            not in {OperationType.CREATE_CENTERMARK, OperationType.CREATE_CENTERLINE}
        ]

        # Phase 2: annotations read only final phase-one operation geometry.
        annotation = AnnotationEngine(self.profile, self.tolerance).annotate(
            geometry_operations=tuple(geometry_operations),
            spec=spec,
            datum=context.datum,
        )
        if annotation.missing_inputs:
            return CompilationResult(
                plan=None,
                missing_inputs=annotation.missing_inputs,
                defaults_applied=defaults + annotation.defaults_applied,
                assumptions=assumptions,
            )
        defaults.extend(annotation.defaults_applied)
        expectations.extend(annotation.expectations)

        # Phase 3: geometry remains first and annotation remains second. Never sort.
        operations = [*geometry_operations, *annotation.operations]
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

        if self.adapter is not None:
            self.preflight(plan)

        return CompilationResult(
            plan=plan,
            missing_inputs=[],
            defaults_applied=defaults,
            assumptions=assumptions,
        )

    def preflight(self, plan: OperationPlan) -> None:
        """Reject an incomplete write mapping before preview or job state advancement."""
        if self.adapter is None:
            return
        missing_operations = self.adapter.unsupported_operations(plan)
        if not missing_operations:
            return
        missing_names = [operation.value for operation in missing_operations]
        raise AdapterCapabilityMissingError(
            "The configured adapter cannot map every operation in the compiled plan",
            required_action="Select a capable adapter or change the requested features",
            details={
                "adapter_type": self.adapter.status().adapter_type,
                "operation_type": missing_names[0],
                "missing_operations": missing_names,
            },
        )

    def _collect_missing(self, feature: FeatureSpec, context: CompileContext) -> list[MissingInput]:
        """Walk the feature tree gathering missing inputs.

        Children are checked against a placeholder parent box: a child's own required
        parameters do not depend on the parent's exact size, only on its presence.
        """
        if feature.type == _ACCEPTED_RASTER_FEATURE:
            report = self._validate_accepted_raster(feature)
        else:
            report = get_compiler(feature.type).validate_inputs(feature, context)
        missing = list(report.missing)

        if feature.children:
            from cad_harness.geometry.primitives import BoundingBox, Point2D, Polyline2D

            placeholder_outline = Polyline2D(
                (
                    Point2D(0.0, 0.0),
                    Point2D(1.0, 0.0),
                    Point2D(1.0, 1.0),
                    Point2D(0.0, 1.0),
                ),
                closed=True,
            )
            child_context = context.for_child(
                feature.feature_id, BoundingBox(0.0, 0.0, 1.0, 1.0), placeholder_outline
            )
            for child in feature.children:
                missing.extend(self._collect_missing(child, child_context))
        return missing

    def _validate_accepted_raster(self, feature: FeatureSpec) -> InputReport:
        """Validate the sealed draft shape without trusting caller-supplied operations."""
        from cad_harness.feature_catalog.base import InputReport

        report = InputReport()
        prefix = f"features[{feature.feature_id}].parameters"
        report.require(
            self.raster_trace_service is not None,
            f"{prefix}.raster_trace_service",
            "This harness instance has no local raster acceptance verifier",
            "configured local RasterTraceService",
        )
        report.require(
            isinstance(feature.parameters.get("report"), dict),
            f"{prefix}.report",
            "The source-bound RasterTraceReport is required",
            "RasterTraceReport object",
        )
        report.require(
            isinstance(feature.parameters.get("acceptance"), dict),
            f"{prefix}.acceptance",
            "The engineer's source-bound acceptance is required",
            "RasterTraceAcceptance object",
        )
        report.require(
            isinstance(feature.parameters.get("acceptance_token"), str),
            f"{prefix}.acceptance_token",
            "The signed raster acceptance token is required",
            "raster-v1 token from the Engineer Desktop or CLI",
        )
        report.require(
            isinstance(feature.parameters.get("layer"), str)
            and bool(str(feature.parameters.get("layer", "")).strip()),
            f"{prefix}.layer",
            "The engineer must explicitly choose the target layer",
            "non-empty AutoCAD layer name",
        )
        report.require(
            not feature.children and not feature.modifiers,
            f"features[{feature.feature_id}]",
            "Accepted raster geometry cannot carry child features or outline modifiers",
            "one flat source-bound raster feature",
        )
        return report

    def _compile_accepted_raster(self, feature: FeatureSpec) -> CompiledFeature:
        """Verify the signed trace and derive operations; never accept operation JSON."""
        from pydantic import ValidationError

        from cad_harness.domain.errors import InvalidFeatureParametersError
        from cad_harness.domain.models.raster import RasterTraceAcceptance, RasterTraceReport
        from cad_harness.feature_catalog.base import CompiledFeature

        validation = self._validate_accepted_raster(feature)
        if not validation.is_complete or self.raster_trace_service is None:
            raise InvalidFeatureParametersError(
                "Accepted raster draft is incomplete",
                required_action="Create a fresh draft with cad_image_draft",
                details={"missing_inputs": [item.path for item in validation.missing]},
            )
        try:
            report = RasterTraceReport.model_validate(feature.parameters["report"])
            acceptance = RasterTraceAcceptance.model_validate(feature.parameters["acceptance"])
        except ValidationError as exc:
            raise InvalidFeatureParametersError(
                "Accepted raster draft contracts are invalid",
                required_action="Create a fresh draft from the current raster trace",
            ) from exc
        operations = self.raster_trace_service.draft_operations(
            report,
            acceptance,
            str(feature.parameters["acceptance_token"]),
            layer=str(feature.parameters["layer"]),
        )
        return CompiledFeature(
            feature_id=feature.feature_id,
            operations=list(operations),
            assumptions=[
                Assumption(
                    path=f"features[{feature.feature_id}]",
                    statement=(
                        "Geometry was reconstructed from a calibrated raster and remains "
                        "unverified until CAD readback and validation succeed"
                    ),
                    affects_geometry=True,
                    requires_approval=True,
                )
            ],
        )

    @staticmethod
    def _resolve_datum(spec: DrawingSpec) -> Point2D | None:
        datum = spec.drawing.datum
        if datum is None or datum.point_mm is None:
            # A named or interactively selected datum must be resolved by the caller
            # before submission; the compiler will not guess coordinates for it.
            return None
        return Point2D(float(datum.point_mm[0]), float(datum.point_mm[1]))
