from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import scripts.live_existing_mcp_acceptance as subject


class _Session:
    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=[object()] * 22)


def test_duplicate_preflight_matches_plan_geometry_to_live_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations = [
        SimpleNamespace(
            type=SimpleNamespace(value="create_circle"),
            layer="OBJECT",
            geometry={"center_mm": [1.0, 2.0], "diameter_mm": 6.0},
        ),
        SimpleNamespace(
            type=SimpleNamespace(value="create_circles"),
            layer="OBJECT",
            geometry={"centers_mm": [[4.0, 5.0], [6.0, 7.0]], "diameter_mm": 2.0},
        ),
        SimpleNamespace(
            type=SimpleNamespace(value="create_line"),
            layer="OBJECT",
            geometry={"start_mm": [0.0, 0.0], "end_mm": [9.0, 0.0]},
        ),
        SimpleNamespace(
            type=SimpleNamespace(value="create_arc"),
            layer="OBJECT",
            geometry={
                "center_mm": [10.0, 11.0],
                "radius_mm": 4.0,
                "start_angle_deg": 90.0,
                "end_angle_deg": -90.0,
            },
        ),
        SimpleNamespace(
            type=SimpleNamespace(value="create_closed_polyline"),
            layer="OBJECT",
            geometry={"vertices_mm": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]},
        ),
    ]
    store = SimpleNamespace(get_plan=lambda _job_id: SimpleNamespace(operations=operations))
    monkeypatch.setattr(
        subject,
        "build_context",
        lambda _config: SimpleNamespace(service=SimpleNamespace(store=store)),
    )
    model = {
        "entities": [
            {
                "layer": "OBJECT",
                "geometry": {"kind": "circle", "center_mm": [1, 2], "radius_mm": 3},
            },
            {
                "layer": "OBJECT",
                "geometry": {"kind": "circle", "center_mm": [4, 5], "radius_mm": 1},
            },
            {
                "layer": "OBJECT",
                "geometry": {"kind": "circle", "center_mm": [6, 7], "radius_mm": 1},
            },
            {
                "layer": "OBJECT",
                "geometry": {"kind": "line", "start_mm": [0, 0], "end_mm": [9, 0]},
            },
            {
                "layer": "OBJECT",
                "geometry": {
                    "kind": "arc",
                    "center_mm": [10, 11],
                    "radius_mm": 4,
                    "start_angle_deg": 90,
                    "end_angle_deg": 270,
                },
            },
            {
                "layer": "OBJECT",
                "geometry": {
                    "kind": "polyline",
                    "vertices": [
                        {"point_mm": [0, 0], "bulge": 0},
                        {"point_mm": [1, 0], "bulge": 0},
                        {"point_mm": [1, 1], "bulge": 0},
                    ],
                    "closed": True,
                },
            },
        ]
    }

    planned = subject._plan_geometry_signatures(tmp_path / "live.yaml", "job-1")
    observed = subject._model_geometry_signatures(model)

    assert sum(planned.values()) == 6
    assert planned == observed


