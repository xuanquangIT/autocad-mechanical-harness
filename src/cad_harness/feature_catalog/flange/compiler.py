"""Flange compiler - skeleton, not yet registered.

Intended geometry: outer circle, optional bore circle, a bolt circle child, plus
centerlines. The rules that must hold before this can be registered (architecture
section 15.3):

* ``outside_diameter_mm > pcd_mm + hole_diameter_mm + 2 * minimum_ligament_mm``
* hole count is a positive integer
* every hole centre lies on the PCD within tolerance
* angular spacing equals ``360 / count`` within tolerance

Registration checklist is the Definition of Done in architecture section 29:
3 golden cases (normal, boundary, invalid), unit tests, property tests, preview
support, and an adapter mapping or an explicit capability gap.
"""

from __future__ import annotations

from cad_harness.domain.errors import UnsupportedFeatureError
from cad_harness.domain.models.drawing_spec import FeatureSpec
from cad_harness.feature_catalog.base import CompileContext, CompiledFeature, InputReport


class FlangeCompiler:
    """Placeholder so the contract is visible before the implementation lands."""

    feature_type = "flange"
    schema_version = "1.0"
    description = "Circular flange with a bore and a bolt circle. Planned, not yet available."
    required_parameters = (
        "outside_diameter_mm",
        "thickness_mm",
        "center_mm",
        "hole_diameter_mm",
        "hole_count",
        "pcd_mm",
    )
    optional_parameters = ("bore_diameter_mm", "start_angle_deg", "material")

    def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
        report = InputReport()
        prefix = f"features[{feature.feature_id}].parameters"
        for key in self.required_parameters:
            report.require(
                feature.parameters.get(key) is not None,
                f"{prefix}.{key}",
                f"{key} is a required flange input",
            )
        return report

    def compile(self, feature: FeatureSpec, context: CompileContext) -> CompiledFeature:
        raise UnsupportedFeatureError(
            "Flange compilation is not implemented yet",
            required_action="Use bolt_circle_pattern with an explicit centre, or wait for Phase 1",
            details={"feature_id": feature.feature_id, "feature_type": self.feature_type},
        )
