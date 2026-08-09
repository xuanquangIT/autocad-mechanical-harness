"""Run or regenerate the golden drawing cases.

    uv run python scripts/run_golden_tests.py            # run them
    uv run python scripts/run_golden_tests.py --update   # regenerate expectations

``--update`` rewrites expected_plan.json, expected_semantic_entities.json and
expected_validation.json. Always review the diff: a changed plan hash means prior
approvals for that plan are void.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = ROOT / "tests" / "golden_drawings"


def _case_directories() -> list[Path]:
    positive = [
        path
        for path in CASES_DIR.iterdir()
        if path.is_dir() and not path.name.startswith(("_", "."))
    ]
    negative_root = CASES_DIR / "_negative"
    negative = [path for path in negative_root.iterdir() if path.is_dir()]
    return sorted((*positive, *negative), key=lambda path: path.name)


def _style_payload(geometry: dict[str, Any]) -> dict[str, str]:
    style: dict[str, str] = {}
    if "dimstyle" in geometry:
        style["dimension_style"] = str(geometry["dimstyle"])
    if "textstyle" in geometry:
        style["text_style"] = str(geometry["textstyle"])
    return style


def _semantic_entities(plan: Any, entity_results: Any) -> dict[str, Any]:
    operations = {operation.operation_id: operation for operation in plan.operations}
    entities: list[dict[str, Any]] = []
    for result in entity_results:
        operation = operations[result.operation_id]
        payload: dict[str, Any] = {
            "operation_id": result.operation_id,
            "feature_id": result.feature_id,
            "entity_type": result.entity_type,
            "layer": operation.layer,
            "measurements": result.measurements,
        }
        style = _style_payload(operation.geometry)
        if style:
            payload["style"] = style
        entities.append(payload)
    return {"entity_count": len(entities), "entities": entities}


def _strip_comments(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _preserve_comment(existing: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the human-written ``_comment`` when regenerating a file."""
    if existing.is_file():
        previous = json.loads(existing.read_text(encoding="utf-8"))
        if "_comment" in previous:
            return {"_comment": previous["_comment"], **payload}
    return payload


def regenerate(case: Path) -> None:
    from cad_harness.adapters.fake import FakeAutoCADAdapter
    from cad_harness.application.services.harness_service import HarnessService
    from cad_harness.config import Settings
    from cad_harness.domain.errors import HarnessError
    from cad_harness.domain.models.validation import ValidationStage

    spec = _strip_comments(json.loads((case / "input_spec.json").read_text(encoding="utf-8")))
    settings = Settings.model_validate(
        {
            "storage": {"preview_directory": str(ROOT / "data" / "previews")},
            "observability": {"log_level": "ERROR"},
        }
    )
    service = HarnessService(settings, FakeAutoCADAdapter())

    job = service.create_job()
    error_path = case / "expected_error.json"
    if error_path.is_file():
        expected_error = _strip_comments(json.loads(error_path.read_text(encoding="utf-8")))
        try:
            service.submit_spec(job.job_id, spec)
        except HarnessError as exc:
            if exc.code.value != expected_error["error_code"]:
                raise
            print(f"{case.name}: expected {exc.code.value}")
            return
        raise AssertionError(f"{case.name}: expected compilation to fail")

    profile_snapshot = case / "company_profile.yaml"
    if not profile_snapshot.is_file():
        profile_snapshot.write_text(
            "profile_ref: demo-profile@1.0\ncompany_approved: false\n",
            encoding="utf-8",
        )

    submitted = service.submit_spec(job.job_id, spec)
    if submitted.get("status") != "ok":
        print(f"{case.name}: spec incomplete: {submitted}", file=sys.stderr)
        return

    plan = service.store.get_plan(job.job_id)
    assert plan is not None
    plan_payload = {
        "canonical_units": plan.canonical_units.value,
        "profile_ref": plan.profile_ref,
        "operations": [
            op.model_dump(mode="json", exclude_none=True, exclude={"target_entity_ref"})
            for op in plan.operations
        ],
    }
    _write(case / "expected_plan.json", plan_payload)

    from cad_harness.preview.svg_writer import write_svg

    stable_preview_plan = plan.model_copy(update={"plan_id": f"golden:{case.name}"})
    write_svg(stable_preview_plan, case / "preview_reference.svg")

    service.preview(job.job_id)
    report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    _write(
        case / "expected_validation.json",
        {
            "stage": report.stage.value,
            "blocking_count": report.blocking_count,
            "error_count": report.error_count,
            "warning_count": report.warning_count,
            "commit_allowed": report.gate_allows_commit(),
            "findings": [
                {"rule_id": f.rule_id, "severity": f.severity.value} for f in report.findings
            ],
        },
    )

    if not report.gate_allows_commit():
        print(f"{case.name}: validation blocks commit; entities not regenerated")
        return

    _, token = service.approve(
        job.job_id, "golden-runner", tuple(f.rule_id for f in report.findings)
    )
    result = service.commit(
        job.job_id,
        idempotency_key=f"golden-{case.name}",
        expected_revision=job.expected_revision,
        plan_hash=str(submitted["plan_hash"]),
        approval_token=token,
    )
    _write(
        case / "expected_semantic_entities.json", _semantic_entities(plan, result.entity_results)
    )
    _regenerate_takeoff(case)
    print(f"{case.name}: regenerated (plan_hash={submitted['plan_hash']})")


