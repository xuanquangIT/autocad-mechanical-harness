"""Read, audit, measure, and take off the exact drawing already open in AutoCAD."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from scripts.live_mcp_r26_acceptance import _SETUP_CONFIRMATIONS, _call


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AssertionError(f"{label} did not return an object")
    return cast(Mapping[str, Any], value)


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


def _classify_contours(model: Mapping[str, Any]) -> tuple[str, list[str]]:
    outline_ref: str | None = None
    inner_refs: list[str] = []
    for raw_entity in model.get("entities", ()):
        entity = _mapping(raw_entity, "drawing entity")
        geometry = _mapping(entity.get("geometry"), "entity geometry")
        entity_ref = entity.get("entity_ref")
        if not isinstance(entity_ref, str):
            raise AssertionError("Drawing entity did not contain a stable reference")
        if geometry.get("kind") == "polyline" and geometry.get("closed") is True:
            if outline_ref is not None:
                raise AssertionError("Acceptance drawing contains more than one closed polyline")
            outline_ref = entity_ref
        elif geometry.get("kind") == "circle":
            inner_refs.append(entity_ref)
    if outline_ref is None:
        raise AssertionError("Acceptance drawing contains no closed plate outline")
    if len(inner_refs) != 4:
        raise AssertionError("Acceptance drawing must contain exactly four circular holes")
    return outline_ref, sorted(inner_refs)


def _is_close(value: object, expected: float, *, tolerance: float = 0.01) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and abs(value - expected) <= tolerance
    )


def _classify_complex_flange(model: Mapping[str, Any]) -> tuple[str, list[str]]:
    outer_ref: str | None = None
    keyed_bore_ref: str | None = None
    redundant_bore_ref: str | None = None
    bolt_refs: list[str] = []
    slot_arc_count = 0
    slot_line_count = 0
    bracket_found = False
    for raw_entity in model.get("entities", ()):
        entity = _mapping(raw_entity, "drawing entity")
        geometry = _mapping(entity.get("geometry"), "entity geometry")
        entity_ref = entity.get("entity_ref")
        if not isinstance(entity_ref, str):
            raise AssertionError("Drawing entity did not contain a stable reference")
        kind = geometry.get("kind")
        center = geometry.get("center_mm")
        if kind == "circle" and isinstance(center, list) and len(center) == 2:
            radius = geometry.get("radius_mm")
            if _is_close(center[0], 350.0) and _is_close(center[1], 100.0):
                if _is_close(radius, 100.0):
                    outer_ref = entity_ref
                elif _is_close(radius, 40.0):
                    redundant_bore_ref = entity_ref
            elif _is_close(radius, 8.0):
                radial_distance = math.hypot(float(center[0]) - 350.0, float(center[1]) - 100.0)
                if _is_close(radial_distance, 75.0):
                    bolt_refs.append(entity_ref)
        elif kind == "arc" and _is_close(geometry.get("radius_mm"), 12.0):
            slot_arc_count += 1
        elif kind == "line":
            start = geometry.get("start_mm")
            end = geometry.get("end_mm")
            if (
                isinstance(start, list)
                and isinstance(end, list)
                and all(-62.01 <= float(point[1]) <= -37.99 for point in (start, end))
            ):
                slot_line_count += 1
        elif kind == "polyline" and geometry.get("closed") is True:
            bounds = entity.get("bounding_box_mm")
            if isinstance(bounds, list) and len(bounds) == 4:
                if all(
                    abs(float(actual) - expected) <= 0.02
                    for actual, expected in zip(
                        bounds,
                        (310.0004930209909, 60.00985090974035, 389.9995069790091, 148.0),
                        strict=True,
                    )
                ):
                    keyed_bore_ref = entity_ref
                if all(
                    _is_close(actual, expected)
                    for actual, expected in zip(bounds, (500.0, 0.0, 650.0, 120.0), strict=True)
                ):
                    bracket_found = True
    if redundant_bore_ref is not None:
        raise AssertionError("Live keyed flange still contains a redundant circular bore")
    if outer_ref is None or keyed_bore_ref is None or len(bolt_refs) != 8:
        raise AssertionError("Live keyed flange geometry was incomplete")
    if slot_arc_count != 2 or slot_line_count != 2:
        raise AssertionError("Live obround slot geometry was incomplete")
    if not bracket_found:
        raise AssertionError("Live L-bracket geometry was incomplete")
    return outer_ref, [keyed_bore_ref, *sorted(bolt_refs)]


async def _workflow(*, layout: Literal["baseplate", "complex"]) -> dict[str, Any]:
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
            tools = await session.list_tools()
            status = await _call(session, "cad_status", {})
            adapter = _mapping(status["data"]["adapter"], "adapter status")
            if (
                adapter.get("adapter_type") != "dotnet_bridge"
                or adapter.get("available") is not True
            ):
                raise AssertionError("MCP did not attach to the loaded live .NET bridge")
            if adapter.get("cad_version") != "26.0":
                raise AssertionError("MCP did not attach to the expected AutoCAD R26 instance")

            before = await _call(session, "cad_document_inspect", {})
            before_document = _mapping(before["data"], "document inspection")
            document_id = before_document.get("document_id")
            revision = before_document.get("revision")
            if not isinstance(document_id, str) or not isinstance(revision, str):
                raise AssertionError("Live document inspection returned incomplete provenance")
            if adapter.get("active_document_id") != document_id:
                raise AssertionError("Bridge status and document inspection disagree")

            read = await _call(
                session,
                "cad_drawing_read",
                _active_read_arguments(document_id),
            )
            model = _mapping(read["data"], "drawing model")
            if model.get("document_id") != document_id or model.get("revision") != revision:
                raise AssertionError("Drawing model is not pinned to the inspected revision")
            if (
                model.get("geometry_normalized") is not True
                or model.get("source_unit_code") != "mm"
            ):
                raise AssertionError("Live drawing geometry was not normalized from millimetres")
            if layout == "baseplate":
                outline_ref, inner_refs = _classify_contours(model)
                expected_perimeter = 520.0
                expected_hole_count = 4
                expected_pierce_count = 5
                thickness_mm = 10.0
                expected_net_area = 160.0 * 100.0 - 4.0 * math.pi * 7.0**2
                net_area_tolerance = 0.01
            else:
                outline_ref, inner_refs = _classify_complex_flange(model)
                expected_perimeter = math.tau * 100.0
                expected_hole_count = 8
                expected_pierce_count = 10
                thickness_mm = 20.0
                bore_radius = 40.0
                key_width = 22.0
                key_depth = 8.0
                half_width = key_width / 2.0
                chord_y = math.sqrt(bore_radius**2 - half_width**2)
                circular_segment = (
                    bore_radius**2 * math.acos(chord_y / bore_radius) - chord_y * half_width
                )
                keyed_bore_area = (
                    math.pi * bore_radius**2
                    - circular_segment
                    + key_width * (bore_radius + key_depth - chord_y)
                )
                expected_net_area = math.pi * 100.0**2 - keyed_bore_area - 8.0 * math.pi * 8.0**2
                # The live keyed bore is the approved 0.01 mm chordal polyline.
                # Compare it to the exact circular-segment oracle with a bounded
                # approximation allowance instead of pretending byte equality.
                net_area_tolerance = 2.0

            recognized = await _call(session, "cad_feature_recognize", {"model": dict(model)})
            recognition = _mapping(recognized["data"], "feature recognition")
            audited = await _call(session, "cad_audit", {"model": dict(model)})
            audit = _mapping(audited["data"], "drawing audit")
            measured = await _call(
                session,
                "cad_measure",
                {
                    "model": dict(model),
                    "request": {
                        "kind": "contour_perimeter",
                        "entity_refs": [outline_ref],
                    },
                },
            )
            measurement = _mapping(measured["data"], "measurement")
            if abs(float(measurement.get("value", 0.0)) - expected_perimeter) > 0.01:
                raise AssertionError("Live outline perimeter did not match analytic geometry")

            taken_off = await _call(
                session,
                "cad_takeoff",
                {
                    "model": dict(model),
                    "request": {
                        "document_id": document_id,
                        "parts": [
                            {
                                "part_code": f"LIVE-{layout.upper()}",
                                "outline_entity_ref": outline_ref,
                                "inner_contour_entity_refs": inner_refs,
                                "thickness_mm": thickness_mm,
                                "material_code": "SS400",
                                "quantity": 1,
                            }
                        ],
                        "material_profile_ref": "demo-materials@1.0",
                    },
                },
            )
            takeoff = _mapping(taken_off["data"], "takeoff")
            parts = takeoff.get("parts")
            if not isinstance(parts, list) or len(parts) != 1:
                raise AssertionError("Live takeoff did not return one plate")
            part = _mapping(parts[0], "takeoff part")
            hole_groups = part.get("hole_groups")
            if not isinstance(hole_groups, list):
                raise AssertionError("Live takeoff returned invalid hole groups")
            hole_count = sum(
                int(_mapping(group, "takeoff hole group").get("count", 0)) for group in hole_groups
            )
            if (
                hole_count != expected_hole_count
                or part.get("pierce_count") != expected_pierce_count
            ):
                raise AssertionError(
                    "Live takeoff inner-contour mismatch: "
                    f"holes={hole_count}, pierces={part.get('pierce_count')}"
                )
            if abs(float(part.get("net_area_mm2", 0.0)) - expected_net_area) > net_area_tolerance:
                raise AssertionError(
                    "Live takeoff net area did not match the independent analytic oracle"
                )

            after = await _call(session, "cad_document_inspect", {})
            after_document = _mapping(after["data"], "post-read document inspection")
            if (
                after_document.get("document_id") != document_id
                or after_document.get("revision") != revision
                or after_document.get("entity_count") != before_document.get("entity_count")
            ):
                raise AssertionError("A read-only MCP operation changed the active drawing")

            report = _mapping(audit.get("report"), "audit report")
            return {
                "tool_count": len(tools.tools),
                "layout": layout,
                "adapter": {
                    "adapter_type": adapter["adapter_type"],
                    "available": adapter["available"],
                    "cad_application": adapter.get("cad_application"),
                    "cad_version": adapter.get("cad_version"),
                    "version_supported": adapter.get("version_supported"),
                    "capabilities": adapter.get("capabilities", []),
                },
                "document": {
                    "document_id_sha256": _digest(document_id),
                    "display_name_sha256": _digest(str(before_document.get("display_name", ""))),
                    "revision_before": revision,
                    "revision_after": after_document["revision"],
                    "entity_count_before": before_document["entity_count"],
                    "entity_count_after": after_document["entity_count"],
                },
                "read": {
                    "entity_count": len(model.get("entities", ())),
                    "coverage_complete": model.get("coverage_complete"),
                    "source_unit_code": model.get("source_unit_code"),
                    "geometry_normalized": model.get("geometry_normalized"),
                },
                "recognition": {
                    "feature_count": len(recognition.get("features", ())),
                    "ambiguous_group_count": len(recognition.get("ambiguous_groups", ())),
                },
                "audit": {
                    "finding_count": len(report.get("findings", ())),
                    "blocking_count": report.get("blocking_count"),
                },
                "measurement": {
                    "kind": measurement["kind"],
                    "value": measurement["value"],
                    "unit": measurement["unit"],
                },
                "takeoff": {
                    "net_area_mm2": part["net_area_mm2"],
                    "unit_mass_kg": part["unit_mass_kg"],
                    "cut_length_mm": part["cut_length_mm"],
                    "hole_count": hole_count,
                    "pierce_count": part["pierce_count"],
                    "company_approved": takeoff["company_approved"],
                },
            }


def run_acceptance(
    *,
    config_path: Path,
    work_root: Path,
    evidence_path: Path,
    layout: Literal["baseplate", "complex"] = "baseplate",
) -> dict[str, Any]:
    config_path = config_path.resolve(strict=True)
    case_root = work_root.resolve() / f"existing-bridge-read-{secrets.token_hex(8)}"
    case_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ["CAD_HARNESS_CONFIG"] = str(config_path)
    os.environ["CAD_HARNESS_ADAPTER"] = "dotnet_bridge"
    os.environ["CAD_HARNESS_APPROVAL_SECRET"] = secrets.token_urlsafe(48)
    os.environ["CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS"] = _SETUP_CONFIRMATIONS
    os.environ["CAD_HARNESS_SQLITE_PATH"] = str(case_root / "harness.db")
    os.environ["CAD_HARNESS_PREVIEW_DIR"] = str(case_root / "previews")
    os.environ["CAD_HARNESS_CHECKPOINT_DIR"] = str(case_root / "checkpoints")
    os.environ["CAD_HARNESS_LOG_LEVEL"] = "ERROR"
    os.environ.pop("CAD_HARNESS_LIVE_WRITE_VERIFIED", None)

    workflow = asyncio.run(_workflow(layout=layout))
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "real_autocad_evidence": True,
        "production_evidence": False,
        "attached_existing_document": True,
        "read_only_workflow": True,
        "drawing_mutated": False,
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
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--layout", choices=("baseplate", "complex"), default="baseplate")
    args = parser.parse_args()
    result = run_acceptance(
        config_path=args.config,
        work_root=args.work_root,
        evidence_path=args.evidence,
        layout=args.layout,
    )
    workflow = result["workflow"]
    print(
        json.dumps(
            {
                "ok": True,
                "adapter_type": workflow["adapter"]["adapter_type"],
                "drawing_mutated": result["drawing_mutated"],
                "entity_count": workflow["read"]["entity_count"],
                "measurement_mm": workflow["measurement"]["value"],
                "takeoff_net_area_mm2": workflow["takeoff"]["net_area_mm2"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
