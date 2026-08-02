"""Canonical JSON and plan hashing.

These tests protect the property everything else relies on: the same plan always
produces the same hash, and a meaningful change always changes it.
"""

from __future__ import annotations

import pytest

from cad_harness.domain.canonical import (
    canonical_json,
    compute_plan_hash,
    hash_prefix,
    normalize,
    sha256_of,
    strip_volatile,
)


class TestCanonicalJson:
    def test_key_order_does_not_affect_output(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_no_insignificant_whitespace(self) -> None:
        assert canonical_json({"a": [1, 2]}) == '{"a":[1,2]}'

    def test_negative_zero_collapses(self) -> None:
        assert canonical_json({"x": -0.0}) == canonical_json({"x": 0.0})

    def test_array_order_is_preserved(self) -> None:
        """Vertex order is geometrically meaningful, so it must survive hashing."""
        assert canonical_json([1, 2]) != canonical_json([2, 1])

    def test_float_precision_is_bounded(self) -> None:
        assert canonical_json({"x": 1.0000000001}) == canonical_json({"x": 1.0})

    def test_meaningful_precision_is_retained(self) -> None:
        assert canonical_json({"x": 20.0001}) != canonical_json({"x": 20.0})

    def test_non_finite_floats_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="Non-finite"):
            canonical_json({"x": float("nan")})

    def test_unsupported_types_are_rejected(self) -> None:
        with pytest.raises(TypeError):
            normalize({"x": object()})


class TestPlanHash:
    def test_hash_ignores_volatile_fields(self) -> None:
        base = {"operations": [{"id": "op-1"}], "profile_ref": "demo@1.0"}
        with_volatile = {
            **base,
            "plan_hash": "sha256:whatever",
            "created_at": "2026-08-02T00:00:00Z",
            "request_id": "req_1",
        }
        assert compute_plan_hash(base) == compute_plan_hash(with_volatile)

    def test_hash_changes_when_geometry_changes(self) -> None:
        first = {"operations": [{"geometry": {"diameter_mm": 14.0}}]}
        second = {"operations": [{"geometry": {"diameter_mm": 14.5}}]}
        assert compute_plan_hash(first) != compute_plan_hash(second)

    def test_hash_changes_when_profile_version_changes(self) -> None:
        """A profile bump must invalidate prior approvals, so it must change the hash."""
        assert compute_plan_hash({"profile_ref": "demo@1.0"}) != compute_plan_hash(
            {"profile_ref": "demo@2.0"}
        )

    def test_strip_volatile_is_recursive(self) -> None:
        stripped = strip_volatile({"a": {"created_at": 1, "keep": 2}, "b": [{"request_id": 3}]})
        assert stripped == {"a": {"keep": 2}, "b": [{}]}

    def test_hash_is_prefixed_and_stable(self) -> None:
        digest = sha256_of({"a": 1})
        assert digest.startswith("sha256:")
        assert digest == sha256_of({"a": 1})
        assert len(hash_prefix(digest)) == 12
