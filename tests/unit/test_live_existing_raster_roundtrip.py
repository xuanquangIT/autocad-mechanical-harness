from __future__ import annotations

from pathlib import Path

import pytest
from scripts.live_existing_raster_roundtrip import (
    _assert_private_material_absent,
    _calibration,
    _line_image_bytes,
    _new_duplicate_ref,
    _proposed_candidate_id,
)

from cad_harness.comprehension.raster_trace import LocalRasterTracer


def test_acceptance_raster_is_deterministic_and_maps_to_exact_slot_flank(tmp_path: Path) -> None:
    first = _line_image_bytes()
    second = _line_image_bytes()
    assert first == second
    report = LocalRasterTracer(tmp_path).trace(
        first,
        display_name="slot-line-calibrated.png",
        calibration=_calibration(),
    )

    candidate_id = _proposed_candidate_id(report)

    assert candidate_id.startswith("raster-candidate-")
    assert len(report.candidates) == 1


def test_new_duplicate_ref_requires_one_new_audit_bound_entity() -> None:
    before = {"entities": [{"entity_ref": "existing"}]}
    after = {"entities": [{"entity_ref": "existing"}, {"entity_ref": "new"}]}
    audit = {
        "report": {
            "findings": [
                {"rule_id": "DUPLICATE_ENTITY", "entity_ref": "new"},
            ]
        }
    }

    assert _new_duplicate_ref(before, after, audit) == "new"


@pytest.mark.parametrize(
    "after,audit",
    [
        ({"entities": [{"entity_ref": "existing"}]}, {"report": {"findings": []}}),
        (
            {"entities": [{"entity_ref": "existing"}, {"entity_ref": "new"}]},
            {"report": {"findings": []}},
        ),
    ],
)
def test_new_duplicate_ref_rejects_missing_scope(
    after: dict[str, object], audit: dict[str, object]
) -> None:
    with pytest.raises(AssertionError):
        _new_duplicate_ref({"entities": [{"entity_ref": "existing"}]}, after, audit)


def test_private_material_scan_rejects_image_and_token(tmp_path: Path) -> None:
    payload = _line_image_bytes()
    token = "raster-v1.claims.signature"
    (tmp_path / "safe.txt").write_text("source hash only", encoding="utf-8")
    _assert_private_material_absent(tmp_path, image_payload=payload, acceptance_token=token)

    (tmp_path / "unsafe.bin").write_bytes(payload)
    with pytest.raises(AssertionError, match="persisted raw image"):
        _assert_private_material_absent(tmp_path, image_payload=payload, acceptance_token=token)
