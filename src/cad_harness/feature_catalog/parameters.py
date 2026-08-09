"""Strict parameter parsing shared by feature compilers."""

from __future__ import annotations

from typing import Any

from cad_harness.domain.errors import InvalidFeatureParametersError, MissingRequiredInputsError
from cad_harness.feature_catalog.base import InputReport
from cad_harness.geometry.primitives import Point2D


def number(parameters: dict[str, Any], key: str) -> float | None:
    value = parameters.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise InvalidFeatureParametersError(
            f"Parameter '{key}' must be numeric", details={key: repr(value)}
        )
    return float(value)


def integer(parameters: dict[str, Any], key: str) -> int | None:
    value = parameters.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidFeatureParametersError(
            f"Parameter '{key}' must be an integer", details={key: repr(value)}
        )
    return value


def point(value: object, key: str) -> Point2D:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise InvalidFeatureParametersError(
            f"{key} must be a two-element [x, y] pair", details={key: repr(value)}
        )
    try:
        return Point2D(float(value[0]), float(value[1]))
    except (TypeError, ValueError) as error:
        raise InvalidFeatureParametersError(
            f"{key} must contain numeric coordinates", details={key: repr(value)}
        ) from error


def missing_error(message: str, report: InputReport) -> MissingRequiredInputsError:
    return MissingRequiredInputsError(
        message,
        required_action="Supply every missing feature input and resubmit the spec",
        details={"missing_inputs": [item.model_dump(mode="json") for item in report.missing]},
    )
