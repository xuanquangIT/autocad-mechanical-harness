"""Deletion traceability in semantic diffs."""

from cad_harness.diff.semantic_diff import build_semantic_diff
from cad_harness.domain.models.document import DocumentSnapshot
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.value_objects.units import Unit


def test_semantic_diff_lists_every_deleted_entity_ref_without_affecting_creates() -> None:
    plan = OperationPlan(
        plan_id="plan-remediation",
        job_id="job-remediation",
        document_id="doc-remediation",
        expected_revision="sha256:before",
        profile_ref="demo-profile@1.0",
        operations=(
            Operation(
                operation_id="delete-1",
                feature_id="remediation:duplicate",
                type=OperationType.DELETE_ENTITY,
                layer="OBJECT",
                target_entity_ref="acad:handle:001",
            ),
            Operation(
                operation_id="create-1",
                feature_id="remediation:bridge",
                type=OperationType.CREATE_LINE,
                layer="OBJECT",
                geometry={"start_mm": [0.0, 0.0], "end_mm": [1.0, 0.0]},
            ),
            Operation(
                operation_id="delete-2",
                feature_id="remediation:zero-length",
                type=OperationType.DELETE_ENTITY,
                layer="OBJECT",
                target_entity_ref="acad:handle:002",
            ),
        ),
    ).with_hash()
    snapshot = DocumentSnapshot(
        document_id=plan.document_id,
        revision="sha256:before",
        path_hash="sha256:redacted",
        display_name="remediation.dwg",
        units=Unit.MM,
    )

    payload = build_semantic_diff(plan, snapshot).to_dict()

    deleted = [entry for entry in payload["entries"] if entry["change"] == "deleted"]
    created = next(entry for entry in payload["entries"] if entry["change"] == "added")
    assert [entry["target_entity_ref"] for entry in deleted] == [
        "acad:handle:001",
        "acad:handle:002",
    ]
    assert len(deleted) == 2
    assert created["target_entity_ref"] is None
