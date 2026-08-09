"""Fake adapter update/delete semantics used by remediation commits."""

from __future__ import annotations

import pytest

from cad_harness.adapters.fake import FakeAutoCADAdapter, FakeDocument, FakeEntity
from cad_harness.domain.errors import ComCallFailedError
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.result import CommitResult
from cad_harness.domain.ports.autocad_adapter import CommitRequest


def _adapter() -> tuple[FakeAutoCADAdapter, FakeEntity]:
    entity = FakeEntity(
        entity_ref="fake:handle:TARGET",
        entity_type="AcDbLine",
        layer="OLD",
        feature_id="original-feature",
        operation_id="original-operation",
        geometry={"properties": {"Color": 1}},
        measurements={},
    )
    document = FakeDocument(document_id="doc-remediation", entities={entity.entity_ref: entity})
    return FakeAutoCADAdapter(document), entity


def _commit(
    adapter: FakeAutoCADAdapter, *operations: Operation, key: str = "remediation-key"
) -> CommitResult:
    revision = adapter.current_revision()
    plan = OperationPlan(
        plan_id="plan-remediation",
        job_id="job-remediation",
        document_id=adapter.document.document_id,
        expected_revision=revision,
        profile_ref="demo-profile",
        operations=operations,
    ).with_hash()
    return adapter.commit(
        CommitRequest(
            plan=plan,
            idempotency_key=key,
            expected_revision=revision,
            approval_token="approved",
        )
    )


def _operation(kind: OperationType, target: str | None = "fake:handle:TARGET") -> Operation:
    return Operation(
        operation_id=f"op-{kind.value}",
        feature_id="remediation-feature",
        type=kind,
        layer="OBJECT",
        geometry={"properties": {"Color": 3, "StyleName": "DEMO"}},
        target_entity_ref=target,
    )


def test_update_mutates_only_target_and_changes_revision() -> None:
    adapter, target = _adapter()
    before_revision = adapter.current_revision()

    result = _commit(adapter, _operation(OperationType.UPDATE_ENTITY))

    assert adapter.document.entities[target.entity_ref] is target
    assert target.layer == "OBJECT"
    assert target.geometry["properties"] == {"Color": 3, "StyleName": "DEMO"}
    assert result.entity_results[0].entity_ref == target.entity_ref
    assert result.entity_results[0].measurements == {"layer": "OBJECT"}
    assert result.new_revision != before_revision


def test_delete_removes_exact_target_with_traceable_tombstone() -> None:
    adapter, target = _adapter()
    before_revision = adapter.current_revision()

    result = _commit(adapter, _operation(OperationType.DELETE_ENTITY))

    assert target.entity_ref not in adapter.document.entities
    assert result.entity_results[0].entity_ref == target.entity_ref
    assert result.entity_results[0].entity_type == target.entity_type
    assert result.entity_results[0].measurements == {"deleted": True}
    assert result.new_revision != before_revision


@pytest.mark.parametrize("kind", [OperationType.UPDATE_ENTITY, OperationType.DELETE_ENTITY])
def test_missing_remediation_target_is_rejected_without_a_write(kind: OperationType) -> None:
    adapter, target = _adapter()
    before_revision = adapter.current_revision()

    with pytest.raises(ComCallFailedError, match="not present") as info:
        _commit(adapter, _operation(kind, "fake:handle:MISSING"))

    assert info.value.details["reason"] == "entity_reference_not_found"
    assert adapter.current_revision() == before_revision
    assert adapter.document.entities[target.entity_ref] is target


def test_failed_batch_rolls_back_prior_staged_update() -> None:
    adapter, target = _adapter()
    before_revision = adapter.current_revision()

    with pytest.raises(ComCallFailedError):
        _commit(
            adapter,
            _operation(OperationType.UPDATE_ENTITY),
            _operation(OperationType.DELETE_ENTITY, "fake:handle:MISSING"),
        )

    assert adapter.current_revision() == before_revision
    assert adapter.document.entities[target.entity_ref] is target
    assert target.layer == "OLD"
    assert target.geometry == {"properties": {"Color": 1}}
    assert adapter.document.snapshots == {}
