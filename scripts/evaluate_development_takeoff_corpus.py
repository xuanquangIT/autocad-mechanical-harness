"""Run five generated DXF take-offs against an independent analytic oracle.

This is development evidence only.  The script creates disposable, parametric
DXF files, reads them through the production DXF adapter, runs the production
take-off engine, and compares the result with formulas implemented here.  It
does not claim engineer review or company approval and never touches AutoCAD.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final, NoReturn

import ezdxf

from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
from cad_harness.company_rules.material_loader import load_material_table
from cad_harness.comprehension.takeoff import compute_takeoff
from cad_harness.domain.canonical import canonical_json, sha256_of
from cad_harness.domain.models.drawing_model import ReadScope
from cad_harness.domain.models.takeoff import MaterialTable, PartInput, TakeoffRequest
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef
from cad_harness.geometry.tolerance import ToleranceProfile

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT: Final = REPOSITORY_ROOT / "data"
OUTPUT_SCHEMA_VERSION: Final = "1.0"
EVALUATOR_VERSION: Final = "development-takeoff-evaluator-v2"
MATERIAL_PROFILE_REF: Final = "demo-materials@1.0"
MATERIAL_CODE: Final = "AL6061"
REFERENCE_DENSITY_KG_PER_M3: Final = Decimal("2700")
MATERIAL_REFERENCE_URL: Final = (
    "https://ntrs.nasa.gov/api/citations/19740014418/downloads/19740014418.pdf"
)
MATERIAL_REFERENCE_LOCATION: Final = "Table II, page 43: density 2700 kg/m3"
MM3_PER_M3: Final = Decimal("1000000000")
ABSOLUTE_GEOMETRY_TOLERANCE: Final = 1.0e-7
ABSOLUTE_MASS_TOLERANCE: Final = 1.0e-12


class DevelopmentTakeoffEvaluationError(ValueError):
    """Fail-closed error with a stable, path-free code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise DevelopmentTakeoffEvaluationError(code)


@dataclass(frozen=True, slots=True)
class _CircleCutout:
    center_mm: tuple[float, float]
    radius_mm: float


@dataclass(frozen=True, slots=True)
class _RectangleCutout:
    origin_mm: tuple[float, float]
    width_mm: float
    height_mm: float


@dataclass(frozen=True, slots=True)
class _Case:
    case_id: str
    width_mm: float
    height_mm: float
    thickness_mm: float
    quantity: int
    circles: tuple[_CircleCutout, ...] = ()
    rectangles: tuple[_RectangleCutout, ...] = ()


CASES: Final = (
    _Case("plate-plain", 100.0, 50.0, 10.0, 1),
    _Case(
        "plate-one-round-hole",
        120.0,
        80.0,
        12.0,
        2,
        circles=(_CircleCutout((60.0, 40.0), 10.0),),
    ),
    _Case(
        "plate-two-round-holes",
        200.0,
        100.0,
        8.0,
        3,
        circles=(
            _CircleCutout((60.0, 50.0), 5.0),
            _CircleCutout((140.0, 50.0), 5.0),
        ),
    ),
    _Case(
        "plate-rectangular-cutout",
        150.0,
        60.0,
        6.0,
        4,
        rectangles=(_RectangleCutout((60.0, 20.0), 30.0, 20.0),),
    ),
    _Case(
        "plate-mixed-cutouts",
        180.0,
        120.0,
        16.0,
        2,
        circles=(
            _CircleCutout((45.0, 60.0), 8.0),
            _CircleCutout((135.0, 60.0), 12.0),
        ),
        rectangles=(_RectangleCutout((80.0, 45.0), 20.0, 30.0),),
    ),
)


