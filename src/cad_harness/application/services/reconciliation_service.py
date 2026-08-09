"""Read-only reconciliation for jobs whose CAD commit outcome is unknown."""

from __future__ import annotations

from dataclasses import dataclass

from cad_harness.adapters.base import BaseAdapter
from cad_harness.domain.errors import UnknownCommitStateError
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.result import EntityMappingRecord
from cad_harness.domain.ports.autocad_adapter import SelectionRequest
from cad_harness.domain.ports.repositories import JobStore


@dataclass(frozen=True, slots=True)
class ReconciliationDifference:
    operation_id: str
    missing_entity_refs: tuple[str, ...] = ()
    mismatched_entity_refs: tuple[str, ...] = ()
    unexpected_entity_refs: tuple[str, ...] = ()
    unresolved: bool = False


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    job_id: str
    document_id: str
    observed_revision: str
    differences: dict[str, ReconciliationDifference]
    truncated: bool


class ReconciliationService:
    """Compare persisted mappings with one adapter read; this class has no write method."""

    def __init__(self, store: JobStore, adapter: BaseAdapter) -> None:
        self._store = store
        self._adapter = adapter

    def reconcile(self, job_id: str) -> ReconciliationReport:
        job = self._store.get_job(job_id)
        if job is None or job.state is not JobState.UNKNOWN_COMMIT_STATE:
            raise UnknownCommitStateError(
                "Only an UNKNOWN_COMMIT_STATE job can be reconciled",
                required_action="Select a job whose commit outcome is unknown",
                details={"job_id": job_id},
            )
        mappings = self._store.entity_mappings_for(job.document_id)
        snapshot = self._adapter.inspect_selection(
            SelectionRequest(document_id=job.document_id, max_entities=max(200, len(mappings) * 2))
        )
        actual = {entity.entity_ref: entity for entity in snapshot.entities}
        expected_refs = {mapping.entity_ref for mapping in mappings}
        feature_operations: dict[str, set[str]] = {}
        by_operation: dict[str, list[EntityMappingRecord]] = {}
        for mapping in mappings:
            feature_operations.setdefault(mapping.feature_id, set()).add(mapping.operation_id)
            by_operation.setdefault(mapping.operation_id, []).append(mapping)

        differences: dict[str, ReconciliationDifference] = {}
        for operation_id in sorted(by_operation):
            operation_mappings = by_operation[operation_id]
            missing: list[str] = []
            mismatched: list[str] = []
            for mapping in operation_mappings:
                entity = actual.get(mapping.entity_ref)
                if entity is None:
                    missing.append(mapping.entity_ref)
                elif entity.feature_id not in {None, mapping.feature_id}:
                    mismatched.append(mapping.entity_ref)

            expected_features = {mapping.feature_id for mapping in operation_mappings}
            unexpected = sorted(
                entity.entity_ref
                for entity in snapshot.entities
                if entity.entity_ref not in expected_refs
                and entity.feature_id in expected_features
                and len(feature_operations.get(str(entity.feature_id), ())) == 1
            )
            if missing or mismatched or unexpected or snapshot.truncated:
                differences[operation_id] = ReconciliationDifference(
                    operation_id=operation_id,
                    missing_entity_refs=tuple(sorted(missing)),
                    mismatched_entity_refs=tuple(sorted(mismatched)),
                    unexpected_entity_refs=tuple(unexpected),
                    unresolved=snapshot.truncated,
                )

        return ReconciliationReport(
            job_id=job_id,
            document_id=job.document_id,
            observed_revision=snapshot.revision,
            differences=differences,
            truncated=snapshot.truncated,
        )
