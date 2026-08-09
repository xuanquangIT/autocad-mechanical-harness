# Feature: cad-ai-production-roadmap, Property 6: Unknown commit reconciliation returns exact differences
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.adapters.base import BaseAdapter
from cad_harness.application.services.reconciliation_service import ReconciliationService
from cad_harness.domain.models.document import EntitySummary, SelectionSnapshot
from cad_harness.domain.models.job import CadJob, JobState
from cad_harness.domain.models.result import EntityMappingRecord
from cad_harness.persistence.memory_store import InMemoryJobStore


class ReadOnlyReconciliationAdapter(BaseAdapter):
    def __init__(self, snapshot: SelectionSnapshot) -> None:
        self.snapshot = snapshot
        self.commit_calls = 0

    def inspect_selection(self, request):
        return self.snapshot

    def commit(self, request):
        self.commit_calls += 1
        raise AssertionError("reconciliation must never commit")


@given(
    states=st.lists(st.integers(min_value=0, max_value=2), min_size=1, max_size=12),
    unexpected=st.lists(st.booleans(), min_size=1, max_size=12),
)
@settings(max_examples=100, deadline=None)
def test_reconciliation_matches_a_mapping_entity_oracle(
    states: list[int], unexpected: list[bool]
) -> None:
    """**Validates: Requirements 2.5, 2.6**"""
    unexpected = (unexpected * (len(states) + 1))[: len(states)]
    store = InMemoryJobStore()
    job = CadJob(job_id="job_reconcile", document_id="doc_reconcile", expected_revision="rev_old")
    store.save_job(job.model_copy(update={"state": JobState.UNKNOWN_COMMIT_STATE}))
    entities: list[EntitySummary] = []
    expected: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {}

    for index, state in enumerate(states):
        operation_id = f"op:{index}"
        feature_id = f"feature:{index}"
        entity_ref = f"entity:{index}"
        store.entity_mappings.append(
            EntityMappingRecord(
                document_id=job.document_id,
                feature_id=feature_id,
                operation_id=operation_id,
                entity_ref=entity_ref,
                last_revision="rev_old",
            )
        )
        missing = (entity_ref,) if state == 0 else ()
        mismatched = (entity_ref,) if state == 2 else ()
        extras = (f"extra:{index}",) if unexpected[index] else ()
        if state:
            entities.append(
                EntitySummary(
                    entity_ref=entity_ref,
                    entity_type="AcDbEntity",
                    layer="OBJECT",
                    feature_id=feature_id if state == 1 else f"wrong:{index}",
                )
            )
        if unexpected[index]:
            entities.append(
                EntitySummary(
                    entity_ref=f"extra:{index}",
                    entity_type="AcDbEntity",
                    layer="OBJECT",
                    feature_id=feature_id,
                )
            )
        if missing or mismatched or extras:
            expected[operation_id] = (missing, mismatched, extras)

    adapter = ReadOnlyReconciliationAdapter(
        SelectionSnapshot(
            document_id=job.document_id, revision="rev_observed", entities=tuple(entities)
        )
    )
    report = ReconciliationService(store, adapter).reconcile(job.job_id)
    actual = {
        operation_id: (
            difference.missing_entity_refs,
            difference.mismatched_entity_refs,
            difference.unexpected_entity_refs,
        )
        for operation_id, difference in report.differences.items()
    }
    assert actual == expected
    assert adapter.commit_calls == 0
