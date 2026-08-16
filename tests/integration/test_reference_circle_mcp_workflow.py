"""Public MCP preparation path for the exact standalone-circle UX regression."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from apps.mcp_server.server import create_server

from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.domain.errors import (
    ApprovalRequiredError,
    PlanHashMismatchError,
    UnsupportedSchemaVersionError,
)
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.validation import ValidationStage
from cad_harness.domain.value_objects.units import Unit


def test_r20_at_origin_layer_zero_prepares_without_extra_questions_or_dwg_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "test-secret")
    config = tmp_path / "planning.yaml"
    config.write_text(
        "\n".join(
            [
                "adapter:",
                "  type: fake",
                "mcp:",
                "  client_profiles:",
                "    clients:",
                "      anonymous:",
                "        mode: planning",
                "storage:",
                f"  sqlite_path: '{(tmp_path / 'harness.db').as_posix()}'",
                f"  preview_directory: '{(tmp_path / 'previews').as_posix()}'",
                f"  checkpoint_directory: '{(tmp_path / 'checkpoints').as_posix()}'",
                f"  export_directory: '{(tmp_path / 'exports').as_posix()}'",
                "observability:",
                "  log_level: ERROR",
            ]
        ),
        encoding="utf-8",
    )
    mcp, context = create_server(config)

    created = asyncio.run(mcp.call_tool("cad_job_create", {}))
    created_payload = created[1] if isinstance(created, tuple) else created
    job_id = created_payload["data"]["job_id"]
    spec = {
        "units": "mm",
        "drawing": {
            "datum": {"type": "point", "point_mm": [0.0, 0.0]},
        },
        "features": [
            {
                "feature_id": "reference-circle-live-request",
                "type": "reference_circle",
                "parameters": {"radius_mm": 20.0, "layer_name": "0"},
            }
        ],
    }
    prepared = asyncio.run(
        mcp.call_tool(
            "cad_change_prepare",
            {
                "job_id": job_id,
                "spec": spec,
            },
        )
    )
    payload = prepared[1] if isinstance(prepared, tuple) else prepared

    assert payload["status"] == "ok"
    assert payload["missing_inputs"] == []
    assert payload["data"]["operation_count"] == 1
    assert payload["data"]["validation"]["commit_allowed"] is True
    assert payload["data"]["semantic_diff"]["summary"] == {
        "added": 1,
        "modified": 0,
        "deleted": 0,
    }
    assert context.service.store.get_job(job_id).state is JobState.VALIDATED
    assert isinstance(context.service.adapter, FakeAutoCADAdapter)
    assert context.service.adapter.document.entities == {}

    legacy_job = asyncio.run(mcp.call_tool("cad_job_create", {}))
    legacy_job_payload = legacy_job[1] if isinstance(legacy_job, tuple) else legacy_job
    rejected_schema = asyncio.run(
        mcp.call_tool(
            "cad_change_prepare",
            {
                "job_id": legacy_job_payload["data"]["job_id"],
                "spec": {**spec, "schema_version": "1.12"},
            },
        )
    )
    rejected_schema_payload = (
        rejected_schema[1] if isinstance(rejected_schema, tuple) else rejected_schema
    )
    assert rejected_schema_payload["status"] == "rejected"
    assert rejected_schema_payload["error"]["code"] == "UNSUPPORTED_SCHEMA_VERSION"
    legacy_job_id = legacy_job_payload["data"]["job_id"]
    assert context.service.store.get_spec(legacy_job_id) is None
    assert context.service.store.get_plan(legacy_job_id) is None
    assert context.service.store.get_job(legacy_job_id).state is JobState.CREATED

    geometry_job_result = asyncio.run(mcp.call_tool("cad_job_create", {}))
    geometry_job_payload = (
        geometry_job_result[1] if isinstance(geometry_job_result, tuple) else geometry_job_result
    )
    geometry_job_id = geometry_job_payload["data"]["job_id"]
    asyncio.run(mcp.call_tool("cad_spec_submit", {"job_id": geometry_job_id, "spec": spec}))
    asyncio.run(mcp.call_tool("cad_preview", {"job_id": geometry_job_id}))
    geometry_validation = asyncio.run(
        mcp.call_tool(
            "cad_validate",
            {"job_id": geometry_job_id, "stage": "preview_geometry"},
        )
    )
    geometry_validation_payload = (
        geometry_validation[1] if isinstance(geometry_validation, tuple) else geometry_validation
    )
    assert geometry_validation_payload["data"]["commit_allowed"] is False
    assert context.service.store.get_job(geometry_job_id).state is JobState.PREVIEWED
    with pytest.raises(ApprovalRequiredError):
        context.service.approve(geometry_job_id, "engineer-test")
    asyncio.run(
        mcp.call_tool(
            "cad_validate",
            {"job_id": geometry_job_id, "stage": "pre_commit"},
        )
    )
    assert context.service.store.get_job(geometry_job_id).state is JobState.VALIDATED
    geometry_report = context.service.store.get_validation(geometry_job_id)
    assert geometry_report is not None
    geometry_warnings = tuple(
        finding.rule_id
        for finding in geometry_report.findings
        if finding.severity.value == "warning"
    )
    assert context.service.approve(
        geometry_job_id,
        "engineer-test",
        geometry_warnings,
    )[0]

    unit_job = context.service.create_job()
    context.service.submit_spec(unit_job.job_id, spec)
    context.service.preview(unit_job.job_id)
    unit_report = context.service.validate(unit_job.job_id, ValidationStage.PRE_COMMIT)
    unit_warnings = tuple(
        finding.rule_id for finding in unit_report.findings if finding.severity.value == "warning"
    )
    _, unit_token = context.service.approve(
        unit_job.job_id,
        "engineer-test",
        unit_warnings,
    )
    unit_plan = context.service.store.get_plan(unit_job.job_id)
    assert unit_plan is not None and unit_plan.plan_hash is not None
    context.service.adapter.document.units = Unit.INCH
    with pytest.raises(ApprovalRequiredError):
        context.service.commit(
            unit_job.job_id,
            idempotency_key="stale-live-units-attempt",
            expected_revision=unit_job.expected_revision,
            plan_hash=unit_plan.plan_hash,
            approval_token=unit_token,
        )
    assert context.service.adapter.document.entities == {}
    context.service.adapter.document.units = Unit.MM

    obsolete_job = context.service.create_job()
    context.service.submit_spec(obsolete_job.job_id, spec)
    current_plan = context.service.store.get_plan(obsolete_job.job_id)
    assert current_plan is not None
    obsolete_plan = current_plan.model_copy(
        update={"schema_version": "1.12", "plan_hash": None}
    ).with_hash()
    context.service.store.save_plan(obsolete_plan)
    preview_files_before = tuple(sorted((tmp_path / "previews").rglob("*")))
    with pytest.raises(UnsupportedSchemaVersionError):
        context.service.preview(obsolete_job.job_id)
    with pytest.raises(UnsupportedSchemaVersionError):
        context.service.validate(obsolete_job.job_id, ValidationStage.PRE_COMMIT)
    assert tuple(sorted((tmp_path / "previews").rglob("*"))) == preview_files_before
    assert context.service.store.get_validation(obsolete_job.job_id) is None
    assert context.service.store.get_job(obsolete_job.job_id).state is JobState.PLANNED
    with pytest.raises(UnsupportedSchemaVersionError):
        context.service.approve(
            obsolete_job.job_id,
            "engineer-test",
            ("STD-PROFILE-PROVENANCE",),
        )
    with pytest.raises(UnsupportedSchemaVersionError):
        context.service.commit(
            obsolete_job.job_id,
            idempotency_key="obsolete-schema-attempt",
            expected_revision=obsolete_job.expected_revision,
            plan_hash=str(obsolete_plan.plan_hash),
            approval_token="not-reached",
        )
    assert context.service.adapter.document.entities == {}

    approval_id, approval_token = context.service.approve(
        job_id,
        "engineer-test",
        ("STD-PROFILE-PROVENANCE",),
    )
    for tool_name in ("cad_spec_submit", "cad_change_submit", "cad_change_prepare"):
        rejected = asyncio.run(
            mcp.call_tool(
                tool_name,
                {"job_id": job_id, "spec": spec},
            )
        )
        rejected_payload = rejected[1] if isinstance(rejected, tuple) else rejected
        assert rejected_payload["status"] == "rejected"
        assert rejected_payload["error"]["code"] == "INVALID_FEATURE_PARAMETERS"
    approved = context.service.store.get_job(job_id)
    assert approved is not None and approved.state is JobState.APPROVED
    assert context.service.store.get_approval(approval_id) is not None

    approved_plan = context.service.store.get_plan(job_id)
    assert approved_plan is not None and approved_plan.plan_hash is not None
    operation = approved_plan.operations[0]
    tampered_operation = operation.model_copy(
        update={"geometry": {**operation.geometry, "diameter_mm": 200.0}}
    )
    tampered_plan = approved_plan.model_copy(update={"operations": (tampered_operation,)})
    assert not tampered_plan.verify_hash(str(tampered_plan.plan_hash))
    context.service.store.save_plan(tampered_plan)
    with pytest.raises(PlanHashMismatchError):
        context.service.commit(
            job_id,
            idempotency_key="tampered-plan-attempt",
            expected_revision=approved.expected_revision,
            plan_hash=str(approved_plan.plan_hash),
            approval_token=approval_token,
        )
    assert context.service.adapter.document.entities == {}