def test_existing_document_workflow_remediates_exact_live_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    inspections = iter(
        [
            {
                "document_id": "doc-1",
                "display_name": "existing.dwg",
                "revision": "sha256:before",
                "entity_count": 20,
                "read_only": False,
            },
            {
                "document_id": "doc-1",
                "display_name": "existing.dwg",
                "revision": "sha256:after",
                "entity_count": 23,
                "read_only": False,
            },
            {
                "document_id": "doc-1",
                "display_name": "existing.dwg",
                "revision": "sha256:before",
                "entity_count": 20,
                "read_only": False,
            },
        ]
    )

    jobs = iter(("job-1", "cleanup-job-1"))
    previews = iter(
        (
            {"artifacts": ["preview.svg"]},
            {
                "artifacts": ["cleanup.svg"],
                "semantic_diff": {"summary": {"deleted": 1}},
            },
        )
    )
    commits = iter(
        (
            {
                "status": "committed",
                "entity_results": [{"entity_ref": "new-1"}],
                "new_revision": "sha256:after",
                "checkpoint_id": None,
            },
            {
                "status": "committed",
                "entity_results": [{"entity_ref": "new-1"}],
                "new_revision": "sha256:before",
                "checkpoint_id": None,
            },
        )
    )
    drawing_reads = iter(
        (
            {
                "document_id": "doc-1",
                "revision": "sha256:before",
                "entities": [{"entity_ref": "old-1", "feature_id": "feature-1"}],
            },
            {
                "document_id": "doc-1",
                "revision": "sha256:after",
                "entities": [
                    {"entity_ref": "old-1", "feature_id": "feature-1"},
                    {"entity_ref": "new-1", "feature_id": "feature-1"},
                ],
            },
        )
    )

    async def call(_session: object, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        if name == "cad_status":
            return {"data": {"adapter": {"adapter_type": "com", "available": True}}}
        if name == "cad_document_inspect":
            return {"data": next(inspections)}
        if name == "cad_drawing_read":
            return {"data": next(drawing_reads)}
        if name == "cad_audit":
            return {
                "data": {
                    "audit_id": "audit-1",
                    "report": {
                        "findings": [{"rule_id": "DUPLICATE_ENTITY", "entity_ref": "new-1"}]
                    },
                }
            }
        if name == "cad_job_create":
            return {"data": {"job_id": next(jobs)}}
        if name == "cad_spec_submit":
            return {
                "data": {
                    "plan_hash": "sha256:plan",
                    "operation_count": 3,
                }
            }
        if name == "cad_change_submit":
            assert arguments["remediation"]["selected_findings"] == [
                {"rule_id": "DUPLICATE_ENTITY", "entity_ref": "new-1"}
            ]
            return {"data": {"plan_hash": "sha256:cleanup", "operation_count": 1}}
        if name == "cad_preview":
            return {"data": next(previews)}
        if name == "cad_validate":
            return {
                "data": {
                    "commit_allowed": True,
                    "blocking_count": 0,
                    "error_count": 0,
                }
            }
        if name == "cad_commit":
            expected_token = (
                "v2.commit-token" if arguments["job_id"] == "job-1" else "v2.cleanup-token"
            )
            assert arguments["approval_token"] == expected_token
            return {"data": next(commits)}
        raise AssertionError(f"unexpected tool call: {name}")

    def issue_approval(
        _config_path: Path,
        job_id: str,
        _plan_hash: str,
        _revision: str,
    ) -> tuple[str, str]:
        if job_id == "job-1":
            return "commit-approval-1", "v2.commit-token"
        return "cleanup-approval-1", "v2.cleanup-token"

    monkeypatch.setattr(subject, "_call", call)
    monkeypatch.setattr(subject, "_issue_live_approval", issue_approval)
    monkeypatch.setattr(
        subject,
        "_plan_geometry_signatures",
        lambda *_args: Counter({("duplicate",): 1}),
    )
    monkeypatch.setattr(
        subject,
        "_model_geometry_signatures",
        lambda *_args: Counter({("duplicate",): 1}),
    )

    result = asyncio.run(
        subject._workflow_session(
            _Session(),  # type: ignore[arg-type]
            config_path=tmp_path / "live.yaml",
            spec={"features": [{"feature_id": "feature-1", "type": "base_plate"}]},
            case_name="complex-existing",
        )
    )

    assert result["drawing_restored"] is True
    assert result["document"]["entity_count_before"] == 20
    assert result["document"]["entity_count_after"] == 23
    assert result["document"]["entity_count_restored"] == 20
    assert result["document"]["revision_restored"] == "sha256:before"
    assert result["cleanup"]["method"] == "audited_duplicate_remediation"
    assert result["cleanup"]["deleted_count"] == 1
    assert result["cleanup"]["restored_revision"] == "sha256:before"
    assert result["cleanup"]["approval_id_sha256"] == subject._digest_text("cleanup-approval-1")
    assert result["cleanup"]["job_id_sha256"] == subject._digest_text("cleanup-job-1")
    assert result["document"]["display_name_sha256"] == subject._digest_text("existing.dwg")
    assert result["document"]["document_id_sha256"] == subject._digest_text("doc-1")
    assert [name for name, _arguments in calls][-2:] == [
        "cad_commit",
        "cad_document_inspect",
    ]
    serialized = json.dumps(result, sort_keys=True)
    assert "commit-token" not in serialized
    assert "cleanup-token" not in serialized
    assert "existing.dwg" not in serialized
    assert "doc-1" not in serialized
    assert "job-1" not in serialized
    assert "approval-1" not in serialized


def test_existing_document_workflow_rejects_new_geometry_before_preview_or_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def call(_session: object, name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        if name == "cad_status":
            return {"data": {"adapter": {"adapter_type": "com", "available": True}}}
        if name == "cad_document_inspect":
            return {
                "data": {
                    "document_id": "doc-1",
                    "display_name": "existing.dwg",
                    "revision": "sha256:before",
                    "entity_count": 20,
                    "read_only": False,
                }
            }
        if name == "cad_drawing_read":
            return {
                "data": {
                    "document_id": "doc-1",
                    "revision": "sha256:before",
                    "entities": [{"entity_ref": "old-1", "feature_id": "other-feature"}],
                }
            }
        if name == "cad_job_create":
            return {"data": {"job_id": "job-unsafe"}}
        if name == "cad_spec_submit":
            return {"data": {"plan_hash": "sha256:unsafe", "operation_count": 1}}
        raise AssertionError(f"unexpected tool call: {name}")

    monkeypatch.setattr(subject, "_call", call)
    monkeypatch.setattr(
        subject,
        "_plan_geometry_signatures",
        lambda *_args: Counter({("new",): 1}),
    )
    monkeypatch.setattr(
        subject,
        "_model_geometry_signatures",
        lambda *_args: Counter(),
    )

    with pytest.raises(AssertionError, match="only commits geometry already present"):
        asyncio.run(
            subject._workflow_session(
                _Session(),  # type: ignore[arg-type]
                config_path=tmp_path / "live.yaml",
                spec={"features": [{"feature_id": "new-feature", "type": "base_plate"}]},
                case_name="unsafe-new-feature",
            )
        )

    assert calls == [
        "cad_status",
        "cad_document_inspect",
        "cad_drawing_read",
        "cad_job_create",
        "cad_spec_submit",
    ]
