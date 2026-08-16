from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest
import scripts.live_mcp_r26_acceptance as live_acceptance
from scripts.live_mcp_r26_acceptance import (
    DurableAcceptanceState,
    ProcessIdentity,
    TrackedProcess,
    _cleanup_acceptance_processes,
    _close_owned_session_preserving_failure,
    _configure_acceptance_bundle_environment,
    _configure_durable_restore_environment,
    _durable_artifact_proof,
    _mcp_server_parameters,
    _run_durable_commit_session,
    _run_durable_rollback_session,
    _run_remediation_acceptance,
    _save_owned_active_dwg,
    _save_owned_as_native_dwg,
    _track_acceptance_processes,
    _trigger_owned_bridge_load,
    _wait_for_owned_com_readiness,
)

from cad_harness.adapters.autocad_com import ComAutoCADAdapter
from cad_harness.application.live_session_proof import LIVE_SESSION_PROOF_ENV
from cad_harness.domain.errors import ApprovalRequiredError


def test_write_mcp_parameters_replace_static_confirmation_with_ephemeral_lsp2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "live.yaml"
    config_path.write_text("adapter:\n  type: dotnet_bridge\n", encoding="utf-8")
    calls: list[tuple[Path, str, str]] = []

    def issue(*, config_path: Path, adapter_type: str, secret: str) -> str:
        calls.append((config_path, adapter_type, secret))
        return "lsp2.current-scope.signature"

    monkeypatch.setattr(live_acceptance, "issue_existing_live_session_proof", issue)
    monkeypatch.setenv("CAD_HARNESS_APPROVAL_SECRET", "ephemeral-test-secret")
    monkeypatch.setenv(
        "CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS", live_acceptance._SETUP_CONFIRMATIONS
    )
    monkeypatch.setenv(LIVE_SESSION_PROOF_ENV, "lsp2.stale-scope.signature")

    parameters = _mcp_server_parameters(config_path)

    assert calls == [(config_path, "dotnet_bridge", "ephemeral-test-secret")]
    assert parameters.env is not None
    assert parameters.env[LIVE_SESSION_PROOF_ENV] == "lsp2.current-scope.signature"
    assert "CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS" not in parameters.env


def test_static_confirmation_alone_cannot_build_write_mcp_parameters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "live.yaml"
    config_path.write_text("adapter:\n  type: dotnet_bridge\n", encoding="utf-8")
    monkeypatch.delenv("CAD_HARNESS_APPROVAL_SECRET", raising=False)
    monkeypatch.setenv(
        "CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS", live_acceptance._SETUP_CONFIRMATIONS
    )
    monkeypatch.setenv(LIVE_SESSION_PROOF_ENV, "lsp2.stale-scope.signature")

    with pytest.raises(ApprovalRequiredError, match="ephemeral approval secret"):
        _mcp_server_parameters(config_path)

    assert "CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS" not in os.environ
    assert LIVE_SESSION_PROOF_ENV not in os.environ


def test_acceptance_bundle_environment_is_workspace_exact_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugins_root = tmp_path / "data" / "live-r26" / "ApplicationPlugins"
    bundle = plugins_root / "AutoCADHarness.bundle"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(
        live_acceptance,
        "_workspace_acceptance_plugins_root",
        lambda: plugins_root.resolve(strict=True),
    )
    monkeypatch.delenv("CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT", raising=False)

    assert _configure_acceptance_bundle_environment() == bundle.resolve(strict=True)
    assert os.environ["CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT"] == str(bundle.resolve(strict=True))

    poisoned = tmp_path / "poisoned" / "AutoCADHarness.bundle"
    poisoned.mkdir(parents=True)
    monkeypatch.setenv("CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT", str(poisoned))
    with pytest.raises(OSError, match="does not match the workspace install"):
        _configure_acceptance_bundle_environment()


def test_durable_restore_environment_is_explicit_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case_root = (tmp_path / "case").resolve()
    monkeypatch.setenv("CAD_HARNESS_DURABLE_RESTORE_VERIFIED", "1")

    checkpoint_root = _configure_durable_restore_environment(case_root, enabled=False)

    assert checkpoint_root == case_root / "bridge-checkpoints"
    assert "CAD_HARNESS_DURABLE_RESTORE_VERIFIED" not in os.environ
    assert os.environ["CAD_HARNESS_CHECKPOINT_ROOT"] == str(checkpoint_root)
    assert os.environ["CAD_HARNESS_COMMIT_JOURNAL_ROOT"] == str(case_root / "commit-journal")

    _configure_durable_restore_environment(case_root, enabled=True)
    assert os.environ["CAD_HARNESS_DURABLE_RESTORE_VERIFIED"] == "1"


