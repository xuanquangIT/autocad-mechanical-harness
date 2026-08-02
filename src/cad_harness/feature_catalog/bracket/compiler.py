"""L-bracket compiler - skeleton, not yet registered.

Needs a six-vertex outline plus optional inner fillet. The fillet is the interesting
part: its tangency must be validated, not assumed, so the geometry kernel needs a
fillet routine with an explicit tangency check before this feature can register.
"""

from __future__ import annotations

from cad_harness.domain.errors import UnsupportedFeatureError
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.feature_catalog.base import CompileContext, CompiledFeature, InputReport


class LBracketCompiler:
    feature_type = "l_bracket"
    schema_version = "1.0"
    description = "L-shaped bracket profile in a 2D view. Planned, not yet available."
    required_parameters = ("leg_a_mm", "leg_b_mm", "thickness_mm", "origin_mm")
    optional_parameters = ("inner_fillet_radius_mm", "material")

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        prefix = f"features[{feature.feature_id}].parameters"
        for key in self.required_parameters:
            report.require(
                feature.parameters.get(key) is not None,
                f"{prefix}.{key}",
                f"{key} is a required bracket input",
            )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        raise UnsupportedFeatureError(
            "L-bracket compilation is not implemented yet",
            required_action="Wait for Phase 1 or extend the catalog per the Definition of Done",
            details={"feature_id": feature.feature_id, "feature_type": self.feature_type},
        )
