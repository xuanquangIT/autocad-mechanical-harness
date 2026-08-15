# Feature: cad-ai-production-roadmap, Property 11: update/delete chỉ định vị entity qua entity_ref

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.adapters.autocad_com import ComAutoCADAdapter
from cad_harness.domain.errors import ComCallFailedError
from cad_harness.domain.models.operation_plan import Operation, OperationType
from cad_harness.persistence.memory_store import InMemoryJobStore


class _Entity:
    ObjectName = "AcDbLine"

    def __init__(self, handle: str) -> None:
        self.Handle = handle
        self.Layer = "OLD"
        self.deleted = False

    def Update(self) -> None:  # noqa: N802
        pass

    def Delete(self) -> None:  # noqa: N802
        self.deleted = True


class _Document:
    def __init__(self, entity: _Entity) -> None:
        self.entity = entity
        self.resolved_handles: list[str] = []

    def HandleToObject(self, handle: str) -> _Entity:  # noqa: N802
        self.resolved_handles.append(handle)
        assert handle == self.entity.Handle
        return self.entity

    def find_nearest(self, *args: object) -> None:
        raise AssertionError("coordinate lookup must never be used")


@given(
    handle=st.text(alphabet="0123456789ABCDEF", min_size=1, max_size=12),
    operation_type=st.sampled_from([OperationType.UPDATE_ENTITY, OperationType.DELETE_ENTITY]),
)
@settings(max_examples=100, deadline=None)
def test_update_delete_resolve_only_the_mapped_entity_ref(
    handle: str, operation_type: OperationType
) -> None:
    """**Validates: Requirements 4.6**"""
    entity_ref = f"acad:handle:{handle}"
    store = InMemoryJobStore()
    store.map_entity(
        document_id="doc-ref",
        feature_id="feature-ref",
        operation_id="op-original",
        entity_ref=entity_ref,
        revision="sha256:r1",
    )
    entity = _Entity(handle)
    document = _Document(entity)
    adapter = ComAutoCADAdapter(job_store=store)
    geometry = {"properties": {"Color": 3}} if operation_type is OperationType.UPDATE_ENTITY else {}
    operation = Operation(
        operation_id="op-change",
        feature_id="feature-ref",
        type=operation_type,
        layer="OBJECT",
        geometry=geometry,
        target_entity_ref=entity_ref,
    )

    adapter._execute(document, "doc-ref", operation)

    assert document.resolved_handles == [handle]
    assert entity.deleted is (operation_type is OperationType.DELETE_ENTITY)


def test_audit_bound_remediation_can_resolve_an_existing_hex_handle() -> None:
    entity = _Entity("A1B2")
    document = _Document(entity)
    operation = Operation(
        operation_id="op:remediation:delete",
        feature_id="remediation:OVERLAPPING_ENTITY:acad:handle:A1B2",
        type=OperationType.DELETE_ENTITY,
        layer="OBJECT",
        target_entity_ref="acad:handle:A1B2",
    )

    ComAutoCADAdapter()._execute(document, "doc-ref", operation)

    assert document.resolved_handles == ["A1B2"]
    assert entity.deleted is True


@pytest.mark.parametrize("entity_ref", ["acad:handle:NOTHEX", "raw-handle", "acad:handle:"])
def test_unmapped_or_noncanonical_target_remains_rejected(entity_ref: str) -> None:
    entity = _Entity("A1B2")
    operation = Operation(
        operation_id="op:change",
        feature_id="ordinary-feature",
        type=OperationType.DELETE_ENTITY,
        layer="OBJECT",
        target_entity_ref=entity_ref,
    )

    with pytest.raises(ComCallFailedError):
        ComAutoCADAdapter()._execute(_Document(entity), "doc-ref", operation)

    assert entity.deleted is False


def test_delete_receipt_does_not_read_the_invalidated_activex_entity() -> None:
    class _InvalidatingEntity:
        Layer = "OLD"

        def __init__(self) -> None:
            self.deleted = False

        @property
        def Handle(self) -> str:  # noqa: N802
            if self.deleted:
                raise RuntimeError("deleted ActiveX entity is invalid")
            return "C0FFEE"

        @property
        def ObjectName(self) -> str:  # noqa: N802
            if self.deleted:
                raise RuntimeError("deleted ActiveX entity is invalid")
            return "AcDbCircle"

        def Delete(self) -> None:  # noqa: N802
            self.deleted = True

    entity = _InvalidatingEntity()
    document = _Document(entity)  # type: ignore[arg-type]
    operation = Operation(
        operation_id="op:remediation:delete",
        feature_id="remediation:OVERLAPPING_ENTITY:acad:handle:C0FFEE",
        type=OperationType.DELETE_ENTITY,
        layer="OBJECT",
        target_entity_ref="acad:handle:C0FFEE",
    )

    results = ComAutoCADAdapter()._execute(document, "doc-ref", operation)

    assert results[0].entity_ref == "acad:handle:C0FFEE"
    assert results[0].entity_type == "AcDbCircle"
    assert results[0].measurements == {"deleted": True}
