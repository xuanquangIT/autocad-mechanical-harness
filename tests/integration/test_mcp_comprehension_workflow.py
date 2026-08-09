"""Functional MCP workflow for the six Task 24 tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import ezdxf
import pytest
import yaml
from apps.mcp_server import tools as tool_package
from apps.mcp_server.server import create_server
from apps.mcp_server.tools import permissions
from sqlalchemy import select

from cad_harness.domain.models.envelope import ToolResponse
from cad_harness.persistence.engine import build_engine, build_session_factory
from cad_harness.persistence.models import AuditEventRow
from cad_harness.security.client_profiles import ClientPermissionProfile


@pytest.fixture
def mcp_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "harness.db"
    monkeypatch.setenv("CAD_HARNESS_ADAPTER", "fake")
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "test-secret")
    monkeypatch.setenv("CAD_HARNESS_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("CAD_HARNESS_SQLITE_PATH", str(database))
    monkeypatch.setenv("CAD_HARNESS_LOG_LEVEL", "ERROR")
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "security": {"export_path_allowlist": [str(tmp_path / "exports")]},
                # This functional workflow validates the MCP contracts, not the separate
                # one-second performance gate. Leave headroom for Windows spawn under a
                # fully loaded test process.
                "measure": {"timeout_seconds": 10},
            }
        ),
        encoding="utf-8",
    )
    mcp, context = create_server(config)
    return mcp, context, database


def _payload(result: Any) -> dict[str, Any]:
    payload = result[1] if isinstance(result, tuple) else result
    return ToolResponse.model_validate(payload).model_dump(mode="json", exclude_none=True)


def _model() -> dict[str, Any]:
    return {
        "document_id": "doc-mcp",
        "revision": "sha256:mcp",
        "display_name": "customer-project.dxf",
        "source_unit_code": "mm",
        "to_mm_factor": 1.0,
        "geometry_normalized": True,
        "scope": {"kind": "model_space"},
        "entities": [
            {
                "entity_ref": "outline",
                "entity_type": "AcDbPolyline",
                "layer": "OBJECT",
                "visible": True,
                "space": "model",
                "geometry": {
                    "kind": "polyline",
                    "vertices": [
                        {"point_mm": [0.0, 0.0]},
                        {"point_mm": [100.0, 0.0]},
                        {"point_mm": [100.0, 50.0]},
                        {"point_mm": [0.0, 50.0]},
                    ],
                    "closed": True,
                },
                "bounding_box_mm": [0.0, 0.0, 100.0, 50.0],
            }
        ],
        "arc_chord_tolerance_mm": 0.01,
    }


def test_read_recognize_measure_takeoff_and_audit_are_structured(
    mcp_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, context, database = mcp_server
    drawing_path = tmp_path / "sensitive-project-name.dxf"
    document = ezdxf.new("R2018")
    document.units = 4
    document.layers.add("OBJECT")
    document.modelspace().add_lwpolyline(
        [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0)],
        close=True,
        dxfattribs={"layer": "OBJECT"},
    )
    document.saveas(drawing_path)

    read = _payload(
        asyncio.run(
            mcp.call_tool(
                "cad_drawing_read",
                {
                    "request_id": "req-read",
                    "request": {
                        "source": {"kind": "file", "format": "dxf", "ref": str(drawing_path)},
                        "scope": {"kind": "model_space"},
                        "max_entities": 100,
                        "max_block_nesting_depth": 3,
                    },
                },
            )
        )
    )
    assert read["status"] == "ok"
    assert read["request_id"] == "req-read"
    assert read["data"]["display_name"].startswith("path:")
    assert "sensitive-project-name" not in str(read)
    assert read["data"]["entities"][0]["geometry"]["kind"] == "polyline"

    missing = _payload(
        asyncio.run(
            mcp.call_tool(
                "cad_drawing_read",
                {
                    "request": {
                        "source": {
                            "kind": "file",
                            "format": "dxf",
                            "ref": str(tmp_path / "secret-customer-missing.dxf"),
                        },
                        "max_entities": 100,
                        "max_block_nesting_depth": 3,
                    }
                },
            )
        )
    )
    assert missing["status"] == "rejected"
    assert missing["error"]["details"]["display_name"].startswith("path:")
    assert "secret-customer" not in str(missing)

    model = _model()
    recognized = _payload(asyncio.run(mcp.call_tool("cad_feature_recognize", {"model": model})))
    assert recognized["status"] == "ok"
    assert recognized["data"]["features"][0]["feature_type"] == "part_outline"

    measured = _payload(
        asyncio.run(
            mcp.call_tool(
                "cad_measure",
                {
                    "model": model,
                    "request": {"kind": "contour_perimeter", "entity_refs": ["outline"]},
                },
            )
        )
    )
    assert measured["data"]["value"] == 300.0
    assert measured["data"]["unit"] == "mm"

    takeoff = _payload(
        asyncio.run(
            mcp.call_tool(
                "cad_takeoff",
                {
                    "model": model,
                    "request": {
                        "document_id": "doc-mcp",
                        "parts": [
                            {
                                "part_code": "P-001",
                                "outline_entity_ref": "outline",
                                "thickness_mm": 10.0,
                                "material_code": "SS400",
                                "quantity": 2,
                            }
                        ],
                        "material_profile_ref": "demo-materials@1.0",
                    },
                },
            )
        )
    )
    assert takeoff["status"] == "ok"
    assert takeoff["data"]["parts"][0]["evidence"]["quantity"] == ["outline"]

    audited = _payload(asyncio.run(mcp.call_tool("cad_audit", {"model": model})))
    assert audited["status"] == "ok"
    assert audited["data"]["audit_id"].startswith("audit_")

    rejected_export = _payload(
        asyncio.run(
            mcp.call_tool(
                "cad_takeoff_export",
                {
                    "report": takeoff["data"],
                    "target_path": str(tmp_path / "takeoff.json"),
                    "format": "json",
                    "request_id": "req-denied-export",
                },
            )
        )
    )
    assert rejected_export["status"] == "rejected"
    assert rejected_export["request_id"] == "req-denied-export"
    assert rejected_export["error"]["code"] == "TOOL_NOT_ALLOWED"

    full_profile = ClientPermissionProfile(
        client_id="integration-client",
        mode="full",
        allowed_tools=frozenset(tool_package.TOOL_NAMES),
    )
    monkeypatch.setattr(permissions, "resolve_profile", lambda *_args: full_profile)
    export_path = tmp_path / "exports" / "takeoff.json"
    exported = _payload(
        asyncio.run(
            mcp.call_tool(
                "cad_takeoff_export",
                {
                    "report": takeoff["data"],
                    "target_path": str(export_path),
                    "format": "json",
                },
            )
        )
    )
    assert exported["status"] == "ok"
    assert exported["data"]["artifact_ref"].startswith("path:")
    assert export_path.is_file()

    assert context.service.audit.verify_chain(None)
    engine = build_engine(database)
    sessions = build_session_factory(engine)
    with sessions() as session:
        rows = session.scalars(
            select(AuditEventRow).order_by(AuditEventRow.created_at, AuditEventRow.event_id)
        ).all()
    engine.dispose()
    ordered = [row.event_type for row in rows]
    cursor = 0
    expected = [
        "DRAWING_READ",
        "TAKEOFF_REPORT_CREATED",
        "DRAWING_AUDITED",
        "EXPORT_CREATED",
    ]
    for event_type in ordered:
        if cursor < len(expected) and event_type == expected[cursor]:
            cursor += 1
    assert cursor == len(expected)
    for event_type in expected:
        assert ordered.count(event_type) == 1
    audit_text = " ".join(str(row.payload_redacted_json) for row in rows)
    assert "sensitive-project-name" not in audit_text
    assert "secret-customer" not in audit_text
    assert "geometry" not in audit_text
    assert "approval_token" not in audit_text
    assert "prompt" not in audit_text