def test_native_dwg_helpers_never_accept_or_save_a_different_document(tmp_path: Path) -> None:
    target = tmp_path / "owned.dwg"

    class _Document:
        FullName = ""

        def SaveAs(self, value: str) -> None:  # noqa: N802
            Path(value).write_bytes(b"native-seed")
            self.FullName = value

        def Save(self) -> None:  # noqa: N802
            Path(self.FullName).write_bytes(b"committed-native")

    document = _Document()

    class _Adapter:
        def _require_document(self) -> Any:
            return document

    adapter = cast(ComAutoCADAdapter, _Adapter())
    _save_owned_as_native_dwg(adapter, target)
    assert target.read_bytes() == b"native-seed"
    _save_owned_active_dwg(adapter, target)
    assert target.read_bytes() == b"committed-native"

    document.FullName = str(tmp_path / "different.dwg")
    (tmp_path / "different.dwg").write_bytes(b"preexisting")
    with pytest.raises(AssertionError, match="outside the durable acceptance target"):
        _save_owned_active_dwg(adapter, target)


def test_bridge_loader_uses_only_the_adapter_fixed_netload_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = '_.NETLOAD\n"D:\\workspace\\fixed\\AutoCADHarness.dll"\n'
    monkeypatch.setattr(live_acceptance, "_expected_acceptance_netload_command", lambda: command)

    class _Document:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        def SetVariable(self, name: str, value: Any) -> None:  # noqa: N802
            self.calls.append(("SetVariable", (name, value)))

        def SendCommand(self, value: str) -> None:  # noqa: N802
            self.calls.append(("SendCommand", value))

    document = _Document()
    application = type("Application", (), {"Visible": False})()

    class _Adapter:
        def require_owned_application(self) -> Any:
            return application

        def _require_document(self) -> Any:
            return document

    _trigger_owned_bridge_load(cast(ComAutoCADAdapter, _Adapter()))

    assert application.Visible is True
    assert document.calls == [
        ("SetVariable", ("FILEDIA", 0)),
        ("SendCommand", command),
    ]


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


def _durable_status() -> dict[str, Any]:
    return {
        "adapter": {
            "adapter_type": "dotnet_bridge",
            "available": True,
            "active_document_id": "doc-live",
            "capabilities": ["inspect_document", "commit", "checkpoint_restore"],
        }
    }


def _semantic_model(revision: str, entities: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "document_id": "doc-live",
        "revision": revision,
        "display_name": "owned.dwg",
        "entities": entities,
    }


def test_durable_commit_session_captures_checkpoint_then_saves_without_leaking_token() -> None:
    initial = _semantic_model("sha256:initial", [])
    committed = _semantic_model("sha256:committed", [{"entity_ref": "new-1", "layer": "OBJECT"}])
    transcript = [
        ("cad_status", _durable_status()),
        (
            "cad_document_inspect",
            {
                "document_id": "doc-live",
                "revision": "sha256:initial",
                "display_name": "owned.dwg",
                "path_hash": "sha256:path",
            },
        ),
        ("cad_drawing_read", initial),
        ("cad_job_create", {"job_id": "job-live", "expected_revision": "sha256:initial"}),
        ("cad_spec_submit", {"plan_hash": "sha256:plan", "operation_count": 1}),
        ("cad_preview", {"artifacts": []}),
        ("cad_validate", {"commit_allowed": True}),
        (
            "cad_commit",
            {
                "status": "committed",
                "checkpoint_id": "checkpoint-live",
                "undo_group": "undo-session-a",
                "new_revision": "sha256:committed",
            },
        ),
        ("cad_drawing_read", committed),
    ]
    session = _TranscriptSession(transcript)
    saves: list[str] = []

    evidence, state = asyncio.run(
        _run_durable_commit_session(
            cast(Any, session),
            spec={"features": [{"feature_type": "base_plate"}]},
            case_name="durable",
            expected_display_name="owned.dwg",
            expected_path_hash="sha256:path",
            approve_job=lambda *_args: ("approval-live", "secret-commit-token"),
            persist_document=lambda: saves.append("saved"),
        )
    )

    assert session.transcript == []
    assert saves == ["saved"]
    assert state == DurableAcceptanceState(
        job_id="job-live",
        document_id="doc-live",
        checkpoint_id="checkpoint-live",
        initial_revision="sha256:initial",
        current_revision="sha256:committed",
        initial_model=initial,
    )
    assert "secret-commit-token" not in json.dumps(evidence, sort_keys=True)


