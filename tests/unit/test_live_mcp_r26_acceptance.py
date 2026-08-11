from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from scripts.live_mcp_r26_acceptance import (
    ProcessIdentity,
    TrackedProcess,
    _cleanup_acceptance_processes,
    _close_owned_session_preserving_failure,
    _run_remediation_acceptance,
    _track_acceptance_processes,
    _wait_for_owned_com_readiness,
)

from cad_harness.adapters.autocad_com import ComAutoCADAdapter


class _McpResult:
    def __init__(self, data: dict[str, Any]) -> None:
        self.structuredContent = {"status": "ok", "data": data}
        self.content: list[Any] = []


class _TranscriptSession:
    def __init__(self, transcript: list[tuple[str, dict[str, Any]]]) -> None:
        self.transcript = list(transcript)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _McpResult:
        self.calls.append((name, arguments))
        expected_name, response = self.transcript.pop(0)
        assert name == expected_name
        return _McpResult(response)


class _DelayedDocuments:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.probes = 0

    @property
    def Count(self) -> int:  # noqa: N802 - mirrors the AutoCAD COM API
        self.probes += 1
        if self.probes <= self.failures:
            raise AttributeError("<unknown>.Count")
        return 0

    def Open(self, *_args: Any) -> None:  # noqa: N802 - mirrors the AutoCAD COM API
        raise AssertionError("The readiness probe must not invoke Documents.Open")


class _OwnedAdapterStub:
    def __init__(self, documents: _DelayedDocuments) -> None:
        self.app = type("App", (), {"Documents": documents})()

    def _require_current_owned_application(self) -> Any:
        return self.app


def test_owned_com_readiness_retries_without_opening_document() -> None:
    documents = _DelayedDocuments(failures=2)
    adapter = _OwnedAdapterStub(documents)

    _wait_for_owned_com_readiness(
        cast(ComAutoCADAdapter, adapter),
        timeout_seconds=1.0,
        poll_seconds=0.0,
    )

    assert documents.probes == 3


def test_owned_com_readiness_times_out_with_original_com_error() -> None:
    adapter = _OwnedAdapterStub(_DelayedDocuments(failures=10_000))

    with pytest.raises(TimeoutError) as caught:
        _wait_for_owned_com_readiness(
            cast(ComAutoCADAdapter, adapter),
            timeout_seconds=0.0,
            poll_seconds=0.0,
        )

    assert isinstance(caught.value.__cause__, AttributeError)


class _ClosingAdapterStub:
    def close_owned_session(self) -> None:
        raise RuntimeError("cleanup failed")


def test_cleanup_does_not_mask_original_acceptance_failure() -> None:
    original = AssertionError("workflow failed")

    _close_owned_session_preserving_failure(
        cast(ComAutoCADAdapter, _ClosingAdapterStub()),
        original,
    )


def test_cleanup_failure_is_raised_without_original_failure() -> None:
    with pytest.raises(RuntimeError, match="cleanup failed"):
        _close_owned_session_preserving_failure(
            cast(ComAutoCADAdapter, _ClosingAdapterStub()),
            None,
        )


class _ProcessAdapterStub:
    def __init__(
        self,
        active_pids: set[int],
        identities: dict[int, ProcessIdentity],
    ) -> None:
        self.active_pids = active_pids
        self.identities = identities

    def _acad_process_ids(self) -> set[int]:
        return set(self.active_pids)

    def _process_identity(self, pid: int) -> ProcessIdentity:
        return self.identities[pid]


def test_process_tracking_records_only_the_owned_process_lineage() -> None:
    adapter = _ProcessAdapterStub(
        {100, 200, 300, 400},
        {
            100: (r"C:\CAD\acad.exe", 900),
            200: (r"C:\CAD\acad.exe", 1_100),
            300: (r"C:\CAD\acad.exe", 1_050),
            400: (r"C:\CAD\acad.exe", 1_300),
        },
    )
    parents = {200: 300, 300: 50, 400: 999}
    tracked: dict[int, TrackedProcess] = {}

    _track_acceptance_processes(
        cast(ComAutoCADAdapter, adapter),
        preexisting_pids={100},
        acceptance_started_100ns=1_000,
        ownership_roots={200},
        tracked=tracked,
        parent_pid=parents.__getitem__,
    )

    assert tracked == {
        200: (r"C:\CAD\acad.exe", 1_100, 300),
        300: (r"C:\CAD\acad.exe", 1_050, 50),
    }


