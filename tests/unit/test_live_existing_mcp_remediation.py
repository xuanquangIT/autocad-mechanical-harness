from __future__ import annotations

import pytest
from scripts.live_existing_mcp_remediation import (
    _duplicate_baseplate_ref,
    _overlap_finding,
    _redundant_bore_ref,
)


def _entity(
    entity_ref: str,
    kind: str,
    *,
    center: list[float] | None = None,
    radius: float | None = None,
    bounds: list[float] | None = None,
    closed: bool | None = None,
) -> dict[str, object]:
    geometry: dict[str, object] = {"kind": kind}
    if center is not None:
        geometry["center_mm"] = center
    if radius is not None:
        geometry["radius_mm"] = radius
    if closed is not None:
        geometry["closed"] = closed
    result: dict[str, object] = {"entity_ref": entity_ref, "geometry": geometry}
    if bounds is not None:
        result["bounding_box_mm"] = bounds
    return result


def test_redundant_bore_requires_one_exact_observed_circle_and_finding() -> None:
    model = {
        "entities": [
            _entity("bore", "circle", center=[350.0, 100.0], radius=40.0),
            _entity("other", "circle", center=[350.0, 100.0], radius=8.0),
        ]
    }
    evidence = {
        "report": {
            "findings": [
                {"rule_id": "OVERLAPPING_ENTITY", "entity_ref": "bore"},
            ]
        }
    }

    assert _redundant_bore_ref(model) == "bore"
    assert _overlap_finding(evidence, "bore")["entity_ref"] == "bore"


def test_redundant_bore_rejects_ambiguous_geometry() -> None:
    circle = _entity("bore-a", "circle", center=[350.0, 100.0], radius=40.0)
    with pytest.raises(AssertionError, match="exactly one"):
        _redundant_bore_ref({"entities": [circle, {**circle, "entity_ref": "bore-b"}]})


def test_duplicate_baseplate_ref_is_bound_to_audit_and_exact_bounds() -> None:
    model = {
        "entities": [
            _entity(
                "duplicate",
                "polyline",
                closed=True,
                bounds=[0.0, 0.0, 160.0, 100.0],
            ),
            _entity(
                "different",
                "polyline",
                closed=True,
                bounds=[0.0, 0.0, 80.0, 50.0],
            ),
        ]
    }
    evidence = {
        "report": {
            "findings": [
                {"rule_id": "DUPLICATE_ENTITY", "entity_ref": "duplicate"},
                {"rule_id": "DUPLICATE_ENTITY", "entity_ref": "different"},
            ]
        }
    }

    assert _duplicate_baseplate_ref(evidence, model) == "duplicate"


def test_duplicate_baseplate_ref_rejects_multiple_exact_candidates() -> None:
    entities = [
        _entity(
            entity_ref,
            "polyline",
            closed=True,
            bounds=[0.0, 0.0, 160.0, 100.0],
        )
        for entity_ref in ("duplicate-a", "duplicate-b")
    ]
    findings = [
        {"rule_id": "DUPLICATE_ENTITY", "entity_ref": entity["entity_ref"]} for entity in entities
    ]
    with pytest.raises(AssertionError, match="exactly one"):
        _duplicate_baseplate_ref({"report": {"findings": findings}}, {"entities": entities})
