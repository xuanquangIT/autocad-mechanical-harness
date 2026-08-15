"""Remove one audit-proven overlapping bore from the already-open AutoCAD drawing."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from scripts.live_mcp_r26_acceptance import _call, _issue_live_approval, _safe_case_name
from scripts.live_session_preflight import issue_existing_live_session_proof


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AssertionError(f"{label} did not return an object")
    return cast(Mapping[str, Any], value)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _active_read_arguments(document_id: str) -> dict[str, Any]:
    return {
        "request": {
            "source": {"kind": "active_document", "format": "dwg", "ref": document_id},
            "scope": {"kind": "model_space"},
            "max_entities": 10_000,
            "max_block_nesting_depth": 5,
            "include_geometry": True,
        }
    }


def _redundant_bore_ref(model: Mapping[str, Any]) -> str:
    matches: list[str] = []
    for raw_entity in model.get("entities", ()):
        entity = _mapping(raw_entity, "drawing entity")
        geometry = _mapping(entity.get("geometry"), "drawing geometry")
        center = geometry.get("center_mm")
        radius = geometry.get("radius_mm")
        entity_ref = entity.get("entity_ref")
        if (
            geometry.get("kind") == "circle"
            and isinstance(center, list)
            and len(center) == 2
            and isinstance(radius, int | float)
            and isinstance(entity_ref, str)
            and abs(float(center[0]) - 350.0) <= 0.01
            and abs(float(center[1]) - 100.0) <= 0.01
            and abs(float(radius) - 40.0) <= 0.01
        ):
            matches.append(entity_ref)
    if len(matches) != 1:
        raise AssertionError("Expected exactly one redundant radius-40 bore circle")
    return matches[0]


def _overlap_finding(evidence: Mapping[str, Any], entity_ref: str) -> Mapping[str, Any]:
    report = _mapping(evidence.get("report"), "audit report")
    matches = [
        _mapping(item, "audit finding")
        for item in report.get("findings", ())
        if isinstance(item, Mapping)
        and item.get("rule_id") == "OVERLAPPING_ENTITY"
        and item.get("entity_ref") == entity_ref
    ]
    if len(matches) != 1:
        raise AssertionError("Audit did not prove exactly one overlap for the redundant bore")
    return matches[0]


def _duplicate_baseplate_ref(evidence: Mapping[str, Any], model: Mapping[str, Any]) -> str:
    entities: dict[str, Mapping[str, Any]] = {}
    for raw_entity in model.get("entities", ()):
        entity = _mapping(raw_entity, "drawing entity")
        entity_ref = entity.get("entity_ref")
        if isinstance(entity_ref, str):
            entities[entity_ref] = entity
    report = _mapping(evidence.get("report"), "audit report")
    matches: list[str] = []
    for raw_finding in report.get("findings", ()):
        finding = _mapping(raw_finding, "audit finding")
        entity_ref = finding.get("entity_ref")
        if finding.get("rule_id") != "DUPLICATE_ENTITY" or not isinstance(entity_ref, str):
            continue
        candidate = entities.get(entity_ref)
        if candidate is None:
            continue
        geometry = _mapping(candidate.get("geometry"), "duplicate geometry")
        bounds = candidate.get("bounding_box_mm")
        if (
            geometry.get("kind") == "polyline"
            and geometry.get("closed") is True
            and isinstance(bounds, list)
            and len(bounds) == 4
            and all(
                abs(float(actual) - expected) <= 0.01
                for actual, expected in zip(bounds, (0.0, 0.0, 160.0, 100.0), strict=True)
            )
        ):
            matches.append(entity_ref)
    if len(matches) != 1:
        raise AssertionError("Audit did not identify exactly one temporary duplicate outline")
    return matches[0]


async def _workflow(
    config_path: Path,
    target: Literal["overlapping-bore", "duplicate-baseplate"],
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
            status = await _call(session, "cad_status", {})
            adapter = _mapping(status["data"]["adapter"], "adapter status")
            if adapter.get("adapter_type") != "com" or adapter.get("available") is not True:
                raise AssertionError("MCP did not attach to the existing live COM document")

            inspected = await _call(session, "cad_document_inspect", {})
            before = _mapping(inspected["data"], "document inspection")
            document_id = before.get("document_id")
            revision = before.get("revision")
            display_name = before.get("display_name")
            entity_count = before.get("entity_count")
            if not all(isinstance(value, str) for value in (document_id, revision, display_name)):
                raise AssertionError("Live document inspection returned incomplete provenance")
            if not isinstance(entity_count, int):
                raise AssertionError("Live document inspection returned no entity count")
            assert isinstance(document_id, str)
            assert isinstance(revision, str)
            assert isinstance(display_name, str)

            read = await _call(session, "cad_drawing_read", _active_read_arguments(document_id))
            model = _mapping(read["data"], "drawing model")
            if model.get("document_id") != document_id or model.get("revision") != revision:
                raise AssertionError("COM semantic model was not pinned to the inspected revision")
            if model.get("geometry_normalized") is not True:
                raise AssertionError("COM semantic geometry was not normalized to millimetres")
            audited = await _call(session, "cad_audit", {"model": dict(model)})
            audit = _mapping(audited["data"], "drawing audit evidence")
            audit_id = audit.get("audit_id")
            if not isinstance(audit_id, str):
                raise AssertionError("Drawing audit did not persist an audit_id")
            if target == "overlapping-bore":
                selected_ref = _redundant_bore_ref(model)
                selected_rule = "OVERLAPPING_ENTITY"
                _overlap_finding(audit, selected_ref)
                technical_inputs = {
                    f"{selected_rule}:{selected_ref}": {"strategy": "delete_selected"}
                }
            else:
                selected_ref = _duplicate_baseplate_ref(audit, model)
                selected_rule = "DUPLICATE_ENTITY"
                technical_inputs = {}

            created = await _call(session, "cad_job_create", {"document_id": document_id})
            job_id = created["data"]["job_id"]
            submitted = await _call(
                session,
                "cad_change_submit",
                {
                    "job_id": job_id,
                    "remediation": {
                        "audit_id": audit_id,
                        "selected_findings": [
                            {"rule_id": selected_rule, "entity_ref": selected_ref}
                        ],
                        "technical_inputs": technical_inputs,
                    },
                },
            )
            plan_hash = submitted["data"]["plan_hash"]
            if submitted["data"].get("operation_count") != 1:
                raise AssertionError("Overlap remediation did not compile exactly one delete")

            previewed = await _call(session, "cad_preview", {"job_id": job_id})
            diff = _mapping(previewed["data"].get("semantic_diff"), "semantic diff")
            summary = _mapping(diff.get("summary"), "semantic diff summary")
            entries = diff.get("entries")
            if summary.get("deleted") != 1 or not isinstance(entries, list):
                raise AssertionError("Preview did not show exactly one deletion")
            if not any(
                isinstance(entry, Mapping)
                and entry.get("change") == "deleted"
                and entry.get("target_entity_ref") == selected_ref
                for entry in entries
            ):
                raise AssertionError("Preview did not bind the deletion to the audited bore")

            validated = await _call(
                session, "cad_validate", {"job_id": job_id, "stage": "pre_commit"}
            )
            validation = _mapping(validated["data"], "validation")
            if validation.get("commit_allowed") is not True:
                raise AssertionError("Pre-commit validation rejected the overlap remediation")

            approval_id, approval_token = _issue_live_approval(
                config_path, job_id, plan_hash, revision
            )
            committed = await _call(
                session,
                "cad_commit",
                {
                    "job_id": job_id,
                    "idempotency_key": f"existing-overlap-{secrets.token_hex(12)}",
                    "expected_revision": revision,
                    "plan_hash": plan_hash,
                    "approval_token": approval_token,
                },
            )
            commit = _mapping(committed["data"], "commit result")
            if commit.get("status") != "committed":
                raise AssertionError("Live COM remediation did not commit")

            after_inspection = await _call(session, "cad_document_inspect", {})
            after = _mapping(after_inspection["data"], "post-commit inspection")
            if after.get("entity_count") != entity_count - 1 or after.get("revision") == revision:
                raise AssertionError("Live overlap remediation did not remove exactly one entity")

            reread = await _call(session, "cad_drawing_read", _active_read_arguments(document_id))
            after_model = _mapping(reread["data"], "post-commit drawing model")
            remaining_refs = {
                entity.get("entity_ref")
                for entity in after_model.get("entities", ())
                if isinstance(entity, Mapping)
            }
            if selected_ref in remaining_refs:
                raise AssertionError("The audited entity still exists after remediation commit")
            reaudited = await _call(session, "cad_audit", {"model": dict(after_model)})
            after_audit = _mapping(reaudited["data"], "post-commit audit")
            after_report = _mapping(after_audit.get("report"), "post-commit audit report")
            if any(
                isinstance(finding, Mapping)
                and finding.get("rule_id") == selected_rule
                and finding.get("entity_ref") == selected_ref
                for finding in after_report.get("findings", ())
            ):
                raise AssertionError("Post-commit audit still contains the selected overlap")

            return {
                "adapter_type": adapter["adapter_type"],
                "cad_version": adapter.get("cad_version"),
                "document_id_sha256": _digest(document_id),
                "display_name_sha256": _digest(display_name),
                "revision_before": revision,
                "revision_after": after["revision"],
                "entity_count_before": entity_count,
                "entity_count_after": after["entity_count"],
                "selected_entity_ref_sha256": _digest(selected_ref),
                "selected_rule": selected_rule,
                "audit_id_sha256": _digest(audit_id),
                "job_id_sha256": _digest(job_id),
                "approval_id_sha256": _digest(approval_id),
                "plan_hash": plan_hash,
                "operation_count": 1,
                "preview_deleted": 1,
                "post_audit_selected_finding_present": False,
            }


def run_acceptance(
    *,
    config_path: Path,
    case_name: str,
    work_root: Path,
    evidence_path: Path,
    target: Literal["overlapping-bore", "duplicate-baseplate"] = "overlapping-bore",
) -> dict[str, Any]:
    config_path = config_path.resolve(strict=True)
    case_root = work_root.resolve() / _safe_case_name(case_name)
    case_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ["CAD_HARNESS_CONFIG"] = str(config_path)
    os.environ["CAD_HARNESS_ADAPTER"] = "com"
    os.environ["CAD_HARNESS_READ_SEMANTIC_ADAPTER"] = "auto"
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

    workflow = asyncio.run(_workflow(config_path, target))
    result = {
        "schema_version": "1.0",
        "real_autocad_evidence": True,
        "production_evidence": False,
        "attached_existing_document": True,
        "opened_or_created_autocad_document": False,
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
        "--target",
        choices=("overlapping-bore", "duplicate-baseplate"),
        default="overlapping-bore",
    )
    args = parser.parse_args()
    result = run_acceptance(
        config_path=args.config,
        case_name=args.case_name,
        work_root=args.work_root,
        evidence_path=args.evidence,
        target=args.target,
    )
    print(json.dumps({"ok": True, **result["workflow"]}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
