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
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = ROOT / "tests" / "golden_drawings"


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

    service.preview(job.job_id)
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
        case / "expected_semantic_entities.json",
        {
            "entity_count": len(result.entity_results),
            "entities": [
                {
                    "operation_id": e.operation_id,
                    "feature_id": e.feature_id,
                    "entity_type": e.entity_type,
                    "measurements": e.measurements,
                }
                for e in result.entity_results
            ],
        },
    )
    print(f"{case.name}: regenerated (plan_hash={submitted['plan_hash']})")


def _write(target: Path, payload: dict[str, Any]) -> None:
    body = _preserve_comment(target, payload)
    target.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden drawing case runner")
    parser.add_argument("--update", action="store_true", help="Regenerate expectations")
    parser.add_argument("--case", default=None, help="Limit to a single case directory")
    args = parser.parse_args()

    cases = [
        path
        for path in sorted(CASES_DIR.iterdir())
        if path.is_dir() and not path.name.startswith(("_", "."))
    ]
    if args.case:
        cases = [c for c in cases if c.name == args.case]
        if not cases:
            print(f"No such case: {args.case}", file=sys.stderr)
            return 1

    if not args.update:
        import os

        env = {**os.environ, "CAD_HARNESS_APPROVAL_SECRET": "golden-runner-secret"}
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-m", "golden", "-q"], cwd=ROOT, env=env, check=False
        ).returncode

    import os

    os.environ.setdefault("CAD_HARNESS_APPROVAL_SECRET", "golden-runner-secret")
    for case in cases:
        regenerate(case)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
