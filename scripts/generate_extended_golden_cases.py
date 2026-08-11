"""Generate deterministic synthetic golden inputs and five DXF take-off fixtures.

These cases expand offline coverage; they are not a substitute for the engineer-selected
acceptance drawings required before a production release.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

import ezdxf

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "golden_drawings"
SOURCES = (
    "base_plate_160x100",
    "corner_notch_boundary",
    "corner_notch_normal",
    "edge_cutout_boundary",
    "edge_cutout_normal",
    "flange_boundary",
    "flange_normal",
    "keyway_boundary",
    "keyway_normal",
    "l_bracket_boundary",
    "l_bracket_normal",
    "linear_hole_pattern_boundary",
    "linear_hole_pattern_normal",
    "slot_boundary",
    "slot_normal",
)


def _payload(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return {key: item for key, item in value.items() if not key.startswith("_")}


def _scale(value: object, factor: float) -> object:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if key == "feature_id" and isinstance(item, str):
                result[key] = item
            elif (
                key.endswith("_mm") and isinstance(item, int | float) and not isinstance(item, bool)
            ):
                result[key] = round(float(item) * factor, 6)
            elif (
                key.endswith("_mm")
                and isinstance(item, list)
                and all(
                    isinstance(part, int | float) and not isinstance(part, bool) for part in item
                )
            ):
                result[key] = [round(float(part) * factor, 6) for part in item]
            else:
                result[key] = _scale(item, factor)
        return result
    if isinstance(value, list):
        return [_scale(item, factor) for item in value]
    return value


def _rename_features(value: object, suffix: str) -> None:
    if isinstance(value, dict):
        feature_id = value.get("feature_id")
        if isinstance(feature_id, str):
            value["feature_id"] = f"{feature_id}-{suffix}"
        for item in value.values():
            _rename_features(item, suffix)
    elif isinstance(value, list):
        for item in value:
            _rename_features(item, suffix)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _takeoff_fixture(case: Path, index: int) -> None:
    width = 100.0 + index * 15.0
    height = 70.0 + index * 10.0
    radius = 3.0 + index
    document = ezdxf.new("R2018")
    document.header["$INSUNITS"] = 4
    document.layers.add("CUT")
    document.layers.add("HOLE")
    model = document.modelspace()
    outline = model.add_lwpolyline(
        [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)],
        close=True,
        dxfattribs={"layer": "CUT"},
    )
    for x in (width * 0.25, width * 0.75):
        model.add_circle((x, height * 0.5), radius, dxfattribs={"layer": "HOLE"})
    document.saveas(case / "input_drawing.dxf")
    _write_json(
        case / "takeoff_request.json",
        {
            "schema_version": "1.12",
            "document_id": "replaced-by-golden-runner",
            "parts": [
                {
                    "part_code": f"GOLDEN-{index + 1:02d}",
                    "outline_entity_ref": str(outline.dxf.handle),
                    "thickness_mm": 8.0 + index,
                    "material_code": "SS400",
                    "quantity": index + 1,
                }
            ],
            "weld_edges": [],
            "material_profile_ref": "demo-materials@1.0",
        },
    )


def generate(*, force: bool) -> None:
    for index, source_name in enumerate(SOURCES):
        target = CASES / f"extended_{index + 1:02d}_{source_name}"
        if target.exists() and not force:
            raise FileExistsError(f"Refusing to overwrite {target.name}; pass --force")
        target.mkdir(parents=True, exist_ok=True)
        fixture_source = "linear_hole_pattern_normal" if index == 11 else source_name
        source = _payload(CASES / fixture_source / "input_spec.json")
        spec = copy.deepcopy(_scale(source, 1.15 + index * 0.035))
        assert isinstance(spec, dict)
        _rename_features(spec, f"g{index + 1:02d}")
        _write_json(target / "input_spec.json", spec)
        if index < 5:
            _takeoff_fixture(target, index)

    base = _payload(CASES / "base_plate_160x100" / "input_spec.json")
    for index, (name, child) in enumerate(
        (
            (
                "extended_16_rectangular_holes_dense",
                {
                    "feature_id": "dense-rect-holes",
                    "type": "rectangular_hole_pattern",
                    "parameters": {
                        "hole_diameter_mm": 8.0,
                        "edge_offset_x_mm": 20.0,
                        "edge_offset_y_mm": 20.0,
                        "count_x": 3,
                        "count_y": 3,
                    },
                },
            ),
            *(
                (
                    f"extended_{17 + offset:02d}_bolt_circle_{count}",
                    {
                        "feature_id": f"bolt-circle-{count}",
                        "type": "bolt_circle_pattern",
                        "parameters": {
                            "hole_diameter_mm": 10.0,
                            "pcd_mm": 60.0 + 10.0 * offset,
                            "count": count,
                            "center_mm": [80.0, 50.0],
                        },
                    },
                )
                for offset, count in enumerate((4, 6, 8))
            ),
        )
    ):
        target = CASES / name
        if target.exists() and not force:
            raise FileExistsError(f"Refusing to overwrite {target.name}; pass --force")
        target.mkdir(parents=True, exist_ok=True)
        spec = copy.deepcopy(base)
        spec["features"][0]["feature_id"] = f"special-plate-{index + 1}"
        spec["features"][0]["children"] = [child]
        _write_json(target / "input_spec.json", spec)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    generate(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