def test_durable_rollback_session_uses_exact_rb1_and_requires_semantic_restore() -> None:
    initial = _semantic_model("sha256:initial", [])
    state = DurableAcceptanceState(
        job_id="job-live",
        document_id="doc-live",
        checkpoint_id="checkpoint-live",
        initial_revision="sha256:initial",
        current_revision="sha256:committed",
        initial_model=initial,
    )
    transcript = [
        ("cad_status", _durable_status()),
        (
            "cad_rollback",
            {
                "job_id": "job-live",
                "checkpoint_id": "checkpoint-live",
                "restored_revision": "sha256:initial",
                "method": "checkpoint_restore",
            },
        ),
        (
            "cad_document_inspect",
            {
                "document_id": "doc-live",
                "revision": "sha256:initial",
                "display_name": "owned.dwg",
                "path_hash": "sha256:path",
            },
        ),
        ("cad_drawing_read", initial),
    ]
    session = _TranscriptSession(transcript)

    evidence = asyncio.run(
        _run_durable_rollback_session(
            cast(Any, session),
            state=state,
            rollback_approval_token="rb1.secret-rollback-token",
            expected_display_name="owned.dwg",
            expected_path_hash="sha256:path",
        )
    )

    rollback_arguments = next(args for name, args in session.calls if name == "cad_rollback")
    assert rollback_arguments == {
        "job_id": "job-live",
        "checkpoint_id": "checkpoint-live",
        "current_revision": "sha256:committed",
        "rollback_approval_token": "rb1.secret-rollback-token",
    }
    assert evidence["method"] == "checkpoint_restore"
    assert "secret-rollback-token" not in json.dumps(evidence, sort_keys=True)


def test_durable_artifact_proof_requires_consumed_catalog_and_clean_committed_journal(
    tmp_path: Path,
) -> None:
    checkpoint_root = tmp_path / "bridge-checkpoints"
    durable_root = checkpoint_root / "whole-dwg-checkpoints-v1"
    journal_root = checkpoint_root / "whole-dwg-restore-journal-v1"
    durable_root.mkdir(parents=True)
    journal_root.mkdir()
    target = tmp_path / "owned.dwg"
    checkpoint = durable_root / "checkpoint-live-job.dwg"
    checkpoint_bytes = b"AC1032-restored-checkpoint"
    checkpoint.write_bytes(checkpoint_bytes)
    target.write_bytes(checkpoint_bytes)
    checkpoint_sha = hashlib.sha256(checkpoint_bytes).hexdigest()
    (durable_root / "checkpoint-catalog.v1.json").write_text(
        json.dumps(
            {
                "payload": {
                    "records": [
                        {
                            "checkpoint_id": "checkpoint-live",
                            "checkpoint_file_name": checkpoint.name,
                            "sha256": checkpoint_sha,
                            "byte_length": len(checkpoint_bytes),
                            "state": "consumed",
                        }
                    ]
                },
                "authentication_tag": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    (journal_root / "approval.restore.json").write_text(
        json.dumps(
            {
                "payload": {
                    "state": "committed",
                    "checkpoint_sha256": checkpoint_sha,
                    "protected_target_locator": "ciphertext-only",
                    "stage_file_name": ".cad-harness-restore-a.stage.dwg",
                    "backup_file_name": ".cad-harness-restore-a.backup.dwg",
                },
                "authentication_tag": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    state = DurableAcceptanceState(
        job_id="job-live",
        document_id="doc-live",
        checkpoint_id="checkpoint-live",
        initial_revision="sha256:initial",
        current_revision="sha256:committed",
        initial_model={},
    )

    evidence = _durable_artifact_proof(
        checkpoint_root,
        state=state,
        target=target,
        rollback_approval_token="rb1.never-persist-this",
    )

    assert evidence["catalog_state"] == "consumed"
    assert evidence["restore_journal_state"] == "committed"
    assert evidence["stage_or_backup_leftover_count"] == 0
    serialized = json.dumps(evidence, sort_keys=True)
    assert str(target) not in serialized
    assert "never-persist-this" not in serialized


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
