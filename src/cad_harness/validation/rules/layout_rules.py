"""Layout, DWS, layer and multi-view validation rules."""

from __future__ import annotations

from dataclasses import dataclass

from cad_harness.domain.models.validation import Finding, Severity, ValidationStage
from cad_harness.validation.engine import RuleContext
from cad_harness.validation.findings import finding

PLAN_STAGES = (ValidationStage.PLAN, ValidationStage.COMPANY_STANDARD, ValidationStage.PRE_COMMIT)


@dataclass(frozen=True, slots=True)
class NoUndeclaredLayerRule:
    rule_id: str = "NO_UNDECLARED_LAYER"
    stages: tuple[ValidationStage, ...] = PLAN_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        declared = context.profile.layer_names()
        return [
            finding(
                self.rule_id,
                Severity.ERROR,
                "Operation references a layer outside the selected company profile",
                feature_id=operation.feature_id,
                operation_id=operation.operation_id,
                expected=sorted(declared),
                actual=operation.layer,
            )
            for operation in context.require_plan().operations
            if operation.layer not in declared
        ]


@dataclass(frozen=True, slots=True)
class LayoutProfileRule:
    rule_id: str = "LAYOUT_PROFILE_MATCH"
    stages: tuple[ValidationStage, ...] = (
        ValidationStage.COMPANY_STANDARD,
        ValidationStage.DRAWING_STANDARD,
    )

    def evaluate(self, context: RuleContext) -> list[Finding]:
        observed = context.extras.get("layout")
        if not isinstance(observed, dict):
            return []
        expected = context.profile.layout_rules
        findings: list[Finding] = []
        for key, configured in (
            ("layout_name", expected.layout_name),
            ("viewport_scale", expected.viewport_scale),
            ("print_scale", expected.print_scale),
        ):
            if configured is not None and observed.get(key) != configured:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        f"Observed {key} does not match the company layout profile",
                        expected=configured,
                        actual=observed.get(key),
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class DwsLayerRule:
    rule_id: str = "DWS_LAYER_MATCH"
    stages: tuple[ValidationStage, ...] = (
        ValidationStage.COMPANY_STANDARD,
        ValidationStage.DRAWING_STANDARD,
    )

    def evaluate(self, context: RuleContext) -> list[Finding]:
        observed = context.extras.get("layer_definitions")
        if not isinstance(observed, dict):
            return []
        findings: list[Finding] = []
        for layer in context.profile.layers:
            actual = observed.get(layer.name)
            expected = {
                "color_index": layer.color_index,
                "lineweight": layer.lineweight,
                "linetype": layer.linetype,
            }
            if actual != expected:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        f"Layer '{layer.name}' does not match the profile DWS definition",
                        expected=expected,
                        actual=actual,
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class ViewProjectionAlignmentRule:
    rule_id: str = "VIEW_PROJECTION_ALIGNMENT"
    stages: tuple[ValidationStage, ...] = PLAN_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        origins: dict[str, tuple[float, float]] = {}
        for operation in context.require_plan().operations:
            view_type = operation.geometry.get("view_type")
            raw_origin = operation.geometry.get("view_origin_mm")
            if (
                isinstance(view_type, str)
                and isinstance(raw_origin, list | tuple)
                and len(raw_origin) == 2
            ):
                origins.setdefault(view_type, (float(raw_origin[0]), float(raw_origin[1])))
        top = origins.get("top")
        findings: list[Finding] = []
        if top is None:
            return findings
        spacing = context.profile.layout_rules.view_spacing_mm
        for view_type, axis, expected_delta in (
            ("front", 0, spacing),
            ("side", 1, spacing),
        ):
            other = origins.get(view_type)
            if other is None:
                continue
            aligned = context.tolerance.length_close(other[axis], top[axis])
            distance = abs(other[1 - axis] - top[1 - axis])
            spaced = spacing is not None and context.tolerance.length_close(distance, spacing)
            if aligned and spaced:
                continue
            findings.append(
                finding(
                    self.rule_id,
                    Severity.ERROR,
                    f"{view_type} view is not projection-aligned with the top view",
                    expected={"aligned_axis": axis, "spacing_mm": expected_delta},
                    actual={"top_origin_mm": top, "view_origin_mm": other},
                    tolerance=context.tolerance.absolute_length_mm,
                )
            )
        return findings
