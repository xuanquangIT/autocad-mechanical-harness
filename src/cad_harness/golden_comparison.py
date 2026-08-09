"""Deterministic semantic comparison for golden drawings and take-off reports.

Golden evidence deliberately compares engineering meaning rather than serialized
DWG/JSON bytes.  Entity handles, references, timestamps, and semantically irrelevant
collection order are excluded; geometry measurements and drawing standards are not.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from enum import Enum
from numbers import Integral, Real
from typing import TypeGuard

from pydantic import BaseModel

from cad_harness.geometry.tolerance import DEMO_TOLERANCE, ToleranceProfile

_MISSING = object()
_ORDER_INSENSITIVE_COLLECTIONS = frozenset(
    {"entities", "parts", "hole_groups", "excluded_contours", "findings"}
)
_EXACT_NUMERIC_FIELDS = frozenset(
    {
        "count",
        "entity_count",
        "pierce_count",
        "quantity",
        "blocking_count",
        "error_count",
        "warning_count",
        "info_count",
    }
)


@dataclass(frozen=True, slots=True)
class GoldenComparisonConfig:
    """Policy for semantic golden comparisons.

    ``ignored_fields`` may be extended by a caller, but engineering fields such as
    layer, style, feature, operation, and measurement are compared by default.
    """

    tolerance: ToleranceProfile = DEMO_TOLERANCE
    ignored_fields: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "created_at",
                "generated_at",
                "timestamp",
                "updated_at",
                "entity_ref",
                "entity_refs",
                "handle",
                "handles",
            }
        )
    )


@dataclass(frozen=True, slots=True)
class GoldenMismatch:
    """One deterministic, human-readable semantic difference."""

    path: str
    expected: object
    actual: object
    reason: str


@dataclass(frozen=True, slots=True)
class GoldenComparisonResult:
    """Comparison outcome returned by both public comparators."""

    mismatches: tuple[GoldenMismatch, ...] = ()

    @property
    def matches(self) -> bool:
        return not self.mismatches

    def assert_matches(self) -> None:
        """Raise a compact assertion suitable for pytest and the golden runner."""

        if self.matches:
            return
        lines = [
            f"{item.path}: {item.reason}; expected={item.expected!r}, actual={item.actual!r}"
            for item in self.mismatches
        ]
        raise AssertionError("Semantic golden mismatch:\n" + "\n".join(lines))


def compare_semantic_entities(
    expected: object,
    actual: object,
    *,
    config: GoldenComparisonConfig | None = None,
) -> GoldenComparisonResult:
    """Compare entity type/feature/operation/layer/style/measurements semantically.

    Inputs may be Pydantic models, dictionaries, or the ``entities`` sequence from a
    golden fixture.  Handles and ordering never establish entity identity.
    """

    policy = config or GoldenComparisonConfig()
    expected_entities = _extract_entities(_to_data(expected))
    actual_entities = _extract_entities(_to_data(actual))
    mismatches = _compare(
        {"entities": expected_entities}, {"entities": actual_entities}, "$", policy
    )
    return GoldenComparisonResult(tuple(mismatches))


def compare_takeoff_reports(
    expected: object,
    actual: object,
    *,
    config: GoldenComparisonConfig | None = None,
) -> GoldenComparisonResult:
    """Compare take-off report content deterministically, never serialized bytes."""

    policy = config or GoldenComparisonConfig()
    mismatches = _compare(_to_data(expected), _to_data(actual), "$", policy)
    return GoldenComparisonResult(tuple(mismatches))


def _extract_entities(value: object) -> list[object]:
    if isinstance(value, Mapping):
        entities = value.get("entities")
        if not _is_sequence(entities):
            raise TypeError("Semantic entity payload must contain an 'entities' sequence")
        return list(entities)
    if _is_sequence(value):
        return list(value)
    raise TypeError("Semantic entity payload must be a sequence or an entities mapping")


def _to_data(value: object) -> object:
    if isinstance(value, BaseModel):
        return _to_data(value.model_dump(mode="json", exclude_none=True))
    if is_dataclass(value) and not isinstance(value, type):
        return _to_data(asdict(value))
    if isinstance(value, Enum):
        return _to_data(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _to_data(item) for key, item in value.items()}
    if _is_sequence(value):
        return [_to_data(item) for item in value]
    return value


def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _is_ignored_field(key: str, config: GoldenComparisonConfig) -> bool:
    lowered = key.casefold()
    return (
        lowered in config.ignored_fields
        or lowered.endswith("_at")
        or lowered.endswith("_timestamp")
        or lowered.endswith("_handle")
        or lowered.endswith("_handles")
        or lowered.endswith("_entity_ref")
        or lowered.endswith("_entity_refs")
    )


def _compare(
    expected: object,
    actual: object,
    path: str,
    config: GoldenComparisonConfig,
) -> list[GoldenMismatch]:
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        return _compare_mappings(expected, actual, path, config)
    if _is_sequence(expected) and _is_sequence(actual):
        expected_items = list(expected)
        actual_items = list(actual)
        if _path_leaf(path) in _ORDER_INSENSITIVE_COLLECTIONS:
            return _compare_unordered(expected_items, actual_items, path, config)
        return _compare_ordered(expected_items, actual_items, path, config)
    if _is_number(expected) and _is_number(actual):
        if _numbers_close(expected, actual, path, config.tolerance):
            return []
        return [GoldenMismatch(path, expected, actual, "numeric value outside tolerance")]
    if expected != actual:
        return [GoldenMismatch(path, expected, actual, "value differs")]
    return []


def _compare_mappings(
    expected: Mapping[object, object],
    actual: Mapping[object, object],
    path: str,
    config: GoldenComparisonConfig,
) -> list[GoldenMismatch]:
    expected_fields = {
        str(key): value
        for key, value in expected.items()
        if not _is_ignored_field(str(key), config)
    }
    actual_fields = {
        str(key): value for key, value in actual.items() if not _is_ignored_field(str(key), config)
    }
    mismatches: list[GoldenMismatch] = []
    for key in sorted(expected_fields.keys() | actual_fields.keys()):
        child_path = f"{path}.{key}"
        expected_value = expected_fields.get(key, _MISSING)
        actual_value = actual_fields.get(key, _MISSING)
        if expected_value is _MISSING:
            mismatches.append(
                GoldenMismatch(child_path, "<missing>", actual_value, "unexpected field")
            )
        elif actual_value is _MISSING:
            mismatches.append(
                GoldenMismatch(child_path, expected_value, "<missing>", "missing field")
            )
        elif _path_leaf(path) == "evidence" and _references_only(expected_value, actual_value):
            # Evidence categories are semantic; their transient entity references are not.
            continue
        else:
            mismatches.extend(_compare(expected_value, actual_value, child_path, config))
    return mismatches


def _references_only(expected: object, actual: object) -> bool:
    return (
        _is_sequence(expected)
        and _is_sequence(actual)
        and all(isinstance(item, str) for item in expected)
        and all(isinstance(item, str) for item in actual)
    )


def _compare_ordered(
    expected: list[object],
    actual: list[object],
    path: str,
    config: GoldenComparisonConfig,
) -> list[GoldenMismatch]:
    mismatches: list[GoldenMismatch] = []
    if len(expected) != len(actual):
        mismatches.append(
            GoldenMismatch(path, len(expected), len(actual), "collection size differs")
        )
    for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=False)):
        mismatches.extend(_compare(expected_item, actual_item, f"{path}[{index}]", config))
    return mismatches


def _compare_unordered(
    expected: list[object],
    actual: list[object],
    path: str,
    config: GoldenComparisonConfig,
) -> list[GoldenMismatch]:
    if len(expected) != len(actual):
        return [GoldenMismatch(path, len(expected), len(actual), "collection size differs")]

    edges = [
        [
            actual_index
            for actual_index, actual_item in enumerate(actual)
            if not _compare(expected_item, actual_item, f"{path}[*]", config)
        ]
        for expected_item in expected
    ]
    actual_to_expected: dict[int, int] = {}

    def augment(expected_index: int, visited: set[int]) -> bool:
        for actual_index in edges[expected_index]:
            if actual_index in visited:
                continue
            visited.add(actual_index)
            owner = actual_to_expected.get(actual_index)
            if owner is None or augment(owner, visited):
                actual_to_expected[actual_index] = expected_index
                return True
        return False

    unmatched_expected = [index for index in range(len(expected)) if not augment(index, set())]
    if not unmatched_expected:
        return []

    unmatched_actual = sorted(set(range(len(actual))) - set(actual_to_expected))
    mismatches: list[GoldenMismatch] = []
    for expected_index, actual_index in zip(unmatched_expected, unmatched_actual, strict=False):
        mismatches.extend(
            _compare(
                expected[expected_index],
                actual[actual_index],
                f"{path}[semantic:{expected_index}]",
                config,
            )
        )
    if not mismatches:
        mismatches.append(
            GoldenMismatch(
                path,
                [expected[index] for index in unmatched_expected],
                [actual[index] for index in unmatched_actual],
                "semantic collection members differ",
            )
        )
    return mismatches


def _is_number(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _numbers_close(
    expected: object, actual: object, path: str, tolerance: ToleranceProfile
) -> bool:
    assert isinstance(expected, Real)
    assert isinstance(actual, Real)
    leaf = _path_leaf(path)
    if leaf in _EXACT_NUMERIC_FIELDS or (
        isinstance(expected, Integral) and isinstance(actual, Integral)
    ):
        return expected == actual
    expected_float = float(expected)
    actual_float = float(actual)
    if not math.isfinite(expected_float) or not math.isfinite(actual_float):
        return expected_float == actual_float
    if "angle" in leaf or leaf.endswith("_deg"):
        return tolerance.angle_close_deg(expected_float, actual_float)
    if "area" in leaf or leaf.endswith("_mm2"):
        return tolerance.area_close(expected_float, actual_float)
    return tolerance.length_close(expected_float, actual_float)


def _path_leaf(path: str) -> str:
    return path.rsplit(".", maxsplit=1)[-1].split("[", maxsplit=1)[0].casefold()


__all__ = [
    "GoldenComparisonConfig",
    "GoldenComparisonResult",
    "GoldenMismatch",
    "compare_semantic_entities",
    "compare_takeoff_reports",
]
