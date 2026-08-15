from __future__ import annotations

import pytest
from scripts.live_existing_bridge_read_acceptance import _classify_contours


def _entity(entity_ref: str, kind: str, *, closed: bool | None = None) -> dict[str, object]:
    geometry: dict[str, object] = {"kind": kind}
    if closed is not None:
        geometry["closed"] = closed
    return {"entity_ref": entity_ref, "geometry": geometry}


def test_existing_bridge_acceptance_selects_one_outline_and_four_holes() -> None:
    model = {
        "entities": [
            _entity("outline", "polyline", closed=True),
            _entity("hole-4", "circle"),
            _entity("hole-2", "circle"),
            _entity("hole-3", "circle"),
            _entity("hole-1", "circle"),
        ]
    }

    assert _classify_contours(model) == (
        "outline",
        ["hole-1", "hole-2", "hole-3", "hole-4"],
    )


@pytest.mark.parametrize(
    "entities",
    [
        [_entity("outline", "polyline", closed=True)],
        [
            _entity("outline-a", "polyline", closed=True),
            _entity("outline-b", "polyline", closed=True),
            *[_entity(f"hole-{index}", "circle") for index in range(4)],
        ],
    ],
)
def test_existing_bridge_acceptance_rejects_ambiguous_geometry(
    entities: list[dict[str, object]],
) -> None:
    with pytest.raises(AssertionError):
        _classify_contours({"entities": entities})