def _regenerate_takeoff(case: Path) -> None:
    drawing = case / "input_drawing.dxf"
    request_path = case / "takeoff_request.json"
    if not drawing.is_file() and not request_path.is_file():
        return
    if not drawing.is_file() or not request_path.is_file():
        raise AssertionError(f"{case.name}: DXF takeoff requires both input files")

    from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
    from cad_harness.company_rules.material_loader import load_material_table
    from cad_harness.comprehension.takeoff import compute_takeoff
    from cad_harness.domain.models.drawing_model import ReadScope
    from cad_harness.domain.models.takeoff import TakeoffRequest
    from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef
    from cad_harness.geometry.tolerance import DEMO_TOLERANCE

    model = DxfDrawingReader(DEMO_TOLERANCE).read(
        DrawingReadRequest(
            source=DrawingSourceRef(kind="file", format="dxf", ref=str(drawing)),
            scope=ReadScope(kind="model_space"),
            max_entities=20_000,
            max_block_nesting_depth=10,
        )
    )
    raw_request = _strip_comments(json.loads(request_path.read_text(encoding="utf-8")))
    raw_request["document_id"] = model.document_id
    request = TakeoffRequest.model_validate(raw_request)
    materials = load_material_table(request.material_profile_ref)
    report = compute_takeoff(model, request, materials=materials, tolerance=DEMO_TOLERANCE)
    _write(case / "expected_takeoff.json", report.model_dump(mode="json", exclude_none=True))


def _write(target: Path, payload: dict[str, Any]) -> None:
    body = _preserve_comment(target, payload)
    target.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden drawing case runner")
    parser.add_argument("--update", action="store_true", help="Regenerate expectations")
    parser.add_argument("--case", default=None, help="Limit to a single case directory")
    args = parser.parse_args()

    cases = _case_directories()
    if args.case:
        cases = [c for c in cases if c.name == args.case]
        if not cases:
            print(f"No such case: {args.case}", file=sys.stderr)
            return 1

    if not args.update:
        env = {**os.environ, "CAD_HARNESS_APPROVAL_SECRET": "golden-runner-secret"}
        if args.case:
            env["CAD_HARNESS_GOLDEN_CASE"] = args.case
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-m", "golden", "-q"], cwd=ROOT, env=env, check=False
        ).returncode

    os.environ.setdefault("CAD_HARNESS_APPROVAL_SECRET", "golden-runner-secret")
    for case in cases:
        regenerate(case)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