def test_process_tracking_rejects_pid_reuse_without_terminating() -> None:
    adapter = _ProcessAdapterStub(
        {100, 200},
        {200: (r"C:\CAD\acad.exe", 1_300)},
    )
    terminated: list[int] = []

    with pytest.raises(AssertionError, match="identity changed"):
        _cleanup_acceptance_processes(
            cast(ComAutoCADAdapter, adapter),
            preexisting_pids={100},
            acceptance_started_100ns=1_000,
            ownership_roots={200},
            tracked={200: (r"C:\CAD\acad.exe", 1_100, 50)},
            terminate=lambda pid, _identity: terminated.append(pid),
            parent_pid=lambda _pid: 50,
            timeout_seconds=0.0,
        )

    assert terminated == []


def test_process_tracking_does_not_adopt_a_reused_parent_pid() -> None:
    adapter = _ProcessAdapterStub(
        {200, 300},
        {
            200: (r"C:\CAD\acad.exe", 1_100),
            300: (r"C:\CAD\acad.exe", 1_200),
        },
    )
    tracked: dict[int, TrackedProcess] = {}

    _track_acceptance_processes(
        cast(ComAutoCADAdapter, adapter),
        preexisting_pids=set(),
        acceptance_started_100ns=1_000,
        ownership_roots={200},
        tracked=tracked,
        parent_pid={200: 300, 300: 50}.__getitem__,
    )

    assert tracked == {200: (r"C:\CAD\acad.exe", 1_100, 300)}


def test_process_tracking_rejects_process_created_before_acceptance_snapshot() -> None:
    adapter = _ProcessAdapterStub(
        {200},
        {200: (r"C:\CAD\acad.exe", 999)},
    )

    with pytest.raises(AssertionError, match="Unowned post-snapshot"):
        _track_acceptance_processes(
            cast(ComAutoCADAdapter, adapter),
            preexisting_pids=set(),
            acceptance_started_100ns=1_000,
            ownership_roots={200},
            tracked={},
            parent_pid=lambda _pid: 50,
        )


def test_cleanup_terminates_all_and_only_acceptance_spawned_processes() -> None:
    adapter = _ProcessAdapterStub(
        {100, 200, 300},
        {
            200: (r"C:\CAD\acad.exe", 1_100),
            300: (r"C:\CAD\acad.exe", 1_050),
        },
    )
    terminated: list[tuple[int, ProcessIdentity]] = []

    def terminate(pid: int, identity: ProcessIdentity) -> None:
        terminated.append((pid, identity))
        adapter.active_pids.remove(pid)

    postexisting = _cleanup_acceptance_processes(
        cast(ComAutoCADAdapter, adapter),
        preexisting_pids={100},
        acceptance_started_100ns=1_000,
        ownership_roots={200},
        tracked={},
        terminate=terminate,
        parent_pid={200: 300, 300: 50}.__getitem__,
        timeout_seconds=0.0,
    )

    assert postexisting == {100}
    assert terminated == [
        (200, (r"C:\CAD\acad.exe", 1_100)),
        (300, (r"C:\CAD\acad.exe", 1_050)),
    ]


def test_cleanup_requires_exact_preexisting_process_baseline() -> None:
    adapter = _ProcessAdapterStub(set(), {})

    with pytest.raises(
        AssertionError,
        match=r"missing=\[100\], leaked=\[\]",
    ):
        _cleanup_acceptance_processes(
            cast(ComAutoCADAdapter, adapter),
            preexisting_pids={100},
            acceptance_started_100ns=1_000,
            ownership_roots=set(),
            tracked={},
            terminate=lambda _pid, _identity: None,
            parent_pid=lambda _pid: 50,
            timeout_seconds=0.0,
        )


def test_cleanup_leaves_unknown_concurrent_autocad_process_untouched() -> None:
    adapter = _ProcessAdapterStub(
        {100, 200, 400},
        {
            200: (r"C:\CAD\acad.exe", 1_100),
            400: (r"C:\CAD\acad.exe", 1_200),
        },
    )
    terminated: list[int] = []

    def terminate(pid: int, _identity: ProcessIdentity) -> None:
        terminated.append(pid)
        adapter.active_pids.remove(pid)

    with pytest.raises(
        AssertionError,
        match=r"unknown_left_untouched=\[400\]",
    ):
        _cleanup_acceptance_processes(
            cast(ComAutoCADAdapter, adapter),
            preexisting_pids={100},
            acceptance_started_100ns=1_000,
            ownership_roots={200},
            tracked={},
            terminate=terminate,
            parent_pid={200: 50, 400: 999}.__getitem__,
            timeout_seconds=0.0,
        )

    assert terminated == [200]
    assert adapter.active_pids == {100, 400}


