"""Finding helpers. Every finding must carry expected, actual and tolerance."""

from __future__ import annotations

from typing import Any

from cad_harness.domain.models.validation import Finding, Severity


def finding(
    rule_id: str,
    severity: Severity,
    message: str,
    *,
    feature_id: str | None = None,
    operation_id: str | None = None,
    entity_ref: str | None = None,
    expected: Any = None,
    actual: Any = None,
    tolerance: float | None = None,
    suggested_fix: str | None = None,
    measurement: dict[str, Any] | None = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message=message,
        feature_id=feature_id,
        operation_id=operation_id,
        entity_ref=entity_ref,
        expected=expected,
        actual=actual,
        tolerance=tolerance,
        suggested_fix=suggested_fix,
        measurement=measurement or {},
    )
