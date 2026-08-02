"""Post-commit measurement rules.

The write path is not trusted. After commit, entities are read back and measured; a
mismatch here is what triggers rollback rather than a silent success.
"""

from __future__ import annotations

from dataclasses import dataclass

from cad_harness.domain.models.validation import Finding, Severity, ValidationStage
from cad_harness.validation.engine import RuleContext
from cad_harness.validation.findings import finding


@dataclass(frozen=True, slots=True)
class EveryOperationProducedEntityRule:
    rule_id: str = "POST-OPERATION-COVERAGE"
    stages: tuple[ValidationStage, ...] = (ValidationStage.POST_COMMIT,)

    def evaluate(self, context: RuleContext) -> list[Finding]:
        result = context.commit_result
        if result is None:
            return [
                finding(
                    self.rule_id,
                    Severity.BLOCKING,
                    "Post-commit validation ran without a commit result",
                    expected="commit result present",
                    actual=None,
                )
            ]

        produced = {entity.operation_id for entity in result.entity_results}
        findings: list[Finding] = []
        for operation in context.plan.operations:
            if operation.operation_id not in produced:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.BLOCKING,
                        "Operation produced no entity",
                        feature_id=operation.feature_id,
                        operation_id=operation.operation_id,
                        expected="one or more entities",
                        actual=None,
                        suggested_fix="Roll back to the checkpoint and reconcile the job",
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class MeasurementMatchesExpectationRule:
    """Compare each read-back measurement against the operation's expectation."""

    rule_id: str = "POST-MEASUREMENT-MATCH"
    stages: tuple[ValidationStage, ...] = (ValidationStage.POST_COMMIT,)

    def evaluate(self, context: RuleContext) -> list[Finding]:
        result = context.commit_result
        if result is None:
            return []

        tolerance = context.tolerance
        operations = {op.operation_id: op for op in context.plan.operations}
        findings: list[Finding] = []

        for entity in result.entity_results:
            operation = operations.get(entity.operation_id)
            if operation is None:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Adapter reported an entity for an unknown operation",
                        entity_ref=entity.entity_ref,
                        expected="operation present in the committed plan",
                        actual=entity.operation_id,
                    )
                )
                continue

            for key, expected_value in operation.expected.items():
                if key not in entity.measurements:
                    findings.append(
                        finding(
                            self.rule_id,
                            Severity.WARNING,
                            f"Adapter did not measure '{key}'",
                            feature_id=operation.feature_id,
                            operation_id=operation.operation_id,
                            entity_ref=entity.entity_ref,
                            expected=expected_value,
                            actual=None,
                            suggested_fix="Declare the capability gap or implement the read-back",
                        )
                    )
                    continue

                actual_value = entity.measurements[key]
                if not _matches(key, expected_value, actual_value, tolerance):
                    findings.append(
                        finding(
                            self.rule_id,
                            Severity.BLOCKING,
                            f"Committed geometry does not match the approved plan for '{key}'",
                            feature_id=operation.feature_id,
                            operation_id=operation.operation_id,
                            entity_ref=entity.entity_ref,
                            expected=expected_value,
                            actual=actual_value,
                            tolerance=tolerance.absolute_length_mm,
                            suggested_fix="Roll back to the checkpoint and re-plan",
                        )
                    )
        return findings


def _matches(key: str, expected: object, actual: object, tolerance: object) -> bool:
    from cad_harness.geometry.tolerance import ToleranceProfile

    assert isinstance(tolerance, ToleranceProfile)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected == actual
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        if key.endswith("_mm2"):
            return tolerance.area_close(float(expected), float(actual))
        if key.endswith("_deg"):
            return tolerance.angle_close_deg(float(expected), float(actual))
        if key.endswith("_mm"):
            return tolerance.length_close(float(expected), float(actual))
        return expected == actual
    return expected == actual