def test_remediation_acceptance_replays_exact_fake_mcp_transcript() -> None:
    duplicated_entities = [
        {"entity_ref": "original", "layer": "OBJECT"},
        {"entity_ref": "duplicate-2", "layer": "OBJECT"},
        {"entity_ref": "wrong-layer-1", "layer": "CENTER"},
        {"entity_ref": "other", "layer": "OBJECT"},
    ]
    remediated_entities = [
        {"entity_ref": "original", "layer": "OBJECT"},
        {"entity_ref": "wrong-layer-1", "layer": "OBJECT"},
        {"entity_ref": "other", "layer": "OBJECT"},
    ]
    transcript: list[tuple[str, dict[str, Any]]] = [
        (
            "cad_job_create",
            {"job_id": "job-duplicate", "expected_revision": "sha256:duplicate-base"},
        ),
        (
            "cad_spec_submit",
            {"plan_hash": "sha256:duplicate-plan", "operation_count": 2},
        ),
        ("cad_preview", {"artifacts": []}),
        ("cad_validate", {"commit_allowed": True}),
        (
            "cad_commit",
            {
                "status": "committed",
                "entity_results": [{"entity_ref": "duplicate-2"}],
                "new_revision": "sha256:duplicated",
            },
        ),
        (
            "cad_drawing_read",
            {
                "document_id": "doc-live",
                "revision": "sha256:duplicated",
                "entities": duplicated_entities,
            },
        ),
        (
            "cad_audit",
            {
                "audit_id": "audit-duplicated",
                "report": {
                    "findings": [
                        {
                            "rule_id": "DUPLICATE_ENTITY",
                            "entity_ref": "duplicate-2",
                        },
                        {
                            "rule_id": "ENTITY_ON_EXPECTED_LAYER",
                            "entity_ref": "wrong-layer-1",
                            "expected": "OBJECT",
                        },
                    ]
                },
            },
        ),
        (
            "cad_job_create",
            {"job_id": "job-remediation", "expected_revision": "sha256:duplicated"},
        ),
        (
            "cad_change_submit",
            {"plan_hash": "sha256:remediation-plan", "operation_count": 2},
        ),
        (
            "cad_preview",
            {
                "semantic_diff": {
                    "entries": [
                        {
                            "change": "deleted",
                            "target_entity_ref": "duplicate-2",
                        },
                        {
                            "change": "modified",
                            "target_entity_ref": "wrong-layer-1",
                        },
                    ]
                }
            },
        ),
        ("cad_validate", {"commit_allowed": True}),
        ("cad_commit", {"status": "committed", "new_revision": "sha256:remediated"}),
        (
            "cad_drawing_read",
            {
                "document_id": "doc-live",
                "revision": "sha256:remediated",
                "entities": remediated_entities,
            },
        ),
        ("cad_audit", {"audit_id": "audit-remediated", "report": {"findings": []}}),
    ]
    session = _TranscriptSession(transcript)
    approvals: list[tuple[str, str, str]] = []

    def approve(job_id: str, plan_hash: str, revision: str) -> tuple[str, str]:
        approvals.append((job_id, plan_hash, revision))
        return f"approval-{job_id}", f"secret-token-{job_id}"

    evidence = asyncio.run(
        _run_remediation_acceptance(
            cast(Any, session),
            document_id="doc-live",
            spec={"features": [{"feature_type": "base_plate"}]},
            case_name="base-plate",
            first_entity_count=2,
            approve_job=approve,
        )
    )

    assert session.transcript == []
    assert approvals == [
        ("job-duplicate", "sha256:duplicate-plan", "sha256:duplicate-base"),
        ("job-remediation", "sha256:remediation-plan", "sha256:duplicated"),
    ]
    change_call = next(
        arguments for name, arguments in session.calls if name == "cad_change_submit"
    )
    assert change_call["remediation"] == {
        "audit_id": "audit-duplicated",
        "selected_findings": [
            {"rule_id": "DUPLICATE_ENTITY", "entity_ref": "duplicate-2"},
            {
                "rule_id": "ENTITY_ON_EXPECTED_LAYER",
                "entity_ref": "wrong-layer-1",
            },
        ],
    }
    assert evidence["remediation"] == {
        "job_id": "job-remediation",
        "approval_id": "approval-job-remediation",
        "operation_count": 2,
        "diff_counts": {"added": 0, "modified": 1, "deleted": 1},
        "exact_target_match": True,
        "target_ref_sha256_by_change": {
            "deleted": ("sha256:e19b2403e0735c8ef1cbb32af47c9f85847b0c82d90b67b6dc80feb1c947739f"),
            "modified": ("sha256:30463278df48b35b800e68be5f24248bd582a42fdb6964340556f353270e585c"),
        },
        "commit_status": "committed",
        "entity_count_before": 4,
        "entity_count_after": 3,
        "selected_pairs_remaining": 0,
        "layer_corrected": True,
        "post_audit_finding_count": 0,
    }
    serialized = json.dumps(evidence, sort_keys=True)
    assert "duplicate-2" not in serialized
    assert "wrong-layer-1" not in serialized
    assert "secret-token" not in serialized
