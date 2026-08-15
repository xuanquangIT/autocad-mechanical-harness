"""Trace one deterministic image into the open drawing, then audit-delete it."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import cv2
import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from cad_harness.application.services.raster_trace_service import RasterTraceService
from cad_harness.comprehension.raster_trace import LocalRasterTracer
from cad_harness.domain.canonical import canonical_json, sha256_of
from cad_harness.domain.models.raster import (
    PixelPoint,
    RasterCalibration,
    RasterTraceReport,
)
from cad_harness.security.execution_receipt import (
    ExecutionReceiptClaims,
    execution_public_key,
    execution_public_key_sha256,
    issue_execution_receipt,
)
from scripts.live_existing_mcp_remediation import _active_read_arguments
from scripts.live_mcp_r26_acceptance import _call, _issue_live_approval, _safe_case_name
from scripts.live_session_preflight import issue_existing_live_session_proof

_LAYER = "OBJECT"
_DISPLAY_NAME = "slot-line-calibrated.png"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AssertionError(f"{label} did not return an object")
    return cast(Mapping[str, Any], value)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _write_execution_artifact(path: Path, value: object) -> str:
    payload = _canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _line_image_bytes() -> bytes:
    image = np.full((220, 220), 255, dtype=np.uint8)
    cv2.line(image, (20, 60), (180, 60), 0, 2, cv2.LINE_8)
    encoded, payload = cv2.imencode(".png", image)
    if not encoded:
        raise AssertionError("OpenCV could not encode the deterministic acceptance raster")
    return payload.tobytes()


def _calibration() -> RasterCalibration:
    # OpenCV observes the two-pixel stroke at x=19..181. This explicit calibration
    # maps that observed evidence to the existing slot flank (322,-38)..(378,-38).
    return RasterCalibration(
        pixel_a=PixelPoint(x=19.0, y=60.0),
        pixel_b=PixelPoint(x=181.0, y=60.0),
        reference_distance_mm=56.0,
        origin_mm=(322.0, -38.0),
    )


def _proposed_candidate_id(report: RasterTraceReport) -> str:
    proposed = tuple(
        candidate.candidate_id for candidate in report.candidates if candidate.status == "proposed"
    )
    if len(proposed) != 1:
        raise AssertionError("Expected exactly one proposed line candidate")
    geometry = next(
        candidate.geometry
        for candidate in report.candidates
        if candidate.candidate_id == proposed[0]
    )
    payload = geometry.model_dump(mode="json")
    if payload != {
        "kind": "line",
        "start_mm": [322.0, -38.0],
        "end_mm": [378.0, -38.0],
    }:
        raise AssertionError("Calibrated raster did not produce the exact reviewed slot flank")
    return proposed[0]


def _new_duplicate_ref(
    before_model: Mapping[str, Any],
    after_model: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> str:
    before_refs = {
        entity.get("entity_ref")
        for entity in before_model.get("entities", ())
        if isinstance(entity, Mapping)
    }
    new_refs = {
        entity.get("entity_ref")
        for entity in after_model.get("entities", ())
        if isinstance(entity, Mapping) and entity.get("entity_ref") not in before_refs
    }
    if len(new_refs) != 1:
        raise AssertionError("Raster commit did not add exactly one observable entity")
    new_ref = next(iter(new_refs))
    if not isinstance(new_ref, str):
        raise AssertionError("Raster commit returned no stable entity reference")
    report = _mapping(audit.get("report"), "drawing audit report")
    matches = [
        finding
        for finding in report.get("findings", ())
        if isinstance(finding, Mapping)
        and finding.get("rule_id") == "DUPLICATE_ENTITY"
        and finding.get("entity_ref") == new_ref
    ]
    if len(matches) != 1:
        raise AssertionError("Audit did not bind the new raster entity to one duplicate finding")
    return new_ref


def _assert_private_material_absent(
    case_root: Path,
    *,
    image_payload: bytes,
    acceptance_token: str,
) -> None:
    encoded_image = base64.b64encode(image_payload)
    encoded_token = acceptance_token.encode("utf-8")
    for path in case_root.rglob("*"):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if image_payload in payload or encoded_image in payload or encoded_token in payload:
            raise AssertionError("Live raster evidence persisted raw image or acceptance material")


async def _workflow(
    *,
    config_path: Path,
    case_root: Path,
    signing_secret: str,
    image_payload: bytes,
    execution_private_key: str | None,
    execution_signer_id: str,
) -> tuple[dict[str, Any], str]:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.mcp_server"],
        cwd=str(Path.cwd()),
        env=dict(os.environ),
        encoding="utf-8",
        encoding_error_handler="strict",
    )
    async with stdio_client(server) as (reader, writer):  # noqa: SIM117
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            status = await _call(session, "cad_status", {})
            adapter = _mapping(status["data"]["adapter"], "adapter status")
            if adapter.get("adapter_type") != "com" or adapter.get("available") is not True:
                raise AssertionError("MCP did not attach to the existing live COM document")
            process_id = adapter.get("process_id")
            valid_process_id = (
                isinstance(process_id, int) and not isinstance(process_id, bool) and process_id > 0
            )
            if execution_private_key is not None and not valid_process_id:
                raise AssertionError("Live COM status did not expose its exact process id")

            inspected = await _call(session, "cad_document_inspect", {})
            before = _mapping(inspected["data"], "document inspection")
            document_id = before.get("document_id")
            revision = before.get("revision")
            entity_count = before.get("entity_count")
            if not isinstance(document_id, str) or not isinstance(revision, str):
                raise AssertionError("Live document inspection returned incomplete provenance")
            if not isinstance(entity_count, int):
                raise AssertionError("Live document inspection returned no entity count")
            read = await _call(session, "cad_drawing_read", _active_read_arguments(document_id))
            before_model = _mapping(read["data"], "pre-raster drawing model")

            traced = await _call(
                session,
                "cad_image_trace",
                {
                    "image_base64": base64.b64encode(image_payload).decode("ascii"),
                    "display_name": _DISPLAY_NAME,
                    "calibration": _calibration().model_dump(mode="json"),
                },
            )
            report = RasterTraceReport.model_validate(traced["data"])
            traced_again = await _call(
                session,
                "cad_image_trace",
                {
                    "image_base64": base64.b64encode(image_payload).decode("ascii"),
                    "display_name": _DISPLAY_NAME,
                    "calibration": _calibration().model_dump(mode="json"),
                },
            )
            repeated_report = RasterTraceReport.model_validate(traced_again["data"])
            if repeated_report.model_dump(mode="json") != report.model_dump(mode="json"):
                raise AssertionError("Repeated live raster trace was not deterministic")
            candidate_id = _proposed_candidate_id(report)
            acceptance_service = RasterTraceService(
                LocalRasterTracer(case_root / "human-review"),
                signing_secret=signing_secret,
            )
            acceptance, acceptance_token = acceptance_service.accept(
                report,
                (candidate_id,),
                "user-authorized-live-test",
                layer=_LAYER,
            )
            drafted = await _call(
                session,
                "cad_image_draft",
                {
                    "document_id": document_id,
                    "report": report.model_dump(mode="json"),
                    "acceptance": acceptance.model_dump(mode="json"),
                    "acceptance_token": acceptance_token,
                    "layer": _LAYER,
                },
            )
            spec = _mapping(drafted["data"], "raster draft spec")

            created = await _call(session, "cad_job_create", {"document_id": document_id})
            raster_job_id = created["data"]["job_id"]
            submitted = await _call(
                session,
                "cad_spec_submit",
                {"job_id": raster_job_id, "spec": dict(spec)},
            )
            if submitted["data"].get("operation_count") != 1:
                raise AssertionError("Raster draft did not compile exactly one operation")
            raster_plan_hash = submitted["data"]["plan_hash"]
            previewed = await _call(session, "cad_preview", {"job_id": raster_job_id})
            preview_diff = _mapping(previewed["data"]["semantic_diff"], "raster preview diff")
            if _mapping(preview_diff["summary"], "raster preview summary").get("added") != 1:
                raise AssertionError("Raster preview did not show exactly one addition")
            validated = await _call(
                session,
                "cad_validate",
                {"job_id": raster_job_id, "stage": "pre_commit"},
            )
            if validated["data"].get("commit_allowed") is not True:
                raise AssertionError("Pre-commit validation rejected the accepted raster draft")
            _, approval_token = _issue_live_approval(
                config_path,
                raster_job_id,
                raster_plan_hash,
                revision,
            )
            committed = await _call(
                session,
                "cad_commit",
                {
                    "job_id": raster_job_id,
                    "idempotency_key": f"existing-raster-{secrets.token_hex(12)}",
                    "expected_revision": revision,
                    "plan_hash": raster_plan_hash,
                    "approval_token": approval_token,
                },
            )
            raster_commit = _mapping(committed["data"], "raster commit")
            if raster_commit.get("status") != "committed":
                raise AssertionError("Accepted raster draft did not commit")

            after_read = await _call(
                session,
                "cad_drawing_read",
                _active_read_arguments(document_id),
            )
            after_model = _mapping(after_read["data"], "post-raster drawing model")
            audited = await _call(session, "cad_audit", {"model": dict(after_model)})
            audit = _mapping(audited["data"], "post-raster audit")
            new_ref = _new_duplicate_ref(before_model, after_model, audit)
            new_entity = next(
                (
                    entity
                    for entity in after_model.get("entities", ())
                    if isinstance(entity, Mapping) and entity.get("entity_ref") == new_ref
                ),
                None,
            )
            if not isinstance(new_entity, Mapping):
                raise AssertionError("Post-raster model did not contain the created entity")
            new_geometry = _mapping(new_entity.get("geometry"), "created raster geometry")
            start = new_geometry.get("start_mm")
            end = new_geometry.get("end_mm")
            if (
                new_geometry.get("kind") != "line"
                or not isinstance(start, list)
                or not isinstance(end, list)
                or len(start) != 2
                or len(end) != 2
                or any(not isinstance(value, int | float) for value in [*start, *end])
            ):
                raise AssertionError("Created raster line did not return exact measured geometry")
            start_x, start_y = float(start[0]), float(start[1])
            end_x, end_y = float(end[0]), float(end[1])
            measured = {
                "start": [start_x, start_y],
                "end": [end_x, end_y],
                "midpoint": [(start_x + end_x) / 2.0, (start_y + end_y) / 2.0],
                "length": ((end_x - start_x) ** 2 + (end_y - start_y) ** 2) ** 0.5,
                "start_x": start_x,
                "start_y": start_y,
                "end_x": end_x,
                "end_y": end_y,
            }
            execution_evidence: dict[str, Any] | None = None
            if execution_private_key is not None:
                candidate = next(
                    item for item in report.candidates if item.candidate_id == candidate_id
                )
                candidate_geometry = _mapping(
                    candidate.geometry.model_dump(mode="json"), "candidate geometry"
                )
                candidate_payload = {
                    "schema_version": "1.0",
                    "source_sha256": report.source.source_sha256,
                    "candidates": [
                        {
                            "candidate_ref": candidate_id,
                            "geometry_kind": "line",
                            "geometry": {
                                "start": candidate_geometry["start_mm"],
                                "end": candidate_geometry["end_mm"],
                            },
                        }
                    ],
                }
                candidate_digest = hashlib.sha256(
                    canonical_json(candidate_payload).encode()
                ).hexdigest()
                candidate_set_sha256 = f"sha256:{candidate_digest}"
                trace_payload = {
                    "source_sha256": report.source.source_sha256,
                    "deterministic_runs": 2,
                    "detected_types": ["line"],
                }
                acceptance_payload = {
                    "source_sha256": acceptance.source_sha256,
                    "engineer_id": acceptance.accepted_by,
                    "evidence_ref": "live-existing-document-human-review",
                    "candidate_set_sha256": candidate_set_sha256,
                    "accepted_candidate_count": 1,
                    "accepted_candidate_refs": [candidate_id],
                }
                evidence_root = case_root / "execution-evidence"
                candidate_sha = _write_execution_artifact(
                    evidence_root / "candidate-geometry.json", candidate_payload
                )
                trace_sha = _write_execution_artifact(evidence_root / "trace.json", trace_payload)
                acceptance_sha = _write_execution_artifact(
                    evidence_root / "acceptance.json", acceptance_payload
                )
                validation_sha = sha256_of(validated["data"])
                readback_payload = {
                    "source_sha256": report.source.source_sha256,
                    "acceptance_sha256": acceptance_sha,
                    "trace_sha256": trace_sha,
                    "candidate_set_sha256": candidate_set_sha256,
                    "accepted_candidate_refs": [candidate_id],
                    "job_id": raster_job_id,
                    "plan_hash": raster_plan_hash,
                    "adapter_type": "com",
                    "process_id": process_id,
                    "document_id": document_id,
                    "autocad_version": adapter.get("cad_version"),
                    "pre_revision": revision,
                    "post_revision": after_model.get("revision"),
                    "measured_geometry": [
                        {
                            "candidate_ref": candidate_id,
                            "entity_ref": new_ref,
                            "geometry_kind": "line",
                            "unit": "mm",
                            "measurements": measured,
                        }
                    ],
                    "validation_report_sha256": validation_sha,
                    "validation_passed": True,
                }
                readback_sha = _write_execution_artifact(
                    evidence_root / "live-readback.json", readback_payload
                )
                claims = ExecutionReceiptClaims(
                    adapter_type="com",
                    process_id=cast(int, process_id),
                    document_id=document_id,
                    pre_revision=revision,
                    post_revision=cast(str, after_model.get("revision")),
                    plan_hash=raster_plan_hash,
                    job_id=raster_job_id,
                    validation_report_sha256=validation_sha,
                    result_sha256=readback_sha,
                )
                receipt = issue_execution_receipt(
                    claims,
                    signer_id=execution_signer_id,
                    private_key=execution_private_key,
                    issued_at=datetime.now(UTC),
                )
                receipt_sha = _write_execution_artifact(
                    evidence_root / "execution-receipt.json", receipt.to_external_dict()
                )
                public_key = execution_public_key(execution_private_key)
                execution_evidence = {
                    "candidate_geometry_sha256": candidate_sha,
                    "trace_sha256": trace_sha,
                    "acceptance_sha256": acceptance_sha,
                    "readback_sha256": readback_sha,
                    "execution_receipt_sha256": receipt_sha,
                    "execution_public_key": public_key,
                    "execution_public_key_sha256": execution_public_key_sha256(public_key),
                    "signer_id": execution_signer_id,
                    "artifacts_relative_to_case_root": {
                        "candidate_geometry": "execution-evidence/candidate-geometry.json",
                        "trace": "execution-evidence/trace.json",
                        "acceptance": "execution-evidence/acceptance.json",
                        "live_readback": "execution-evidence/live-readback.json",
                        "execution_receipt": "execution-evidence/execution-receipt.json",
                    },
                }
            audit_id = audit.get("audit_id")
            if not isinstance(audit_id, str):
                raise AssertionError("Post-raster audit did not persist an audit id")

            cleanup_created = await _call(
                session,
                "cad_job_create",
                {"document_id": document_id},
            )
            cleanup_job_id = cleanup_created["data"]["job_id"]
            cleanup_submitted = await _call(
                session,
                "cad_change_submit",
                {
                    "job_id": cleanup_job_id,
                    "remediation": {
                        "audit_id": audit_id,
                        "selected_findings": [
                            {"rule_id": "DUPLICATE_ENTITY", "entity_ref": new_ref}
                        ],
                        "technical_inputs": {},
                    },
                },
            )
            if cleanup_submitted["data"].get("operation_count") != 1:
                raise AssertionError("Raster cleanup did not compile exactly one deletion")
            cleanup_plan_hash = cleanup_submitted["data"]["plan_hash"]
            cleanup_preview = await _call(
                session,
                "cad_preview",
                {"job_id": cleanup_job_id},
            )
            cleanup_diff = _mapping(
                cleanup_preview["data"]["semantic_diff"], "cleanup preview diff"
            )
            if _mapping(cleanup_diff["summary"], "cleanup summary").get("deleted") != 1:
                raise AssertionError("Raster cleanup preview did not show one deletion")
            cleanup_validation = await _call(
                session,
                "cad_validate",
                {"job_id": cleanup_job_id, "stage": "pre_commit"},
            )
            if cleanup_validation["data"].get("commit_allowed") is not True:
                raise AssertionError("Pre-commit validation rejected raster cleanup")
            current_revision = after_model.get("revision")
            if not isinstance(current_revision, str):
                raise AssertionError("Post-raster model has no revision")
            _, cleanup_approval = _issue_live_approval(
                config_path,
                cleanup_job_id,
                cleanup_plan_hash,
                current_revision,
            )
            cleaned = await _call(
                session,
                "cad_commit",
                {
                    "job_id": cleanup_job_id,
                    "idempotency_key": f"existing-raster-cleanup-{secrets.token_hex(12)}",
                    "expected_revision": current_revision,
                    "plan_hash": cleanup_plan_hash,
                    "approval_token": cleanup_approval,
                },
            )
            cleanup_commit = _mapping(cleaned["data"], "cleanup commit")
            if cleanup_commit.get("status") != "committed":
                raise AssertionError("Raster cleanup did not commit")

            final_inspection = await _call(session, "cad_document_inspect", {})
            final = _mapping(final_inspection["data"], "final document inspection")
            final_read = await _call(
                session,
                "cad_drawing_read",
                _active_read_arguments(document_id),
            )
            final_model = _mapping(final_read["data"], "final drawing model")
            final_refs = {
                entity.get("entity_ref")
                for entity in final_model.get("entities", ())
                if isinstance(entity, Mapping)
            }
            if (
                final.get("entity_count") != entity_count
                or final.get("revision") != revision
                or new_ref in final_refs
            ):
                raise AssertionError("Raster round trip did not restore the existing drawing")

            return (
                {
                    "adapter_type": adapter.get("adapter_type"),
                    "cad_version": adapter.get("cad_version"),
                    "document_id_sha256": _digest(document_id),
                    "display_name_sha256": _digest(str(before.get("display_name", ""))),
                    "entity_count_before": entity_count,
                    "entity_count_after_raster": len(after_model.get("entities", ())),
                    "entity_count_final": final.get("entity_count"),
                    "revision_before": revision,
                    "revision_after_raster": current_revision,
                    "revision_final": final.get("revision"),
                    "trace_digest": report.trace_digest,
                    "source_sha256": report.source.source_sha256,
                    "candidate_id_sha256": _digest(candidate_id),
                    "candidate_count": len(report.candidates),
                    "accepted_candidate_count": 1,
                    "raster_plan_hash": raster_plan_hash,
                    "raster_operation_count": 1,
                    "raster_preview_added": 1,
                    "raster_commit_status": raster_commit.get("status"),
                    "new_entity_ref_sha256": _digest(new_ref),
                    "cleanup_rule": "DUPLICATE_ENTITY",
                    "cleanup_plan_hash": cleanup_plan_hash,
                    "cleanup_operation_count": 1,
                    "cleanup_preview_deleted": 1,
                    "cleanup_commit_status": cleanup_commit.get("status"),
                    "revision_restored": final.get("revision") == revision,
                    "execution_evidence": execution_evidence,
                },
                acceptance_token,
            )


def run_acceptance(
    *,
    config_path: Path,
    case_name: str,
    work_root: Path,
    evidence_path: Path,
    execution_private_key: str | None = None,
    execution_signer_id: str = "development-live-runner",
) -> dict[str, Any]:
    case_name = _safe_case_name(case_name)
    config_path = config_path.resolve(strict=True)
    case_root = work_root.resolve() / case_name
    case_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    signing_secret = secrets.token_urlsafe(48)
    os.environ["CAD_HARNESS_CONFIG"] = str(config_path)
    os.environ["CAD_HARNESS_ADAPTER"] = "com"
    os.environ["CAD_HARNESS_READ_SEMANTIC_ADAPTER"] = "auto"
    os.environ["CAD_HARNESS_APPROVAL_SECRET"] = signing_secret
    os.environ["CAD_HARNESS_LIVE_WRITE_VERIFIED"] = "1"
    os.environ["CAD_HARNESS_SQLITE_PATH"] = str(case_root / "harness.db")
    os.environ["CAD_HARNESS_PREVIEW_DIR"] = str(case_root / "previews")
    os.environ["CAD_HARNESS_CHECKPOINT_DIR"] = str(case_root / "checkpoints")
    os.environ["CAD_HARNESS_LOG_LEVEL"] = "ERROR"
    os.environ.pop("CAD_HARNESS_BRIDGE_PIPE_NAME_TEMPLATE", None)
    os.environ.pop("CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS", None)
    os.environ["CAD_HARNESS_LIVE_SESSION_PROOF"] = issue_existing_live_session_proof(
        config_path=config_path,
        adapter_type="com",
        secret=signing_secret,
    )

    image_payload = _line_image_bytes()
    workflow, acceptance_token = asyncio.run(
        _workflow(
            config_path=config_path,
            case_root=case_root,
            signing_secret=signing_secret,
            image_payload=image_payload,
            execution_private_key=execution_private_key,
            execution_signer_id=execution_signer_id,
        )
    )
    _assert_private_material_absent(
        case_root,
        image_payload=image_payload,
        acceptance_token=acceptance_token,
    )
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "real_autocad_evidence": True,
        "production_evidence": False,
        "attached_existing_document": True,
        "opened_or_created_autocad_document": False,
        "drawing_restored_after_test": True,
        "image_generated_for_development_test": True,
        "human_authorization": "explicit_user_authorized_live_test",
        "raw_image_or_acceptance_token_persisted": False,
        "mcp_transport": "stdio",
        "workflow": workflow,
    }
    evidence_path = evidence_path.resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--case-name", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--execution-private-key-env",
        help="optional issuer-only environment variable containing a raw Ed25519 private key",
    )
    parser.add_argument("--execution-signer-id", default="development-live-runner")
    args = parser.parse_args()
    execution_private_key = (
        os.environ.get(args.execution_private_key_env)
        if args.execution_private_key_env is not None
        else None
    )
    if args.execution_private_key_env is not None and not execution_private_key:
        raise SystemExit("Execution receipt private key is unavailable")
    result = run_acceptance(
        config_path=args.config,
        case_name=args.case_name,
        work_root=args.work_root,
        evidence_path=args.evidence,
        execution_private_key=execution_private_key,
        execution_signer_id=args.execution_signer_id,
    )
    workflow = result["workflow"]
    print(
        json.dumps(
            {
                "ok": True,
                "adapter_type": workflow["adapter_type"],
                "drawing_restored": result["drawing_restored_after_test"],
                "entity_count_final": workflow["entity_count_final"],
                "revision_final": workflow["revision_final"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
