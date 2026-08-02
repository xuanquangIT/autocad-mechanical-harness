"""CLI: ``uv run cad-harness <command>``.

Also the engineer's minimum approval surface until the desktop app exists: ``approve``
issues a token bound to one plan hash and revision.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from apps.mcp_server.context import build_context
from cad_harness.domain.errors import HarnessError
from cad_harness.domain.models.validation import Severity, ValidationStage

#: The pilot case study from architecture section 32.
DEMO_SPEC: dict[str, Any] = {
    "units": "mm",
    "drawing": {
        "projection": "orthographic",
        "view": "top",
        "datum": {"type": "point", "point_mm": [0.0, 0.0]},
    },
    "features": [
        {
            "feature_id": "base-plate-001",
            "type": "rectangular_plate",
            "parameters": {
                "width_mm": 160.0,
                "height_mm": 100.0,
                "thickness_mm": 12.0,
                "material": "SS400",
                "origin_mm": [0.0, 0.0],
            },
            "children": [
                {
                    "feature_id": "base-plate-001-holes",
                    "type": "rectangular_hole_pattern",
                    "parameters": {
                        "hole_diameter_mm": 14.0,
                        "edge_offset_x_mm": 20.0,
                        "edge_offset_y_mm": 20.0,
                        "count_x": 2,
                        "count_y": 2,
                    },
                }
            ],
        }
    ],
    "annotations": {"general_tolerance": "ISO 2768-m", "dimensions": "auto_required"},
}


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _cmd_status(args: argparse.Namespace) -> int:
    context = build_context(args.config)
    _emit(context.service.status())
    return 0


def _cmd_features(args: argparse.Namespace) -> int:
    context = build_context(args.config)
    _emit(context.service.search_features(args.query))
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    """Run the reference case end to end against the configured adapter.

    With the default ``fake`` adapter this exercises the whole pipeline, including the
    approval gate, without any CAD installed.
    """
    context = build_context(args.config)
    service = context.service

    job = service.create_job()
    print(f"job:        {job.job_id}", file=sys.stderr)
    print(f"revision:   {job.expected_revision}", file=sys.stderr)

    submitted = service.submit_spec(job.job_id, DEMO_SPEC)
    if submitted.get("status") == "needs_input":
        _emit(submitted)
        return 2
    print(f"plan_hash:  {submitted['plan_hash']}", file=sys.stderr)

    preview = service.preview(job.job_id)
    for artifact in preview["artifacts"]:
        print(f"artifact:   {artifact['kind']} -> {artifact['artifact_ref']}", file=sys.stderr)

    report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    print(
        f"validation: blocking={report.blocking_count} errors={report.error_count} "
        f"warnings={report.warning_count}",
        file=sys.stderr,
    )
    for entry in report.findings:
        print(f"  [{entry.severity.value}] {entry.rule_id}: {entry.message}", file=sys.stderr)

    if not report.gate_allows_commit():
        _emit(
            {
                "status": "blocked",
                "findings": [f.model_dump(mode="json") for f in report.findings],
            }
        )
        return 3

    if not args.commit:
        _emit(
            {
                "status": "previewed",
                "job_id": job.job_id,
                "plan_hash": submitted["plan_hash"],
                "note": (
                    "Pass --commit to approve and commit. Requires CAD_HARNESS_APPROVAL_SECRET."
                ),
            }
        )
        return 0

    acknowledged = tuple(f.rule_id for f in report.findings if f.severity is Severity.WARNING)
    approval_id, token = service.approve(job.job_id, args.approved_by, acknowledged)
    print(f"approval:   {approval_id} by {args.approved_by}", file=sys.stderr)

    result = service.commit(
        job.job_id,
        idempotency_key=f"demo-{job.job_id}",
        expected_revision=job.expected_revision,
        plan_hash=str(submitted["plan_hash"]),
        approval_token=token,
    )
    _emit(result.model_dump(mode="json"))
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    """Create the SQLite schema directly. Pilot machines should use Alembic instead."""
    from cad_harness.config import load_settings
    from cad_harness.persistence.engine import build_engine, create_all

    settings = load_settings(args.config)
    engine = build_engine(Path(settings.storage.sqlite_path))
    create_all(engine)
    _emit({"status": "ok", "sqlite_path": str(settings.storage.sqlite_path)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cad-harness", description="AutoCAD Mechanical Harness CLI"
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to a config YAML file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Show adapter, profile and capabilities")
    status.set_defaults(func=_cmd_status)

    features = subparsers.add_parser("features", help="List or search catalog features")
    features.add_argument("query", nargs="?", default="")
    features.set_defaults(func=_cmd_features)

    demo = subparsers.add_parser("demo", help="Run the reference base-plate case end to end")
    demo.add_argument("--commit", action="store_true", help="Approve and commit the plan")
    demo.add_argument("--approved-by", default="cli-user", help="Approver identity to record")
    demo.set_defaults(func=_cmd_demo)

    migrate = subparsers.add_parser("migrate", help="Create the SQLite schema (dev only)")
    migrate.set_defaults(func=_cmd_migrate)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except HarnessError as error:
        _emit({"status": "error", "error": error.to_payload()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
