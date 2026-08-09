"""Company-standard reconciliation rules for an extracted DrawingModel."""

from __future__ import annotations

from dataclasses import dataclass

from cad_harness.domain.errors import HarnessError
from cad_harness.domain.models.drawing_model import (
    DimensionGeometry,
    DrawingModel,
    TextGeometry,
)
from cad_harness.domain.models.validation import Finding, Severity, ValidationStage
from cad_harness.validation.engine import RuleContext
from cad_harness.validation.findings import finding

DRAWING_STAGES = (ValidationStage.DRAWING_STANDARD,)


def _model(context: RuleContext) -> DrawingModel:
    model = context.require_drawing_model()
    if not isinstance(model, DrawingModel):
        raise HarnessError(
            "Drawing-standard rules require the complete DrawingModel contract",
            required_action="Read the drawing through DrawingReadService before validation",
        )
    return model


@dataclass(frozen=True, slots=True)
class LayerSetMatchesProfileRule:
    rule_id: str = "LAYER_SET_MATCHES_PROFILE"
    stages: tuple[ValidationStage, ...] = DRAWING_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        model = _model(context)
        expected_by_name = {layer.name: layer for layer in context.profile.layers}
        actual_by_name = {layer.name: layer for layer in model.layers}
        findings: list[Finding] = []
        for name in sorted(expected_by_name.keys() - actual_by_name.keys()):
            findings.append(
                finding(
                    self.rule_id,
                    Severity.ERROR,
                    f"Required company layer '{name}' is missing",
                    expected={"present": True, "layer": name},
                    actual={"present": False},
                    suggested_fix=f"Create layer '{name}' from the controlled standard",
                )
            )
        for name in sorted(actual_by_name.keys() - expected_by_name.keys()):
            findings.append(
                finding(
                    self.rule_id,
                    Severity.WARNING,
                    f"Drawing contains undeclared layer '{name}'",
                    expected=sorted(expected_by_name),
                    actual=name,
                    suggested_fix="Map the layer to the company standard or remove it after review",
                )
            )
        for name in sorted(expected_by_name.keys() & actual_by_name.keys()):
            expected = expected_by_name[name]
            actual = actual_by_name[name]
            expected_properties = {
                "color_index": expected.color_index,
                "linetype": expected.linetype,
                "lineweight": expected.lineweight,
            }
            actual_properties = {
                "color_index": actual.color_index,
                "linetype": actual.linetype,
                "lineweight": actual.lineweight,
            }
            if expected_properties != actual_properties:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        f"Layer '{name}' properties differ from the company profile",
                        expected=expected_properties,
                        actual=actual_properties,
                        suggested_fix=f"Restore layer '{name}' from the controlled standard",
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class EntityOnExpectedLayerRule:
    rule_id: str = "ENTITY_ON_EXPECTED_LAYER"
    stages: tuple[ValidationStage, ...] = DRAWING_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        return [
            finding(
                self.rule_id,
                Severity.ERROR,
                "Entity is assigned to the wrong company layer",
                entity_ref=entity.entity_ref,
                expected=expected,
                actual=entity.layer,
                suggested_fix=f"Move the entity to layer '{expected}' through remediation",
            )
            for entity in _model(context).entities
            if (expected := context.profile.entity_layer_map.get(entity.entity_type)) is not None
            and entity.layer != expected
        ]


@dataclass(frozen=True, slots=True)
class DimstyleInProfileRule:
    rule_id: str = "DIMSTYLE_IN_PROFILE"
    stages: tuple[ValidationStage, ...] = DRAWING_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        allowed = context.profile.dimension_styles or (
            (context.profile.dimension_style,) if context.profile.dimension_style else ()
        )
        return [
            finding(
                self.rule_id,
                Severity.ERROR,
                "Dimension uses a style outside the company profile",
                entity_ref=entity.entity_ref,
                expected=list(allowed),
                actual=geometry.dimension_style,
                suggested_fix="Apply an approved dimension style through remediation",
            )
            for entity in _model(context).entities
            if isinstance((geometry := entity.geometry), DimensionGeometry)
            and geometry.dimension_style not in allowed
        ]


@dataclass(frozen=True, slots=True)
class TextstyleInProfileRule:
    rule_id: str = "TEXTSTYLE_IN_PROFILE"
    stages: tuple[ValidationStage, ...] = DRAWING_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        allowed = context.profile.text_styles or (
            (context.profile.text_style,) if context.profile.text_style else ()
        )
        return [
            finding(
                self.rule_id,
                Severity.ERROR,
                "Text uses a style outside the company profile",
                entity_ref=entity.entity_ref,
                expected=list(allowed),
                actual=geometry.text_style,
                suggested_fix="Apply an approved text style through remediation",
            )
            for entity in _model(context).entities
            if isinstance((geometry := entity.geometry), TextGeometry)
            and geometry.text_style not in allowed
        ]


@dataclass(frozen=True, slots=True)
class DocumentUnitsMatchProfileRule:
    rule_id: str = "DOCUMENT_UNITS_MATCH_PROFILE"
    stages: tuple[ValidationStage, ...] = DRAWING_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        actual = _model(context).source_unit_code
        if actual == context.profile.canonical_unit:
            return []
        return [
            finding(
                self.rule_id,
                Severity.ERROR,
                "Document units do not match the company profile",
                expected=context.profile.canonical_unit,
                actual=actual,
                suggested_fix="Confirm units and convert the drawing before production use",
            )
        ]
