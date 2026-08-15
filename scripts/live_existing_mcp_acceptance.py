"""Safely exercise duplicate-only MCP writes against an engineer-opened drawing.

The runner refuses before preview or commit unless every planned geometry/layer
signature already exists, then removes only audit-proven duplicate entities
through a separately approved remediation job and verifies exact restoration.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from apps.mcp_server.context import build_context
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from scripts.live_mcp_r26_acceptance import _call, _issue_live_approval, _load_spec, _safe_case_name
from scripts.live_session_preflight import issue_existing_live_session_proof


def _spec_without_annotations(path: Path) -> dict[str, Any]:
    spec = _load_spec(path)
    spec["annotations"] = {"dimensions": "none"}
    return spec


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _active_read_arguments(document_id: str) -> dict[str, Any]:
    return {
        "request": {
            "source": {
                "kind": "active_document",
                "format": "dwg",
                "ref": document_id,
            },
            "scope": {"kind": "model_space"},
            "max_entities": 10_000,
            "max_block_nesting_depth": 5,
            "include_geometry": True,
        }
    }


GeometrySignature = tuple[object, ...]


def _number(value: object) -> float:
    if not isinstance(value, int | float):
        raise AssertionError("Acceptance geometry contained a non-numeric coordinate")
    return round(float(value), 9)


def _point(value: object) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise AssertionError("Acceptance geometry contained an invalid point")
    return (_number(value[0]), _number(value[1]))


def _plan_geometry_signatures(config_path: Path, job_id: str) -> Counter[GeometrySignature]:
    context = build_context(config_path)
    plan = context.service.store.get_plan(job_id)
    if plan is None:
        raise AssertionError("MCP did not persist the acceptance plan")
    signatures: Counter[GeometrySignature] = Counter()
    for operation in plan.operations:
        geometry = operation.geometry
        layer = operation.layer
        operation_type = operation.type.value
        if operation_type == "create_circle":
            signatures[
                (
                    "circle",
                    layer,
                    _point(geometry.get("center_mm")),
                    _number(geometry.get("diameter_mm")) / 2.0,
                )
            ] += 1
        elif operation_type == "create_circles":
            centers = geometry.get("centers_mm")
            if not isinstance(centers, list):
                raise AssertionError("Circle array plan contained no centers")
            radius = _number(geometry.get("diameter_mm")) / 2.0
            for center in centers:
                signatures[("circle", layer, _point(center), radius)] += 1
        elif operation_type == "create_line":
            signatures[
                (
                    "line",
                    layer,
                    _point(geometry.get("start_mm")),
                    _point(geometry.get("end_mm")),
                )
            ] += 1
        elif operation_type == "create_arc":
            signatures[
                (
                    "arc",
                    layer,
                    _point(geometry.get("center_mm")),
                    _number(geometry.get("radius_mm")),
                    _number(geometry.get("start_angle_deg")) % 360.0,
                    _number(geometry.get("end_angle_deg")) % 360.0,
                )
            ] += 1
        elif operation_type in {"create_polyline", "create_closed_polyline"}:
            vertices = geometry.get("vertices_mm")
            if not isinstance(vertices, list):
                raise AssertionError("Polyline plan contained no vertices")
            signatures[
                (
                    "polyline",
                    layer,
                    tuple(_point(vertex) for vertex in vertices),
                    operation_type == "create_closed_polyline",
                )
            ] += 1
        else:
            raise AssertionError(
                f"Repeatable existing-document acceptance does not support {operation_type}"
            )
    return signatures


def _model_geometry_signatures(model: Mapping[str, Any]) -> Counter[GeometrySignature]:
    signatures: Counter[GeometrySignature] = Counter()
    entities = model.get("entities")
    if not isinstance(entities, list):
        raise AssertionError("Existing DrawingModel contained no entities")
    for entity in entities:
        if not isinstance(entity, Mapping) or not isinstance(entity.get("layer"), str):
            continue
        geometry = entity.get("geometry")
        if not isinstance(geometry, Mapping):
            continue
        layer = entity["layer"]
        kind = geometry.get("kind")
        if kind == "circle":
            signature: GeometrySignature = (
                "circle",
                layer,
                _point(geometry.get("center_mm")),
                _number(geometry.get("radius_mm")),
            )
        elif kind == "line":
            signature = (
                "line",
                layer,
                _point(geometry.get("start_mm")),
                _point(geometry.get("end_mm")),
            )
        elif kind == "arc":
            signature = (
                "arc",
                layer,
                _point(geometry.get("center_mm")),
                _number(geometry.get("radius_mm")),
                _number(geometry.get("start_angle_deg")) % 360.0,
                _number(geometry.get("end_angle_deg")) % 360.0,
            )
        elif kind == "polyline":
            vertices = geometry.get("vertices")
            if not isinstance(vertices, list):
                continue
            signature = (
                "polyline",
                layer,
                tuple(
                    _point(vertex.get("point_mm"))
                    for vertex in vertices
                    if isinstance(vertex, Mapping)
                ),
                geometry.get("closed") is True,
            )
        else:
            continue
        signatures[signature] += 1
    return signatures


async def _workflow_session(
    session: ClientSession,
    *,
    config_path: Path,
    spec: dict[str, Any],
    case_name: str,
) -> dict[str, Any]:
    listed = await session.list_tools()
    status = await _call(session, "cad_status", {})
    adapter = status["data"]["adapter"]
    if adapter.get("adapter_type") != "com" or adapter.get("available") is not True:
        raise AssertionError("MCP did not attach to the existing live COM document")
    before = await _call(session, "cad_document_inspect", {})
    document = before["data"]
    if document.get("read_only") is True:
        raise AssertionError("The engineer-opened drawing is read-only")
    before_read = await _call(
        session,
        "cad_drawing_read",
        _active_read_arguments(document["document_id"]),
    )
    before_model = before_read["data"]

    created = await _call(
        session,
        "cad_job_create",
        {"document_id": document["document_id"]},
    )
    job_id = created["data"]["job_id"]
    submitted = await _call(
        session,
        "cad_spec_submit",
        {"job_id": job_id, "spec": spec},
    )
    plan_hash = submitted["data"]["plan_hash"]
    planned_geometry = _plan_geometry_signatures(config_path, job_id)
    existing_geometry = _model_geometry_signatures(before_model)
    if planned_geometry - existing_geometry:
        raise AssertionError(
            "Repeatable existing-document acceptance only commits geometry already present"
        )
    preview = await _call(session, "cad_preview", {"job_id": job_id})
    validation = await _call(
        session,
        "cad_validate",
        {"job_id": job_id, "stage": "pre_commit"},
    )
    if validation["data"]["commit_allowed"] is not True:
        raise AssertionError("Pre-commit validation rejected the existing drawing")
    approval_id, approval_token = _issue_live_approval(
        config_path,
        job_id,
        plan_hash,
        document["revision"],
    )
    committed = await _call(
        session,
        "cad_commit",
        {
            "job_id": job_id,
            "idempotency_key": f"existing-{case_name}-{secrets.token_hex(8)}",
            "expected_revision": document["revision"],
            "plan_hash": plan_hash,
            "approval_token": approval_token,
        },
    )
    commit = committed["data"]
    entity_results = commit.get("entity_results")
    if commit.get("status") != "committed" or not isinstance(entity_results, list):
        raise AssertionError("COM commit returned no real entity evidence")
    created_refs = tuple(
        item.get("entity_ref")
        for item in entity_results
        if isinstance(item, dict) and isinstance(item.get("entity_ref"), str)
    )
    if len(created_refs) != len(entity_results) or len(set(created_refs)) != len(created_refs):
        raise AssertionError("COM commit returned incomplete or duplicate entity references")
    after = await _call(session, "cad_document_inspect", {})
    after_document = after["data"]
    if (
        after_document["revision"] == document["revision"]
        or after_document["entity_count"] <= document["entity_count"]
    ):
        raise AssertionError("Existing drawing did not change after the live COM commit")

    after_read = await _call(
        session,
        "cad_drawing_read",
        _active_read_arguments(document["document_id"]),
    )
    audited = await _call(session, "cad_audit", {"model": after_read["data"]})
    audit = audited["data"]
    audit_id = audit.get("audit_id")
    findings = audit.get("report", {}).get("findings", ())
    proven_duplicate_refs = {
        finding.get("entity_ref")
        for finding in findings
        if isinstance(finding, dict)
        and finding.get("rule_id") == "DUPLICATE_ENTITY"
        and finding.get("entity_ref") in created_refs
    }
    if not isinstance(audit_id, str) or proven_duplicate_refs != set(created_refs):
        raise AssertionError("Audit did not prove every created entity is a removable duplicate")

    cleanup_created = await _call(
        session,
        "cad_job_create",
        {"document_id": document["document_id"]},
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
                    {"rule_id": "DUPLICATE_ENTITY", "entity_ref": entity_ref}
                    for entity_ref in created_refs
                ],
                "technical_inputs": {},
            },
        },
    )
    cleanup_plan_hash = cleanup_submitted["data"]["plan_hash"]
    if cleanup_submitted["data"].get("operation_count") != len(created_refs):
        raise AssertionError("Cleanup did not compile one deletion per proven duplicate")
    cleanup_preview = await _call(session, "cad_preview", {"job_id": cleanup_job_id})
    cleanup_diff = cleanup_preview["data"].get("semantic_diff", {})
    if cleanup_diff.get("summary", {}).get("deleted") != len(created_refs):
        raise AssertionError("Cleanup preview did not contain every proven deletion")
    cleanup_validation = await _call(
        session,
        "cad_validate",
        {"job_id": cleanup_job_id, "stage": "pre_commit"},
    )
    if cleanup_validation["data"].get("commit_allowed") is not True:
        raise AssertionError("Pre-commit validation rejected duplicate cleanup")
    cleanup_approval_id, cleanup_token = _issue_live_approval(
        config_path,
        cleanup_job_id,
        cleanup_plan_hash,
        after_document["revision"],
    )
    cleanup_committed = await _call(
        session,
        "cad_commit",
        {
            "job_id": cleanup_job_id,
            "idempotency_key": f"existing-cleanup-{case_name}-{secrets.token_hex(8)}",
            "expected_revision": after_document["revision"],
            "plan_hash": cleanup_plan_hash,
            "approval_token": cleanup_token,
        },
    )
    cleanup_commit = cleanup_committed["data"]
    if cleanup_commit.get("status") != "committed" or len(
        cleanup_commit.get("entity_results", ())
    ) != len(created_refs):
        raise AssertionError("Duplicate cleanup did not commit every deletion")
    restored = await _call(session, "cad_document_inspect", {})
    restored_document = restored["data"]
    if (
        restored_document["document_id"] != document["document_id"]
        or restored_document["revision"] != document["revision"]
        or restored_document["entity_count"] != document["entity_count"]
    ):
        raise AssertionError("Existing drawing did not return to its exact pre-test state")

    return {
        "tool_count": len(listed.tools),
        "adapter": {
            "adapter_type": adapter["adapter_type"],
            "available": adapter["available"],
            "cad_application": adapter.get("cad_application"),
            "cad_version": adapter.get("cad_version"),
            "capabilities": adapter.get("capabilities", []),
            "process_id": adapter.get("process_id"),
            "version_supported": adapter.get("version_supported"),
        },
        "document": {
            "display_name_sha256": _digest_text(document["display_name"]),
            "document_id_sha256": _digest_text(document["document_id"]),
            "revision_before": document["revision"],
            "revision_after": after_document["revision"],
            "revision_restored": restored_document["revision"],
            "entity_count_before": document["entity_count"],
            "entity_count_after": after_document["entity_count"],
            "entity_count_restored": restored_document["entity_count"],
        },
        "job_id_sha256": _digest_text(job_id),
        "plan_hash": plan_hash,
        "operation_count": submitted["data"]["operation_count"],
        "preview_artifact_count": len(preview["data"]["artifacts"]),
        "validation": {
            "blocking_count": validation["data"]["blocking_count"],
            "error_count": validation["data"]["error_count"],
        },
        "approval_id_sha256": _digest_text(approval_id),
        "commit": {
            "status": commit["status"],
            "entity_count": len(entity_results),
            "new_revision": commit["new_revision"],
            "checkpoint_present": bool(commit.get("checkpoint_id")),
        },
        "cleanup": {
            "approval_id_sha256": _digest_text(cleanup_approval_id),
            "job_id_sha256": _digest_text(cleanup_job_id),
            "method": "audited_duplicate_remediation",
            "deleted_count": len(created_refs),
            "restored_revision": restored_document["revision"],
        },
        "drawing_restored": True,
    }


async def _workflow(
    *,
    config_path: Path,
    spec: dict[str, Any],
    case_name: str,
) -> dict[str, Any]:
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
            return await _workflow_session(
                session,
                config_path=config_path,
                spec=spec,
                case_name=case_name,
            )


def run_acceptance(
    *,
    config_path: Path,
    spec_path: Path,
    case_name: str,
    work_root: Path,
    evidence_path: Path,
) -> dict[str, Any]:
    case_name = _safe_case_name(case_name)
    config_path = config_path.resolve(strict=True)
    spec = _spec_without_annotations(spec_path)
    case_root = work_root.resolve() / case_name
    case_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ["CAD_HARNESS_CONFIG"] = str(config_path)
    os.environ["CAD_HARNESS_ADAPTER"] = "com"
    signing_secret = secrets.token_urlsafe(48)
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

    workflow = asyncio.run(
        _workflow(
            config_path=config_path,
            spec=spec,
            case_name=case_name,
        )
    )
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "real_autocad_evidence": True,
        "production_evidence": False,
        "attached_existing_document": True,
        "mcp_transport": "stdio",
        "input_spec_sha256": _canonical_sha256(spec),
        "workflow": workflow,
    }
    evidence_path = evidence_path.resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--case-name", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    result = run_acceptance(
        config_path=args.config,
        spec_path=args.spec,
        case_name=args.case_name,
        work_root=args.work_root,
        evidence_path=args.evidence,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "real_autocad_evidence": True,
                "adapter_type": result["workflow"]["adapter"]["adapter_type"],
                "drawing_restored": result["workflow"]["drawing_restored"],
                "entity_count_restored": result["workflow"]["document"]["entity_count_restored"],
                "revision_restored": result["workflow"]["document"]["revision_restored"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