def _rounded_kg(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _close(actual: float, expected: float, tolerance: float, code: str) -> None:
    if not math.isfinite(actual) or not math.isclose(
        actual, expected, rel_tol=0.0, abs_tol=tolerance
    ):
        _fail(code)


def _analytic(case: _Case) -> dict[str, object]:
    circle_area = sum(math.pi * item.radius_mm**2 for item in case.circles)
    rectangle_area = sum(item.width_mm * item.height_mm for item in case.rectangles)
    net_area = case.width_mm * case.height_mm - circle_area - rectangle_area
    outer_length = 2.0 * (case.width_mm + case.height_mm)
    inner_length = sum(2.0 * math.pi * item.radius_mm for item in case.circles) + sum(
        2.0 * (item.width_mm + item.height_mm) for item in case.rectangles
    )
    unit_mass_raw = (
        Decimal(str(net_area))
        * Decimal(str(case.thickness_mm))
        * REFERENCE_DENSITY_KG_PER_M3
        / MM3_PER_M3
    )
    total_mass_raw = unit_mass_raw * Decimal(case.quantity)
    diameter_counts: dict[float, int] = {}
    for circle in case.circles:
        diameter = circle.radius_mm * 2.0
        diameter_counts[diameter] = diameter_counts.get(diameter, 0) + 1
    return {
        "net_area_mm2": net_area,
        "outer_cut_length_mm": outer_length,
        "inner_cut_length_mm": inner_length,
        "cut_length_mm": outer_length + inner_length,
        "pierce_count": 1 + len(case.circles) + len(case.rectangles),
        "unit_mass_kg_raw": float(unit_mass_raw),
        "unit_mass_kg": _rounded_kg(unit_mass_raw),
        "total_mass_kg_raw": float(total_mass_raw),
        "total_mass_kg": _rounded_kg(total_mass_raw),
        "hole_groups": tuple(sorted(diameter_counts.items())),
    }


def _load_development_material_table() -> MaterialTable:
    """Seam kept explicit so density drift is testable without replacing production code."""
    return load_material_table(MATERIAL_PROFILE_REF)


def _write_case_dxf(case: _Case, target: Path) -> tuple[str, tuple[str, ...]]:
    document = ezdxf.new("R2010", setup=False)
    document.header["$INSUNITS"] = 4  # millimetres
    document.layers.add("OBJECT")
    document.layers.add("CUTOUT")
    modelspace = document.modelspace()
    outline = modelspace.add_lwpolyline(
        (
            (0.0, 0.0),
            (case.width_mm, 0.0),
            (case.width_mm, case.height_mm),
            (0.0, case.height_mm),
        ),
        close=True,
        dxfattribs={"layer": "OBJECT"},
    )
    inner_handles: list[str] = []
    for circle in case.circles:
        circle_entity = modelspace.add_circle(
            circle.center_mm,
            circle.radius_mm,
            dxfattribs={"layer": "CUTOUT"},
        )
        inner_handles.append(str(circle_entity.dxf.handle))
    for rectangle in case.rectangles:
        x, y = rectangle.origin_mm
        rectangle_entity = modelspace.add_lwpolyline(
            (
                (x, y),
                (x + rectangle.width_mm, y),
                (x + rectangle.width_mm, y + rectangle.height_mm),
                (x, y + rectangle.height_mm),
            ),
            close=True,
            dxfattribs={"layer": "CUTOUT"},
        )
        inner_handles.append(str(rectangle_entity.dxf.handle))
    document.saveas(target)
    return str(outline.dxf.handle), tuple(inner_handles)


def _evaluate_case(case: _Case, root: Path) -> dict[str, object]:
    path = root / f"{case.case_id}.dxf"
    outline_ref, inner_refs = _write_case_dxf(case, path)
    request = DrawingReadRequest(
        source=DrawingSourceRef(kind="file", format="dxf", ref=str(path)),
        scope=ReadScope(kind="model_space"),
        max_entities=100,
        max_block_nesting_depth=2,
        include_geometry=True,
    )
    reader = DxfDrawingReader()
    model = reader.read(request)
    if not model.coverage_complete or not model.geometry_normalized:
        _fail("DXF_READ_INCOMPLETE")
    takeoff_request = TakeoffRequest(
        document_id=model.document_id,
        parts=(
            PartInput(
                part_code=case.case_id.upper(),
                outline_entity_ref=outline_ref,
                thickness_mm=case.thickness_mm,
                material_code=MATERIAL_CODE,
                quantity=case.quantity,
                inner_contour_entity_refs=inner_refs,
            ),
        ),
        material_profile_ref=MATERIAL_PROFILE_REF,
    )
    materials = _load_development_material_table()
    material = next(
        (entry for entry in materials.entries if entry.material_code == MATERIAL_CODE), None
    )
    if material is None or Decimal(str(material.density_kg_per_m3)) != REFERENCE_DENSITY_KG_PER_M3:
        _fail("DEVELOPMENT_MATERIAL_DENSITY_DRIFT")
    if materials.company_approved:
        _fail("DEVELOPMENT_PROFILE_MUST_NOT_BE_APPROVED")
    tolerance = ToleranceProfile(id="development-takeoff", version="1.0")
    first = compute_takeoff(model, takeoff_request, materials=materials, tolerance=tolerance)
    second = compute_takeoff(model, takeoff_request, materials=materials, tolerance=tolerance)
    if first != second:
        _fail("TAKEOFF_NOT_DETERMINISTIC")
    if len(first.parts) != 1 or first.excluded_contours:
        _fail("TAKEOFF_RESULT_INCOMPLETE")
    line = first.parts[0]
    expected = _analytic(case)
    for field in (
        "net_area_mm2",
        "outer_cut_length_mm",
        "inner_cut_length_mm",
        "cut_length_mm",
    ):
        expected_value = expected[field]
        if not isinstance(expected_value, int | float):
            _fail("INTERNAL_ORACLE_TYPE_ERROR")
        _close(
            float(getattr(line, field)),
            float(expected_value),
            ABSOLUTE_GEOMETRY_TOLERANCE,
            f"ORACLE_MISMATCH_{field.upper()}",
        )
    for field in ("unit_mass_kg_raw", "total_mass_kg_raw"):
        expected_value = expected[field]
        if not isinstance(expected_value, int | float):
            _fail("INTERNAL_ORACLE_TYPE_ERROR")
        _close(
            float(getattr(line, field)),
            float(expected_value),
            ABSOLUTE_MASS_TOLERANCE,
            f"ORACLE_MISMATCH_{field.upper()}",
        )
    for field in ("pierce_count", "unit_mass_kg", "total_mass_kg"):
        if getattr(line, field) != expected[field]:
            _fail(f"ORACLE_MISMATCH_{field.upper()}")
    actual_groups = tuple((group.diameter_mm, group.count) for group in line.hole_groups)
    if actual_groups != expected["hole_groups"]:
        _fail("ORACLE_MISMATCH_HOLE_GROUPS")
    return {
        "case_id": case.case_id,
        "source_kind": "generated_parametric_dxf",
        "input_entity_count": len(model.entities),
        "inner_contour_count": len(inner_refs),
        "oracle_match": True,
        "deterministic_repeat": True,
        "metrics": {
            field: expected[field]
            for field in (
                "net_area_mm2",
                "cut_length_mm",
                "pierce_count",
                "unit_mass_kg_raw",
                "total_mass_kg_raw",
            )
        },
    }


def evaluate_development_takeoff_corpus() -> dict[str, object]:
    """Evaluate the closed five-case development corpus."""
    if len(CASES) < 5:
        _fail("INSUFFICIENT_DEVELOPMENT_CASES")
    with tempfile.TemporaryDirectory(prefix="cad-harness-takeoff-") as directory:
        cases = [_evaluate_case(case, Path(directory)) for case in CASES]
    identity = {
        "evaluator_version": EVALUATOR_VERSION,
        "case_definitions": [
            {
                "case_id": case.case_id,
                "width_mm": case.width_mm,
                "height_mm": case.height_mm,
                "thickness_mm": case.thickness_mm,
                "quantity": case.quantity,
                "circles": [[item.center_mm, item.radius_mm] for item in case.circles],
                "rectangles": [
                    [item.origin_mm, item.width_mm, item.height_mm] for item in case.rectangles
                ],
            }
            for case in CASES
        ],
        "material_profile_ref": MATERIAL_PROFILE_REF,
        "reference_density_kg_per_m3": str(REFERENCE_DENSITY_KG_PER_M3),
    }
    report: dict[str, object] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "report_kind": "development_takeoff_corpus_evaluation",
        "evaluation_id": sha256_of(identity),
        "evaluator_version": EVALUATOR_VERSION,
        "production_evidence": False,
        "production_acceptance_eligible": False,
        "engineer_selected": False,
        "independently_human_reviewed": False,
        "company_approved": False,
        "customer_data_used": False,
        "production_dxf_reader_exercised": True,
        "production_takeoff_engine_exercised": True,
        "independent_formula_oracle": True,
        "material_reference": {
            "profile_ref": MATERIAL_PROFILE_REF,
            "material_code": MATERIAL_CODE,
            "density_kg_per_m3": float(REFERENCE_DENSITY_KG_PER_M3),
            "classification": "development_demo_only",
            "company_approved": False,
            "public_reference_url": MATERIAL_REFERENCE_URL,
            "public_reference_location": MATERIAL_REFERENCE_LOCATION,
            "public_reference_is_company_approval": False,
        },
        "summary": {
            "case_count": len(cases),
            "oracle_match_count": sum(bool(case["oracle_match"]) for case in cases),
            "deterministic_repeat_count": sum(bool(case["deterministic_repeat"]) for case in cases),
        },
        "limitations": [
            "generated_cases_are_not_engineer_selected_drawings",
            "analytic_oracle_is_software_evidence_not_independent_human_review",
            "demo_material_table_is_not_company_approved_or_pricing_data",
            "offline_dxf_evaluation_does_not_exercise_autocad_or_mcp",
        ],
        "cases": cases,
    }
    normalized = json.loads(canonical_json(report))
    if not isinstance(normalized, dict):
        _fail("INTERNAL_REPORT_TYPE_ERROR")
    return normalized


def render_evaluation(report: Mapping[str, object]) -> str:
    return canonical_json(dict(report)) + "\n"


def _write_once(target: Path, payload: str, *, output_root: Path) -> None:
    try:
        root = output_root.resolve(strict=True)
        if not root.is_dir() or target.suffix.casefold() != ".json":
            _fail("OUTPUT_PATH_NOT_ALLOWED")
        candidate = (target if target.is_absolute() else root / target).resolve(strict=False)
        candidate.parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        _fail("OUTPUT_PATH_NOT_ALLOWED")
    try:
        with candidate.open("x", encoding="utf-8", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        _fail("OUTPUT_ALREADY_EXISTS")
    except OSError:
        _fail("OUTPUT_WRITE_FAILED")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    try:
        if (args.output is None) != (args.output_root is None):
            _fail("OUTPUT_ALLOWLIST_REQUIRED")
        rendered = render_evaluation(evaluate_development_takeoff_corpus())
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            _write_once(args.output, rendered, output_root=args.output_root)
    except DevelopmentTakeoffEvaluationError as error:
        sys.stderr.write(
            json.dumps(
                {"error": {"code": error.code}, "production_evidence": False},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
