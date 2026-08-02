"""Slot / keyway compiler - skeleton, not yet registered.

The straight-flank geometry already exists in :func:`cad_harness.geometry.patterns.slot_outline`.
What remains is choosing the target representation (polyline with bulges vs. line +
arc entities), which differs per adapter capability, and the tangency validation.
"""

from __future__ import annotations

from cad_harness.domain.errors import UnsupportedFeatureError
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.feature_catalog.base import CompileContext, CompiledFeature, InputReport


class SlotCompiler:
    feature_type = "slot"
    schema_version = "1.0"
    description = "Obround slot or keyway in a 2D view. Planned, not yet available."
    required_parameters = ("length_mm", "width_mm", "center_mm")
    optional_parameters = ("angle_deg", "through", "add_centerlines")

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        prefix = f"features[{feature.feature_id}].parameters"
        for key in self.required_parameters:
            report.require(
                feature.parameters.get(key) is not None,
                f"{prefix}.{key}",
                f"{key} is a required slot input",
            )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        raise UnsupportedFeatureError(
            "Slot compilation is not implemented yet",
            required_action="Wait for Phase 1 or extend the catalog per the Definition of Done",
            details={"feature_id": feature.feature_id, "feature_type": self.feature_type},
        )
