"""CLI: ``uv run cad-harness <command>``.

Also the engineer's minimum approval surface until the desktop app exists: ``approve``
issues a token bound to one plan hash and revision.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any

from apps.mcp_server.context import build_context
from cad_harness.domain.errors import (
    ApprovalRequiredError,
    ApprovalScopeMismatchError,
    HarnessError,
    UnsupportedInputFormatError,
)
from cad_harness.domain.models.raster import RasterTraceReport
from cad_harness.domain.models.validation import Severity, ValidationStage
from cad_harness.persistence.schema_migration import DatabaseSchemaError

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

    if service.adapter.status().adapter_type != "fake":
        _emit(
            {
                "status": "blocked",
                "reason": "live_cli_self_approval_disabled",
                "required_action": (
                    "Use Engineer Desktop to review and approve the exact live plan; "
                    "the CLI demo may commit only to the in-memory fake adapter."
                ),
            }
        )
        return 4

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
    """Safely initialize or upgrade the exact configured SQLite database."""
    from cad_harness.config import load_settings
    from cad_harness.persistence.schema_migration import upgrade_database

    settings = load_settings(args.config)
    revision = upgrade_database(Path(settings.storage.sqlite_path))
    _emit({"status": "ok", "database_revision": revision})
    return 0


def _cmd_raster_accept(args: argparse.Namespace) -> int:
    """Human-only acceptance surface for one exact calibrated raster trace."""
    if not args.confirm_reviewed_overlay:
        raise ApprovalRequiredError(
            "Raster candidates were not confirmed against the generated overlay",
            required_action="Review the overlay, then repeat with --confirm-reviewed-overlay",
        )
    context = build_context(args.config)
    service = context.raster_trace_service
    if service is None:
        raise ApprovalRequiredError(
            "Raster acceptance requires a local signing secret",
            required_action="Set CAD_HARNESS_APPROVAL_SECRET and retry locally",
        )
    report = RasterTraceReport.model_validate_json(args.report.read_text(encoding="utf-8"))
    overlay_path, overlay_sha256 = _resolve_raster_overlay(context, report)
    del overlay_path
    if not hmac.compare_digest(args.reviewed_overlay_sha256, overlay_sha256):
        raise ApprovalScopeMismatchError(
            "Reviewed overlay digest does not match the current trace",
            required_action="Run raster-review again and review that exact SVG before accepting",
            details={"expected_overlay_sha256": overlay_sha256},
        )
    acceptance, token = service.accept(
        report,
        tuple(args.candidate),
        args.accepted_by,
        layer=args.layer,
    )
    _emit(
        {
            "status": "ok",
            "acceptance": acceptance.model_dump(mode="json"),
            "acceptance_token": token,
            "warning": "Token expires in at most 15 minutes and covers only this exact trace.",
        }
    )
    return 0


def _resolve_raster_overlay(context: Any, report: RasterTraceReport) -> tuple[Path, str]:
    try:
        path = context.raster_tracer.resolve_overlay_path(report)
        payload = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise UnsupportedInputFormatError(
            "The trace overlay is missing or no longer bound to this report",
            required_action="Trace the source image again and review the newly generated overlay",
        ) from exc
    return path, f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _cmd_raster_review(args: argparse.Namespace) -> int:
    """Resolve one opaque overlay for explicit local human review."""
    context = build_context(args.config)
    report = RasterTraceReport.model_validate_json(args.report.read_text(encoding="utf-8"))
    overlay_path, overlay_sha256 = _resolve_raster_overlay(context, report)
    _emit(
        {
            "status": "review_required",
            "trace_id": report.trace_id,
            "source_sha256": report.source.source_sha256,
            "overlay_artifact_ref": report.overlay_artifact_ref,
            "overlay_path": str(overlay_path),
            "overlay_sha256": overlay_sha256,
            "candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "status": candidate.status.value,
                    "geometry_kind": candidate.geometry.kind,
                    "confidence": candidate.confidence,
                    "fit_error_px": candidate.fit_error_px,
                }
                for candidate in report.candidates
            ],
            "next_action": (
                "Open the local SVG, compare every selected candidate to the source, then run "
                "raster-accept with this exact overlay SHA-256"
            ),
        }
    )
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

    migrate = subparsers.add_parser(
        "migrate", help="Safely initialize or upgrade the configured SQLite database"
    )
    migrate.set_defaults(func=_cmd_migrate)

    raster_review = subparsers.add_parser(
        "raster-review",
        help="Resolve a trace overlay and print its hash-bound candidate review summary",
    )
    raster_review.add_argument("report", type=Path, help="RasterTraceReport JSON to review")
    raster_review.set_defaults(func=_cmd_raster_review)

    raster_accept = subparsers.add_parser(
        "raster-accept",
        help="Accept reviewed raster candidates and issue a short-lived local token",
    )
    raster_accept.add_argument("report", type=Path, help="RasterTraceReport JSON to review")
    raster_accept.add_argument(
        "--candidate",
        action="append",
        required=True,
        help="Proposed candidate id to accept; repeat for multiple candidates",
    )
    raster_accept.add_argument("--accepted-by", required=True, help="Engineer identity")
    raster_accept.add_argument(
        "--layer", required=True, help="Exact target layer approved for these candidates"
    )
    raster_accept.add_argument(
        "--confirm-reviewed-overlay",
        action="store_true",
        help="Confirm the local SVG overlay and exact candidates were reviewed",
    )
    raster_accept.add_argument(
        "--reviewed-overlay-sha256",
        required=True,
        help="Exact SHA-256 printed by raster-review for the reviewed SVG",
    )
    raster_accept.set_defaults(func=_cmd_raster_accept)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except HarnessError as error:
        _emit({"status": "error", "error": error.to_payload()})
        return 1
    except DatabaseSchemaError:
        _emit(
            {
                "status": "error",
                "error": {
                    "code": "DATABASE_SCHEMA_UNSAFE",
                    "message": "The configured database schema cannot be safely migrated.",
                    "retryable": False,
                    "required_action": (
                        "Back up the configured database, verify its release provenance, "
                        "and retry cad-harness migrate."
                    ),
                },
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
