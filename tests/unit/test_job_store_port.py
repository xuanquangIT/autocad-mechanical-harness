"""`JobStore` port conformance for the in-memory store (Requirements 1.1, 1.4, 1.8)."""

from __future__ import annotations

from typing import get_type_hints

from cad_harness.application.services.harness_service import HarnessService
from cad_harness.domain.models.result import Checkpoint
from cad_harness.domain.ports.repositories import JobStore
from cad_harness.persistence.memory_store import InMemoryJobStore


def _map(store: InMemoryJobStore, document_id: str, entity_ref: str, suffix: str) -> None:
    store.map_entity(
        document_id=document_id,
        feature_id=f"feature:{suffix}",
        operation_id=f"op:{suffix}",
        entity_ref=entity_ref,
        revision="sha256:r1",
    )


class TestPortConformance:
    def test_in_memory_store_satisfies_the_port(self) -> None:
        # The service depends on the port only, so the shipped store must satisfy it.
        assert isinstance(InMemoryJobStore(), JobStore)

    def test_port_declares_the_full_job_aggregate(self) -> None:
        for name in (
            "map_entity",
            "entity_mappings_for",
            "save_checkpoint",
            "save_approval",
            "get_approval",
            "revoke_approvals_for_job",
        ):
            assert hasattr(JobStore, name), name

    def test_service_depends_on_the_port_not_a_concrete_store(self) -> None:
        # Layering: the application layer must not name a persistence implementation.
        hints = get_type_hints(HarnessService.__init__)
        assert hints["store"] == JobStore | None


class TestEntityMappings:
    def test_mappings_are_scoped_per_document_in_insertion_order(self) -> None:
        store = InMemoryJobStore()
        _map(store, "doc_1", "ent_a", "a")
        _map(store, "doc_2", "ent_b", "b")
        _map(store, "doc_1", "ent_c", "c")

        refs = [m.entity_ref for m in store.entity_mappings_for("doc_1")]
        assert refs == ["ent_a", "ent_c"]
        assert store.entity_mappings_for("doc_3") == ()

    def test_mapping_records_the_observed_revision(self) -> None:
        store = InMemoryJobStore()
        _map(store, "doc_1", "ent_a", "a")
        record = store.entity_mappings_for("doc_1")[0]
        assert record.operation_id == "op:a"
        assert record.last_revision == "sha256:r1"


class TestCheckpoints:
    def test_checkpoint_round_trips(self) -> None:
        store = InMemoryJobStore()
        checkpoint = Checkpoint(
            checkpoint_id="ckpt_1",
            job_id="job_1",
            revision="sha256:r1",
            artifact_ref="checkpoints/ckpt_1.dwg",
        )
        store.save_checkpoint(checkpoint)
        assert store.checkpoints["ckpt_1"] == checkpoint
