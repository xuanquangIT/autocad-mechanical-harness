"""End-to-end MCP STDIO acceptance on a PID-owned AutoCAD 2027 scratch drawing.

This entrypoint is intentionally destructive only inside ``--work-root``. It launches a
new version-specific AutoCAD process, proves its PID ownership, opens a generated scratch
DXF, and sends every harness operation through a real MCP stdio session. Approval remains
outside MCP: the runner invokes the same human-only service boundary after inspecting the
preview and validation report, then supplies that short-lived token to ``cad_commit``.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import secrets
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import ezdxf
from apps.mcp_server.context import build_context
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from cad_harness.adapters.autocad_com import (
    ComAutoCADAdapter,
    _expected_acceptance_netload_command,
    _workspace_acceptance_plugins_root,
)
from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter
from cad_harness.application.manual_gate import LIVE_SETUP_STEPS
from cad_harness.domain.models.envelope import ToolResponse
from cad_harness.domain.models.validation import Severity
from cad_harness.domain.ports.autocad_adapter import InspectRequest, RollbackRequest

_SETUP_CONFIRMATIONS = ",".join(step.value for step in LIVE_SETUP_STEPS)
_PROFILE_LAYERS = ("OBJECT", "DIM", "CENTER", "TEXT", "HATCH")
_DURABLE_CAPABILITY = "checkpoint_restore"
_DURABLE_CHECKPOINT_DIRECTORY = "whole-dwg-checkpoints-v1"
_DURABLE_RESTORE_JOURNAL_DIRECTORY = "whole-dwg-restore-journal-v1"


@dataclass(frozen=True, slots=True)
class DurableAcceptanceState:
    """Private cross-session facts; approval secrets never enter acceptance evidence."""

    job_id: str
    document_id: str
    checkpoint_id: str
    initial_revision: str
    current_revision: str
    initial_model: dict[str, Any]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bridge_path_hash(path: Path) -> str:
    normalized = str(path.resolve()).replace("\\", "/").strip().upper()
    return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def _required_acceptance_bundle_root() -> Path:
    """Return the one workspace-local bundle that the isolated helper may NETLOAD."""
    plugins_root = _workspace_acceptance_plugins_root()
    bundle_root = (plugins_root / "AutoCADHarness.bundle").resolve(strict=True)
    if bundle_root.parent != plugins_root or bundle_root.name != "AutoCADHarness.bundle":
        raise OSError("Acceptance bundle did not resolve to the dedicated workspace install root")
    return bundle_root


def _configure_acceptance_bundle_environment() -> Path:
    """Bind the helper's inherited environment before its spawned process starts."""
    bundle_root = _required_acceptance_bundle_root()
    configured = os.environ.get("CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT")
    if configured is not None and Path(configured).resolve(strict=True) != bundle_root:
        raise OSError("CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT does not match the workspace install")
    os.environ["CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT"] = str(bundle_root)
    return bundle_root


def _configure_durable_restore_environment(case_root: Path, *, enabled: bool) -> Path:
    """Set the whole-DWG bridge gate before AutoCAD inherits the environment."""
    checkpoint_root = (case_root / "bridge-checkpoints").resolve()
    os.environ["CAD_HARNESS_CHECKPOINT_ROOT"] = str(checkpoint_root)
    os.environ["CAD_HARNESS_COMMIT_JOURNAL_ROOT"] = str((case_root / "commit-journal").resolve())
    if enabled:
        os.environ["CAD_HARNESS_DURABLE_RESTORE_VERIFIED"] = "1"
    else:
        os.environ.pop("CAD_HARNESS_DURABLE_RESTORE_VERIFIED", None)
    return checkpoint_root


def _safe_case_name(value: str) -> str:
    if not value or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in value
    ):
        raise ValueError("case-name must use lowercase ASCII letters, digits, '-' or '_'")
    return value


def _load_spec(path: Path) -> dict[str, Any]:
    payload = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Drawing spec must be one JSON object")
    return {key: value for key, value in payload.items() if not key.startswith("_")}


def _create_empty_scratch(path: Path) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    drawing = ezdxf.new("R2018")
    drawing.units = 4  # millimetres
    for layer in _PROFILE_LAYERS:
        drawing.layers.add(layer)
    drawing.styles.add("DEMO-ISO", font="txt")
    drawing.dimstyles.new("DEMO-ISO-MM")
    drawing.saveas(path)


def _save_owned_as_native_dwg(com: ComAutoCADAdapter, target: Path) -> None:
    """Convert the generated DXF to one canonical, local, writable DWG before NETLOAD."""
    if target.suffix.casefold() != ".dwg" or target.exists():
        raise FileExistsError("The durable acceptance target must be one new DWG")
    target = target.resolve()
    document = com._require_document()
    document.SaveAs(str(target))
    if not target.is_file():
        raise AssertionError("AutoCAD did not persist the native durable acceptance DWG")
    active_path = Path(str(document.FullName)).resolve(strict=True)
    if active_path != target:
        raise AssertionError("AutoCAD did not keep the generated native DWG active")


def _save_owned_active_dwg(com: ComAutoCADAdapter, target: Path) -> None:
    """Persist committed content only to the already-bound acceptance-owned native DWG."""
    target = target.resolve(strict=True)
    document = com._require_document()
    active_path = Path(str(document.FullName)).resolve(strict=True)
    if active_path != target:
        raise AssertionError("Refusing to save a document outside the durable acceptance target")
    document.Save()
    if not target.is_file():
        raise AssertionError("AutoCAD did not persist the committed native DWG")


def _semantic_model_sha256(model: dict[str, Any]) -> str:
    canonical = json.dumps(
        model,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _tool_payload(result: Any) -> dict[str, Any]:
    payload = result.structuredContent
    if payload is None:
        text_blocks = [block.text for block in result.content if hasattr(block, "text")]
        if len(text_blocks) != 1:
            raise AssertionError("MCP response did not contain one structured envelope")
        payload = json.loads(text_blocks[0])
    response = ToolResponse.model_validate(payload)
    return response.model_dump(mode="json", exclude_none=True)


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = _tool_payload(await session.call_tool(name, arguments))
    if payload["status"] != "ok":
        error = payload.get("error", {})
        diagnostic: dict[str, Any] | None = None
        if name in {"cad_commit", "cad_drawing_read", "cad_rollback"}:
            diagnostic = _tool_payload(await session.call_tool("cad_status", {}))
        raise AssertionError(
            f"{name} failed: {json.dumps(error, sort_keys=True, ensure_ascii=True)}; "
            f"status={json.dumps(diagnostic, sort_keys=True, ensure_ascii=True)}"
        )
    return payload


def _mcp_server_parameters() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.mcp_server"],
        cwd=str(Path.cwd()),
        env=dict(os.environ),
        encoding="utf-8",
        encoding_error_handler="strict",
    )


@asynccontextmanager
async def _mcp_session() -> AsyncIterator[ClientSession]:
    """Own one MCP subprocess; leaving this context proves its service memory is gone."""
    async with stdio_client(_mcp_server_parameters()) as (reader, writer):  # noqa: SIM117
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            yield session


def _require_live_durable_adapter(status: dict[str, Any]) -> dict[str, Any]:
    adapter = status["data"]["adapter"]
    capabilities = adapter.get("capabilities")
    if (
        adapter.get("adapter_type") != "dotnet_bridge"
        or adapter.get("available") is not True
        or not isinstance(capabilities, list)
        or _DURABLE_CAPABILITY not in capabilities
    ):
        raise AssertionError(
            "The explicit durable acceptance requires a live dotnet_bridge advertising "
            "checkpoint_restore"
        )
    return cast(dict[str, Any], adapter)


def _wait_for_owned_com_readiness(
    com: ComAutoCADAdapter,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.25,
) -> None:
    """Wait until the PID-owned automation object's document API is callable.

    ``DispatchEx`` can expose a valid HWND before AutoCAD has finished publishing
    its ``Documents`` collection through automation.  Probe the exact collection
    and method needed by ``open_owned_document`` without invoking ``Open``: retrying
    the write-capable call itself could open the scratch drawing twice after a late
    COM response.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while True:
        try:
            app = com._require_current_owned_application()
            documents = app.Documents
            int(documents.Count)
            open_document = documents.Open
            if not callable(open_document):
                raise AttributeError("AutoCAD Documents.Open is not callable")
            return
        except Exception as exc:  # AutoCAD publishes automation members asynchronously.
            last_error = exc
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "PID-owned AutoCAD document automation did not become ready"
            ) from last_error
        time.sleep(poll_seconds)


def _close_owned_session_preserving_failure(
    com: ComAutoCADAdapter,
    original_failure: BaseException | None,
) -> None:
    """Close the disposable session without replacing an in-flight acceptance error."""
    try:
        com.close_owned_session()
    except Exception:
        if original_failure is None:
            raise


ProcessIdentity = tuple[str, int]
TrackedProcess = tuple[str, int, int]


def _process_parent_pid(pid: int) -> int:
    """Return the Windows parent PID captured in the system process snapshot."""
    if sys.platform != "win32":
        raise OSError("AutoCAD process ancestry requires Windows")

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    create_snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot
    snapshot = create_snapshot(0x00000002, 0)
    invalid_handle_value = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle_value:
        raise OSError("Could not snapshot Windows processes for ancestry verification")
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not ctypes.windll.kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise OSError("Could not enumerate Windows processes for ancestry verification")
        while True:
            if int(entry.th32ProcessID) == pid:
                return int(entry.th32ParentProcessID)
            if not ctypes.windll.kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        ctypes.windll.kernel32.CloseHandle(snapshot)
    raise OSError(f"Could not find AutoCAD PID {pid} in the process snapshot")


def _track_acceptance_processes(
    com: ComAutoCADAdapter,
    *,
    preexisting_pids: set[int],
    acceptance_started_100ns: int,
    ownership_roots: set[int],
    tracked: dict[int, TrackedProcess],
    parent_pid: Callable[[int], int] = _process_parent_pid,
) -> set[int]:
    """Track only post-snapshot AutoCAD processes connected to a proven COM PID."""
    current_pids = com._acad_process_ids()
    candidates: dict[int, TrackedProcess] = {}
    for pid in current_pids - preexisting_pids:
        try:
            identity = com._process_identity(pid)
        except OSError:
            if pid not in com._acad_process_ids():
                continue
            raise
        image_path, creation_time_100ns = identity
        candidates[pid] = (image_path, creation_time_100ns, parent_pid(pid))

    all_records = {**tracked, **candidates}
    acceptance_pids = set(tracked) | ownership_roots
    while True:
        connected: set[int] = set()
        for pid, (_image, created, process_parent) in candidates.items():
            if pid in acceptance_pids:
                connected.add(pid)
                continue
            parent_record = all_records.get(process_parent)
            if (
                process_parent in acceptance_pids
                and parent_record is not None
                and parent_record[1] <= created
            ):
                connected.add(pid)
                continue
            if any(
                child_pid in acceptance_pids
                and child_record[2] == pid
                and created <= child_record[1]
                for child_pid, child_record in all_records.items()
            ):
                connected.add(pid)
        expanded = acceptance_pids | connected
        if expanded == acceptance_pids:
            break
        acceptance_pids = expanded

    for pid in sorted(acceptance_pids & candidates.keys()):
        image_path, creation_time_100ns, process_parent = candidates[pid]
        if (
            Path(image_path).name.casefold() != "acad.exe"
            or creation_time_100ns < acceptance_started_100ns
        ):
            raise AssertionError(f"Unowned post-snapshot AutoCAD process identity: {pid}")
        record = (image_path, creation_time_100ns, process_parent)
        previous = tracked.setdefault(pid, record)
        if previous != record:
            raise AssertionError(f"AutoCAD PID identity changed during acceptance: {pid}")
    return current_pids


def _terminate_exact_process(pid: int, expected_identity: ProcessIdentity) -> None:
    """Terminate one acceptance-owned process after revalidating identity on its handle."""
    if sys.platform != "win32":
        raise OSError("AutoCAD process cleanup requires Windows")
    process_terminate = 0x0001
    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_terminate | process_query_limited_information | synchronize,
        False,
        wintypes.DWORD(pid),
    )
    if not handle:
        raise OSError(f"Could not open acceptance-owned AutoCAD PID {pid}")
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise OSError(f"Could not read creation time for AutoCAD PID {pid}")
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(capacity)
        ):
            raise OSError(f"Could not read image path for AutoCAD PID {pid}")
        creation_time_100ns = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        actual_identity = (buffer.value, creation_time_100ns)
        if actual_identity != expected_identity:
            raise AssertionError(f"Refusing to terminate reused or changed AutoCAD PID {pid}")
        if not ctypes.windll.kernel32.TerminateProcess(handle, 1):
            raise OSError(f"Could not terminate acceptance-owned AutoCAD PID {pid}")
        wait_result = ctypes.windll.kernel32.WaitForSingleObject(handle, 10_000)
        if wait_result != 0:
            raise TimeoutError(f"Acceptance-owned AutoCAD PID {pid} did not terminate")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _cleanup_acceptance_processes(
    com: ComAutoCADAdapter,
    *,
    preexisting_pids: set[int],
    acceptance_started_100ns: int,
    ownership_roots: set[int],
    tracked: dict[int, TrackedProcess],
    terminate: Callable[[int, ProcessIdentity], None] = _terminate_exact_process,
    parent_pid: Callable[[int], int] = _process_parent_pid,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.1,
) -> set[int]:
    """Remove only identity-proven processes spawned after the acceptance snapshot."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        current_pids = _track_acceptance_processes(
            com,
            preexisting_pids=preexisting_pids,
            acceptance_started_100ns=acceptance_started_100ns,
            ownership_roots=ownership_roots,
            tracked=tracked,
            parent_pid=parent_pid,
        )
        for pid in sorted((current_pids - preexisting_pids) & tracked.keys()):
            record = tracked[pid]
            identity = (record[0], record[1])
            try:
                terminate(pid, identity)
            except OSError:
                if pid in com._acad_process_ids():
                    raise
        postexisting_pids = com._acad_process_ids()
        if postexisting_pids == preexisting_pids:
            return postexisting_pids
        unknown_pids = sorted((postexisting_pids - preexisting_pids) - tracked.keys())
        if unknown_pids:
            missing = sorted(preexisting_pids - postexisting_pids)
            raise AssertionError(
                "AutoCAD process baseline was not restored; "
                f"missing={missing}, unknown_left_untouched={unknown_pids}"
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_seconds)
    missing = sorted(preexisting_pids - postexisting_pids)
    leaked = sorted(postexisting_pids - preexisting_pids)
    raise AssertionError(
        f"AutoCAD process baseline was not restored; missing={missing}, leaked={leaked}"
    )


def _trigger_owned_bridge_load(com: ComAutoCADAdapter) -> None:
    """Load the exact installed R26 bridge DLL in the owned process.

    AutoCAD's PID-owned ``/Automation`` startup does not run package startup loading.
    This acceptance-only setup command uses a fixed bundle location and cannot contain
    geometry or user-derived input.  AutoCAD's existing SECURELOAD policy remains in
    force; the runner never changes trusted paths or security settings.
    """
    app = com.require_owned_application()
    app.Visible = True
    document = com._require_document()
    # Scope the dialog suppression to this PID-owned disposable document.  Without
    # it AutoCAD's asynchronous SendCommand stops at the NETLOAD file picker and
    # never consumes the fixed DLL path that follows.
    document.SetVariable("FILEDIA", 0)
    # NETLOAD is the only setup command. Bridge health is probed through the real MCP
    # cad_status tool after this command has returned to AutoCAD's idle context; queuing
    # a second modal diagnostic here can keep ExecuteInCommandContextAsync unavailable.
    document.SendCommand(_expected_acceptance_netload_command())


ApprovalIssuer = Callable[[str, str, str], tuple[str, str]]


def _issue_live_approval(
    config_path: Path,
    job_id: str,
    plan_hash: str,
    expected_revision: str,
) -> tuple[str, str]:
    """Issue approval through the same human-only service boundary used by Desktop."""
    approval_context = build_context(
        config_path,
        manual_confirmations=LIVE_SETUP_STEPS,
    )
    report = approval_context.service.store.get_validation(job_id)
    if report is None:
        raise AssertionError("MCP validation evidence was not persisted")
    warning_ids = tuple(
        sorted(
            {finding.rule_id for finding in report.findings if finding.severity is Severity.WARNING}
        )
    )
    return approval_context.service.approve(
        job_id,
        approved_by="live-r26-acceptance-engineer",
        warnings_acknowledged=warning_ids,
        displayed_plan_hash=plan_hash,
        displayed_revision=expected_revision,
    )


def _issue_live_rollback_approval(
    config_path: Path,
    expected_scope: dict[str, str],
) -> tuple[str, str]:
    """Create rb1 through a fresh human-only context after MCP session A exits."""
    approval_context = build_context(
        config_path,
        manual_confirmations=LIVE_SETUP_STEPS,
    )
    scope = approval_context.service.rollback_scope(expected_scope["job_id"])
    if scope != expected_scope:
        raise AssertionError("The durable rollback scope changed before human approval")
    approval, token = approval_context.service.approve_rollback(
        scope["job_id"],
        approved_by="live-r26-durable-rollback-engineer",
        displayed_checkpoint_id=scope["checkpoint_id"],
        displayed_current_revision=scope["current_revision"],
    )
    if not token.startswith("rb1."):
        raise AssertionError("The human-only rollback boundary did not issue an rb1 token")
    return approval.approval_id, token


def _active_drawing_read_arguments(document_id: str) -> dict[str, Any]:
    return {
        "request": {
            "source": {
                "kind": "active_document",
                "format": "dwg",
                "ref": document_id,
            },
            "scope": {"kind": "model_space"},
            "max_entities": 10000,
            "max_block_nesting_depth": 5,
            "include_geometry": True,
        }
    }


async def _run_durable_commit_session(
    session: ClientSession,
    *,
    spec: dict[str, Any],
    case_name: str,
    expected_display_name: str,
    expected_path_hash: str,
    approve_job: ApprovalIssuer,
    persist_document: Callable[[], None],
) -> tuple[dict[str, Any], DurableAcceptanceState]:
    """Commit once while preserving the exact initial semantic checkpoint evidence."""
    adapter = _require_live_durable_adapter(await _call(session, "cad_status", {}))
    inspected = await _call(session, "cad_document_inspect", {})
    if (
        adapter.get("active_document_id") != inspected["data"]["document_id"]
        or inspected["data"]["display_name"].casefold() != expected_display_name.casefold()
        or inspected["data"]["path_hash"] != expected_path_hash
    ):
        raise AssertionError("Durable session A is not bound to the owned native scratch DWG")

    document_id = inspected["data"]["document_id"]
    initial_revision = inspected["data"]["revision"]
    initial_read = await _call(
        session,
        "cad_drawing_read",
        _active_drawing_read_arguments(document_id),
    )
    initial_model = initial_read["data"]
    if initial_model["document_id"] != document_id or initial_model["revision"] != initial_revision:
        raise AssertionError("Initial semantic readback disagrees with the inspected native DWG")

    created = await _call(session, "cad_job_create", {"document_id": document_id})
    if created["data"]["expected_revision"] != initial_revision:
        raise AssertionError("Durable job did not bind the exact initial revision")
    job_id = created["data"]["job_id"]
    submitted = await _call(session, "cad_spec_submit", {"job_id": job_id, "spec": spec})
    plan_hash = submitted["data"]["plan_hash"]
    previewed = await _call(session, "cad_preview", {"job_id": job_id})
    validated = await _call(
        session,
        "cad_validate",
        {"job_id": job_id, "stage": "pre_commit"},
    )
    if not validated["data"]["commit_allowed"]:
        raise AssertionError("Durable checkpoint plan did not pass pre-commit validation")
    approval_id, approval_token = approve_job(job_id, plan_hash, initial_revision)
    committed = await _call(
        session,
        "cad_commit",
        {
            "job_id": job_id,
            "idempotency_key": f"live-mcp-durable-{case_name}-{secrets.token_hex(8)}",
            "expected_revision": initial_revision,
            "plan_hash": plan_hash,
            "approval_token": approval_token,
        },
    )
    commit = committed["data"]
    checkpoint_id = commit.get("checkpoint_id")
    undo_group = commit.get("undo_group")
    current_revision = commit.get("new_revision")
    if (
        commit.get("status") != "committed"
        or not isinstance(checkpoint_id, str)
        or not checkpoint_id
        or not isinstance(undo_group, str)
        or not undo_group
        or not isinstance(current_revision, str)
        or current_revision == initial_revision
    ):
        raise AssertionError("Durable commit returned incomplete checkpoint or revision evidence")

    committed_read = await _call(
        session,
        "cad_drawing_read",
        _active_drawing_read_arguments(document_id),
    )
    committed_model = committed_read["data"]
    if (
        committed_model["document_id"] != document_id
        or committed_model["revision"] != current_revision
        or committed_model == initial_model
    ):
        raise AssertionError("Committed semantic readback did not prove the intended mutation")
    persist_document()

    evidence = {
        "adapter": adapter,
        "job_id": job_id,
        "plan_hash": plan_hash,
        "operation_count": submitted["data"]["operation_count"],
        "preview_artifact_count": len(previewed["data"]["artifacts"]),
        "approval_id": approval_id,
        "checkpoint_id": checkpoint_id,
        "initial_revision": initial_revision,
        "committed_revision": current_revision,
        "initial_semantic_sha256": _semantic_model_sha256(initial_model),
        "committed_semantic_sha256": _semantic_model_sha256(committed_model),
        "committed_entity_count": len(committed_model["entities"]),
        "session_undo_receipt_created": True,
    }
    return evidence, DurableAcceptanceState(
        job_id=job_id,
        document_id=document_id,
        checkpoint_id=checkpoint_id,
        initial_revision=initial_revision,
        current_revision=current_revision,
        initial_model=initial_model,
    )


async def _run_durable_rollback_session(
    session: ClientSession,
    *,
    state: DurableAcceptanceState,
    rollback_approval_token: str,
    expected_display_name: str,
    expected_path_hash: str,
) -> dict[str, Any]:
    """Consume rb1 in a second MCP process, forcing undo_group=None at the service boundary."""
    _require_live_durable_adapter(await _call(session, "cad_status", {}))
    rolled_back = await _call(
        session,
        "cad_rollback",
        {
            "job_id": state.job_id,
            "checkpoint_id": state.checkpoint_id,
            "current_revision": state.current_revision,
            "rollback_approval_token": rollback_approval_token,
        },
    )
    rollback = rolled_back["data"]
    if (
        rollback.get("method") != _DURABLE_CAPABILITY
        or rollback.get("checkpoint_id") != state.checkpoint_id
        or rollback.get("restored_revision") != state.initial_revision
    ):
        raise AssertionError("MCP session B did not perform the exact checkpoint_restore")

    inspected = await _call(session, "cad_document_inspect", {})
    if (
        inspected["data"]["document_id"] != state.document_id
        or inspected["data"]["revision"] != state.initial_revision
        or inspected["data"]["display_name"].casefold() != expected_display_name.casefold()
        or inspected["data"]["path_hash"] != expected_path_hash
    ):
        raise AssertionError("Whole-file reopen did not restore the exact native document identity")
    restored_read = await _call(
        session,
        "cad_drawing_read",
        _active_drawing_read_arguments(state.document_id),
    )
    restored_model = restored_read["data"]
    if restored_model != state.initial_model:
        raise AssertionError("Whole-file rollback did not restore the exact semantic readback")
    return {
        "method": rollback["method"],
        "checkpoint_id": rollback["checkpoint_id"],
        "restored_revision": rollback["restored_revision"],
        "restored_semantic_sha256": _semantic_model_sha256(restored_model),
        "restored_entity_count": len(restored_model["entities"]),
        "document_identity_preserved": True,
    }


def _run_direct_durable_replay(
    config_path: Path,
    *,
    state: DurableAcceptanceState,
    rollback_approval_token: str,
) -> dict[str, Any]:
    """Replay the exact rb1 directly against a fresh bridge adapter, bypassing job state."""
    replay_context = build_context(
        config_path,
        manual_confirmations=LIVE_SETUP_STEPS,
    )
    adapter = replay_context.service.adapter
    if not isinstance(adapter, DotNetBridgeAdapter):
        raise AssertionError("Durable replay did not construct a DotNetBridgeAdapter")
    result = adapter.rollback(
        RollbackRequest(
            job_id=state.job_id,
            document_id=state.document_id,
            checkpoint_id=state.checkpoint_id,
            current_revision=state.current_revision,
            rollback_approval_token=rollback_approval_token,
            undo_group=None,
        )
    )
    if (
        result.method != _DURABLE_CAPABILITY
        or result.checkpoint_id != state.checkpoint_id
        or result.restored_revision != state.initial_revision
    ):
        raise AssertionError("Direct durable replay returned different rollback evidence")
    inspected = adapter.inspect_document(InspectRequest(document_id=state.document_id))
    if inspected.document_id != state.document_id or inspected.revision != state.initial_revision:
        raise AssertionError("Direct durable replay changed the restored document")
    return {
        "method": result.method,
        "checkpoint_id": result.checkpoint_id,
        "restored_revision": result.restored_revision,
        "document_identity_preserved": True,
    }


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("Durable acceptance artifact is not one JSON object")
    return value


def _durable_catalog_checkpoint(
    checkpoint_root: Path,
    checkpoint_id: str,
    *,
    expected_state: str,
) -> tuple[Path, str]:
    durable_root = checkpoint_root / _DURABLE_CHECKPOINT_DIRECTORY
    catalog_path = durable_root / "checkpoint-catalog.v1.json"
    catalog = _json_object(catalog_path)
    payload = catalog.get("payload")
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise AssertionError("Durable checkpoint catalog has no authenticated record list")
    matches = [
        record
        for record in records
        if isinstance(record, dict) and record.get("checkpoint_id") == checkpoint_id
    ]
    if len(matches) != 1 or matches[0].get("state") != expected_state:
        raise AssertionError("Durable checkpoint catalog state does not match acceptance phase")
    record = matches[0]
    file_name = record.get("checkpoint_file_name")
    expected_sha256 = record.get("sha256")
    if (
        not isinstance(file_name, str)
        or Path(file_name).name != file_name
        or not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or not isinstance(catalog.get("authentication_tag"), str)
    ):
        raise AssertionError("Durable checkpoint catalog record is incomplete")
    checkpoint_path = (durable_root / file_name).resolve(strict=True)
    if checkpoint_path.parent != durable_root.resolve(strict=True):
        raise AssertionError("Durable checkpoint artifact escaped its direct-child root")
    if _sha256(checkpoint_path) != expected_sha256 or checkpoint_path.stat().st_size != record.get(
        "byte_length"
    ):
        raise AssertionError("Durable checkpoint artifact does not match its catalog facts")
    return checkpoint_path, expected_sha256


def _durable_artifact_proof(
    checkpoint_root: Path,
    *,
    state: DurableAcceptanceState,
    target: Path,
    rollback_approval_token: str,
) -> dict[str, Any]:
    checkpoint_path, checkpoint_sha256 = _durable_catalog_checkpoint(
        checkpoint_root,
        state.checkpoint_id,
        expected_state="consumed",
    )
    journal_root = checkpoint_root / _DURABLE_RESTORE_JOURNAL_DIRECTORY
    journals = sorted(journal_root.glob("*.restore.json"))
    if len(journals) != 1:
        raise AssertionError("Durable acceptance expected exactly one restore journal")
    raw_journal = journals[0].read_text(encoding="utf-8")
    target_text = str(target.resolve(strict=True))
    if (
        rollback_approval_token in raw_journal
        or target_text in raw_journal
        or target_text.replace("\\", "/") in raw_journal
    ):
        raise AssertionError("Durable journal exposed an approval token or raw target path")
    journal = json.loads(raw_journal)
    if not isinstance(journal, dict):
        raise AssertionError("Durable restore journal is not one JSON object")
    payload = journal.get("payload")
    if (
        not isinstance(payload, dict)
        or payload.get("state") != "committed"
        or payload.get("checkpoint_sha256") != checkpoint_sha256
        or not isinstance(payload.get("protected_target_locator"), str)
        or not payload["protected_target_locator"]
        or not isinstance(journal.get("authentication_tag"), str)
    ):
        raise AssertionError("Durable restore journal lacks committed authenticated evidence")
    forbidden_keys = {
        "approval_token",
        "rollback_approval_token",
        "original_path",
        "target_path",
    }
    if forbidden_keys & payload.keys():
        raise AssertionError("Durable restore journal persisted a forbidden secret or path field")
    stage_name = payload.get("stage_file_name")
    backup_name = payload.get("backup_file_name")
    if not isinstance(stage_name, str) or not isinstance(backup_name, str):
        raise AssertionError("Durable restore journal lacks bounded cleanup artifact names")
    target_parent = target.parent.resolve(strict=True)
    leftovers = [
        candidate
        for candidate in (target_parent / stage_name, target_parent / backup_name)
        if candidate.exists()
    ]
    leftovers.extend(target_parent.glob(".cad-harness-restore-*.stage.dwg"))
    leftovers.extend(target_parent.glob(".cad-harness-restore-*.backup.dwg"))
    if leftovers:
        raise AssertionError("Committed durable rollback left stage or backup artifacts")
    target_sha256 = _sha256(target)
    if target_sha256 != checkpoint_sha256 or _sha256(checkpoint_path) != target_sha256:
        raise AssertionError("Restored target bytes do not match the consumed checkpoint")
    return {
        "catalog_state": "consumed",
        "restore_journal_state": "committed",
        "checkpoint_sha256": checkpoint_sha256,
        "target_sha256": target_sha256,
        "catalog_authentication_tag_present": True,
        "restore_journal_authentication_tag_present": True,
        "target_locator_protected": True,
        "stage_or_backup_leftover_count": 0,
    }


async def _run_durable_checkpoint_restore_workflow(
    *,
    config_path: Path,
    spec: dict[str, Any],
    case_name: str,
    native_dwg: Path,
    checkpoint_root: Path,
    persist_document: Callable[[], None],
) -> dict[str, Any]:
    expected_display_name = native_dwg.name
    expected_path_hash = _bridge_path_hash(native_dwg)
    async with _mcp_session() as session_a:
        commit_evidence, state = await _run_durable_commit_session(
            session_a,
            spec=spec,
            case_name=case_name,
            expected_display_name=expected_display_name,
            expected_path_hash=expected_path_hash,
            approve_job=lambda job_id, plan_hash, revision: _issue_live_approval(
                config_path,
                job_id,
                plan_hash,
                revision,
            ),
            persist_document=persist_document,
        )

    # Session A is fully closed here. The next service reads the same SQLite job but
    # cannot possess its process-local undo receipt, so rollback must select checkpoint_restore.
    post_commit_sha256 = _sha256(native_dwg)
    checkpoint_path, checkpoint_sha256 = _durable_catalog_checkpoint(
        checkpoint_root,
        state.checkpoint_id,
        expected_state="available",
    )
    if post_commit_sha256 == checkpoint_sha256:
        raise AssertionError("Persisted post-commit DWG still matches its pre-commit checkpoint")
    expected_scope = {
        "job_id": state.job_id,
        "document_id": state.document_id,
        "checkpoint_id": state.checkpoint_id,
        "current_revision": state.current_revision,
    }
    rollback_approval_id, rollback_token = _issue_live_rollback_approval(
        config_path,
        expected_scope,
    )
    async with _mcp_session() as session_b:
        rollback_evidence = await _run_durable_rollback_session(
            session_b,
            state=state,
            rollback_approval_token=rollback_token,
            expected_display_name=expected_display_name,
            expected_path_hash=expected_path_hash,
        )

    restored_sha256 = _sha256(native_dwg)
    if restored_sha256 != checkpoint_sha256:
        raise AssertionError("Whole-file rollback target hash differs from the checkpoint")
    replay_evidence = _run_direct_durable_replay(
        config_path,
        state=state,
        rollback_approval_token=rollback_token,
    )
    replay_sha256 = _sha256(native_dwg)
    if replay_sha256 != restored_sha256:
        raise AssertionError("Idempotent durable replay mutated the restored DWG")
    artifact_evidence = _durable_artifact_proof(
        checkpoint_root,
        state=state,
        target=native_dwg,
        rollback_approval_token=rollback_token,
    )
    return {
        "mode": "durable_checkpoint_restore",
        "session_a": commit_evidence,
        "mcp_server_restart_removed_session_undo_receipt": True,
        "rollback_approval_id": rollback_approval_id,
        "session_b": rollback_evidence,
        "direct_adapter_replay": replay_evidence,
        "post_commit_target_sha256": post_commit_sha256,
        "restored_target_sha256": restored_sha256,
        "replay_target_sha256": replay_sha256,
        "checkpoint_artifact_bytes": checkpoint_path.stat().st_size,
        "artifacts": artifact_evidence,
    }


def _redacted_entity_ref(entity_ref: str) -> str:
    return f"sha256:{hashlib.sha256(entity_ref.encode('utf-8')).hexdigest()}"


def _select_remediation_findings(
    audit_data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    findings = audit_data["report"]["findings"]
    duplicates = sorted(
        (
            finding
            for finding in findings
            if finding["rule_id"] == "DUPLICATE_ENTITY" and finding.get("entity_ref")
        ),
        key=lambda finding: finding["entity_ref"],
    )
    if not duplicates:
        raise AssertionError("The second identical spec commit produced no duplicate finding")
    duplicate = duplicates[0]
    wrong_layers = sorted(
        (
            finding
            for finding in findings
            if finding["rule_id"] == "ENTITY_ON_EXPECTED_LAYER"
            and finding.get("entity_ref")
            and finding["entity_ref"] != duplicate["entity_ref"]
            and isinstance(finding.get("expected"), str)
        ),
        key=lambda finding: finding["entity_ref"],
    )
    if not wrong_layers:
        raise AssertionError(
            "Audit produced no expected-layer finding on a different entity reference"
        )
    return duplicate, wrong_layers[0]


async def _run_remediation_acceptance(
    session: ClientSession,
    *,
    document_id: str,
    spec: dict[str, Any],
    case_name: str,
    first_entity_count: int,
    approve_job: ApprovalIssuer,
) -> dict[str, Any]:
    """Create deterministic defects, remediate two exact findings, and re-audit."""
    duplicate_job = await _call(session, "cad_job_create", {"document_id": document_id})
    duplicate_job_id = duplicate_job["data"]["job_id"]
    duplicate_expected_revision = duplicate_job["data"]["expected_revision"]
    duplicate_plan = await _call(
        session,
        "cad_spec_submit",
        {"job_id": duplicate_job_id, "spec": spec},
    )
    duplicate_plan_hash = duplicate_plan["data"]["plan_hash"]
    await _call(session, "cad_preview", {"job_id": duplicate_job_id})
    duplicate_validation = await _call(
        session,
        "cad_validate",
        {"job_id": duplicate_job_id, "stage": "pre_commit"},
    )
    if not duplicate_validation["data"]["commit_allowed"]:
        raise AssertionError("Duplicate-seeding plan did not pass pre-commit validation")
    duplicate_approval_id, duplicate_token = approve_job(
        duplicate_job_id,
        duplicate_plan_hash,
        duplicate_expected_revision,
    )
    duplicate_commit = await _call(
        session,
        "cad_commit",
        {
            "job_id": duplicate_job_id,
            "idempotency_key": f"live-mcp-duplicate-{case_name}-{secrets.token_hex(8)}",
            "expected_revision": duplicate_expected_revision,
            "plan_hash": duplicate_plan_hash,
            "approval_token": duplicate_token,
        },
    )
    duplicate_commit_data = duplicate_commit["data"]
    if (
        duplicate_commit_data["status"] != "committed"
        or not duplicate_commit_data["entity_results"]
    ):
        raise AssertionError("Duplicate-seeding commit created no live entities")

    duplicated_read = await _call(
        session,
        "cad_drawing_read",
        _active_drawing_read_arguments(document_id),
    )
    duplicated_model = duplicated_read["data"]
    duplicated_entity_count = len(duplicated_model["entities"])
    if duplicated_entity_count <= first_entity_count:
        raise AssertionError("Second identical spec commit did not increase live entity count")
    duplicated_audit = await _call(session, "cad_audit", {"model": duplicated_model})
    duplicate_finding, layer_finding = _select_remediation_findings(duplicated_audit["data"])
    duplicate_ref = duplicate_finding["entity_ref"]
    layer_ref = layer_finding["entity_ref"]
    expected_layer = layer_finding["expected"]

    remediation_job = await _call(session, "cad_job_create", {"document_id": document_id})
    remediation_job_id = remediation_job["data"]["job_id"]
    remediation_revision = remediation_job["data"]["expected_revision"]
    remediated_plan = await _call(
        session,
        "cad_change_submit",
        {
            "job_id": remediation_job_id,
            "remediation": {
                "audit_id": duplicated_audit["data"]["audit_id"],
                "selected_findings": [
                    {"rule_id": "DUPLICATE_ENTITY", "entity_ref": duplicate_ref},
                    {"rule_id": "ENTITY_ON_EXPECTED_LAYER", "entity_ref": layer_ref},
                ],
            },
        },
    )
    remediation_plan_hash = remediated_plan["data"]["plan_hash"]
    remediation_preview = await _call(
        session,
        "cad_preview",
        {"job_id": remediation_job_id},
    )
    diff_entries = remediation_preview["data"]["semantic_diff"]["entries"]
    counts = {
        change: sum(entry["change"] == change for entry in diff_entries)
        for change in ("added", "modified", "deleted")
    }
    if counts != {"added": 0, "modified": 1, "deleted": 1}:
        raise AssertionError(f"Unexpected remediation semantic diff counts: {counts}")
    targets_by_change = {entry["change"]: entry.get("target_entity_ref") for entry in diff_entries}
    if targets_by_change != {"deleted": duplicate_ref, "modified": layer_ref}:
        raise AssertionError("Remediation preview did not target the exact selected findings")

    remediation_validation = await _call(
        session,
        "cad_validate",
        {"job_id": remediation_job_id, "stage": "pre_commit"},
    )
    if not remediation_validation["data"]["commit_allowed"]:
        raise AssertionError("Remediation plan did not pass pre-commit validation")
    remediation_approval_id, remediation_token = approve_job(
        remediation_job_id,
        remediation_plan_hash,
        remediation_revision,
    )
    remediation_commit = await _call(
        session,
        "cad_commit",
        {
            "job_id": remediation_job_id,
            "idempotency_key": f"live-mcp-remediation-{case_name}-{secrets.token_hex(8)}",
            "expected_revision": remediation_revision,
            "plan_hash": remediation_plan_hash,
            "approval_token": remediation_token,
        },
    )
    remediation_commit_data = remediation_commit["data"]
    if remediation_commit_data["status"] != "committed":
        raise AssertionError("Live remediation did not commit")

    remediated_read = await _call(
        session,
        "cad_drawing_read",
        _active_drawing_read_arguments(document_id),
    )
    remediated_model = remediated_read["data"]
    remediated_entity_count = len(remediated_model["entities"])
    if remediated_entity_count != duplicated_entity_count - 1:
        raise AssertionError("Remediation did not delete exactly one live entity")
    corrected_entity = next(
        (entity for entity in remediated_model["entities"] if entity["entity_ref"] == layer_ref),
        None,
    )
    if corrected_entity is None or corrected_entity["layer"] != expected_layer:
        raise AssertionError("Expected-layer remediation was not visible in live readback")
    remediated_audit = await _call(session, "cad_audit", {"model": remediated_model})
    remaining_pairs = {
        (finding["rule_id"], finding.get("entity_ref"))
        for finding in remediated_audit["data"]["report"]["findings"]
    }
    selected_pairs = {
        ("DUPLICATE_ENTITY", duplicate_ref),
        ("ENTITY_ON_EXPECTED_LAYER", layer_ref),
    }
    if selected_pairs & remaining_pairs:
        raise AssertionError("A selected remediation finding remained after live commit")

    return {
        "duplicate_seed": {
            "job_id": duplicate_job_id,
            "approval_id": duplicate_approval_id,
            "operation_count": duplicate_plan["data"]["operation_count"],
            "entity_count_before": first_entity_count,
            "entity_count_after": duplicated_entity_count,
            "commit_status": duplicate_commit_data["status"],
        },
        "selection": [
            {
                "rule_id": "DUPLICATE_ENTITY",
                "entity_ref_sha256": _redacted_entity_ref(duplicate_ref),
            },
            {
                "rule_id": "ENTITY_ON_EXPECTED_LAYER",
                "entity_ref_sha256": _redacted_entity_ref(layer_ref),
                "expected_layer": expected_layer,
            },
        ],
        "remediation": {
            "job_id": remediation_job_id,
            "approval_id": remediation_approval_id,
            "operation_count": remediated_plan["data"]["operation_count"],
            "diff_counts": counts,
            "exact_target_match": True,
            "target_ref_sha256_by_change": {
                change: _redacted_entity_ref(target)
                for change, target in sorted(targets_by_change.items())
            },
            "commit_status": remediation_commit_data["status"],
            "entity_count_before": duplicated_entity_count,
            "entity_count_after": remediated_entity_count,
            "selected_pairs_remaining": 0,
            "layer_corrected": True,
            "post_audit_finding_count": len(remediated_audit["data"]["report"]["findings"]),
        },
    }


async def _run_mcp_workflow(
    *,
    config_path: Path,
    spec: dict[str, Any],
    case_name: str,
    expected_display_name: str,
    expected_path_hash: str,
    export_target: Path | None = None,
) -> dict[str, Any]:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.mcp_server"],
        cwd=str(Path.cwd()),
        env=dict(os.environ),
        encoding="utf-8",
        encoding_error_handler="strict",
    )
    async with stdio_client(server) as (reader, writer):  # noqa: SIM117
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = sorted(tool.name for tool in tools.tools)
            status = await _call(session, "cad_status", {})
            adapter_status = status["data"]["adapter"]
            if adapter_status["adapter_type"] != "dotnet_bridge" or not adapter_status["available"]:
                raise AssertionError(
                    "MCP did not connect to the live dotnet_bridge adapter: "
                    f"adapter_type={adapter_status.get('adapter_type')!r}; "
                    f"message={adapter_status.get('message')!r}"
                )

            inspected = await _call(session, "cad_document_inspect", {})
            if (
                adapter_status["active_document_id"] != inspected["data"]["document_id"]
                or inspected["data"]["display_name"].casefold() != expected_display_name.casefold()
                or inspected["data"]["path_hash"] != expected_path_hash
            ):
                raise AssertionError(
                    "MCP bridge document identity does not match the PID-owned scratch drawing"
                )
            document_id = inspected["data"]["document_id"]
            initial_revision = inspected["data"]["revision"]
            created = await _call(session, "cad_job_create", {"document_id": document_id})
            job_id = created["data"]["job_id"]
            submitted = await _call(
                session,
                "cad_spec_submit",
                {"job_id": job_id, "spec": spec},
            )
            plan_hash = submitted["data"]["plan_hash"]
            previewed = await _call(session, "cad_preview", {"job_id": job_id})
            validated = await _call(
                session,
                "cad_validate",
                {"job_id": job_id, "stage": "pre_commit"},
            )
            if not validated["data"]["commit_allowed"]:
                raise AssertionError("Pre-commit validation did not allow the generated plan")

            # Human-only boundary. MCP never receives an approval-issuing tool.
            approval_id, approval_token = _issue_live_approval(
                config_path,
                job_id,
                plan_hash,
                initial_revision,
            )
            committed = await _call(
                session,
                "cad_commit",
                {
                    "job_id": job_id,
                    "idempotency_key": f"live-mcp-{case_name}-{secrets.token_hex(8)}",
                    "expected_revision": initial_revision,
                    "plan_hash": plan_hash,
                    "approval_token": approval_token,
                },
            )
            commit_data = committed["data"]
            if commit_data["status"] != "committed" or not commit_data["entity_results"]:
                raise AssertionError("Live MCP commit returned no committed entity evidence")

            detailed = await _call(
                session,
                "cad_drawing_read",
                _active_drawing_read_arguments(document_id),
            )
            recognized = await _call(
                session,
                "cad_feature_recognize",
                {"model": detailed["data"]},
            )
            audited = await _call(session, "cad_audit", {"model": detailed["data"]})
            live_takeoff: dict[str, Any] | None = None
            root_parameters = spec.get("features", [{}])[0].get("parameters", {})
            outlines = [
                feature
                for feature in recognized["data"]["features"]
                if feature["feature_type"] == "part_outline"
            ]
            if (
                outlines
                and isinstance(root_parameters.get("thickness_mm"), (int, float))
                and isinstance(root_parameters.get("material"), str)
            ):
                outline_ref = outlines[0]["entity_refs"][0]
                measured = await _call(
                    session,
                    "cad_measure",
                    {
                        "model": detailed["data"],
                        "request": {
                            "kind": "contour_perimeter",
                            "entity_refs": [outline_ref],
                        },
                    },
                )
                takeoff = await _call(
                    session,
                    "cad_takeoff",
                    {
                        "model": detailed["data"],
                        "request": {
                            "document_id": document_id,
                            "parts": [
                                {
                                    "part_code": case_name,
                                    "outline_entity_ref": outline_ref,
                                    "thickness_mm": root_parameters["thickness_mm"],
                                    "material_code": root_parameters["material"],
                                    "quantity": 1,
                                }
                            ],
                            "material_profile_ref": "demo-materials@1.0",
                        },
                    },
                )
                part = takeoff["data"]["parts"][0]
                live_takeoff = {
                    "outline_entity_ref": outline_ref,
                    "measured_perimeter_mm": measured["data"]["value"],
                    "net_area_mm2": part["net_area_mm2"],
                    "unit_mass_kg": part["unit_mass_kg"],
                    "cut_length_mm": part["cut_length_mm"],
                    "pierce_count": part["pierce_count"],
                    "company_approved": takeoff["data"]["company_approved"],
                }
            remediation_acceptance = await _run_remediation_acceptance(
                session,
                document_id=document_id,
                spec=spec,
                case_name=case_name,
                first_entity_count=len(detailed["data"]["entities"]),
                approve_job=lambda remediation_job_id, remediation_plan_hash, revision: (
                    _issue_live_approval(
                        config_path,
                        remediation_job_id,
                        remediation_plan_hash,
                        revision,
                    )
                ),
            )
            exported: dict[str, Any] | None = None
            if export_target is not None:
                export_result = await _call(
                    session,
                    "cad_export",
                    {
                        "document_id": document_id,
                        "target_path": str(export_target),
                        "export_format": "dwg",
                        "overwrite": False,
                    },
                )
                exported = {
                    "format": export_result["data"]["format"],
                    "artifact_ref": export_result["data"]["artifact_ref"],
                }
            return {
                "tool_count": len(tool_names),
                "tool_names": tool_names,
                "adapter": adapter_status,
                "document_id": document_id,
                "initial_revision": initial_revision,
                "job_id": job_id,
                "plan_hash": plan_hash,
                "operation_count": submitted["data"]["operation_count"],
                "preview_artifact_count": len(previewed["data"]["artifacts"]),
                "validation": {
                    "blocking_count": validated["data"]["blocking_count"],
                    "error_count": validated["data"]["error_count"],
                    "warning_count": validated["data"]["warning_count"],
                },
                "approval_id": approval_id,
                "commit": {
                    "status": commit_data["status"],
                    "previous_revision": commit_data["previous_revision"],
                    "new_revision": commit_data["new_revision"],
                    "entity_count": len(commit_data["entity_results"]),
                    "checkpoint_present": bool(commit_data.get("checkpoint_id")),
                    "undo_group_present": bool(commit_data.get("undo_group")),
                },
                "readback_entity_count": len(detailed["data"]["entities"]),
                "recognized_feature_count": len(recognized["data"]["features"]),
                "ambiguous_recognition_group_count": len(recognized["data"]["ambiguous_groups"]),
                "open_contour_count": len(recognized["data"]["open_contours"]),
                "audit_finding_count": len(audited["data"]["report"]["findings"]),
                "live_takeoff": live_takeoff,
                "live_remediation": remediation_acceptance,
                "export": exported,
            }


def run_acceptance(
    *,
    config_path: Path,
    spec_path: Path,
    case_name: str,
    work_root: Path,
    evidence_path: Path,
    durable_checkpoint_restore: bool = False,
) -> dict[str, Any]:
    case_name = _safe_case_name(case_name)
    config_path = config_path.resolve(strict=True)
    spec = _load_spec(spec_path)
    _configure_acceptance_bundle_environment()
    case_root = (work_root / case_name).resolve()
    scratch = case_root / f"{case_name}-scratch.dxf"
    result_dwg = case_root / f"{case_name}-result.dwg"
    if case_root.exists():
        raise FileExistsError(f"Acceptance case root already exists: {case_root}")
    _create_empty_scratch(scratch)
    scratch_hash = _sha256(scratch)

    os.environ["CAD_HARNESS_CONFIG"] = str(config_path)
    os.environ["CAD_HARNESS_APPROVAL_SECRET"] = secrets.token_urlsafe(48)
    os.environ["CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS"] = _SETUP_CONFIRMATIONS
    os.environ["CAD_HARNESS_SQLITE_PATH"] = str(case_root / "harness.db")
    os.environ["CAD_HARNESS_PREVIEW_DIR"] = str(case_root / "previews")
    os.environ["CAD_HARNESS_CHECKPOINT_DIR"] = str(case_root / "checkpoints")
    checkpoint_root = _configure_durable_restore_environment(
        case_root,
        enabled=durable_checkpoint_restore,
    )
    os.environ["CAD_HARNESS_LIVE_WRITE_VERIFIED"] = "1"
    os.environ["CAD_HARNESS_LOG_LEVEL"] = "ERROR"

    # AutoCAD 2027 may spend longer than one minute discovering a newly versioned
    # per-user bundle on its first launch. The PID is already ownership-fenced, so
    # keep a bounded two-minute readiness window without weakening cleanup safety.
    com = ComAutoCADAdapter("autocad", startup_wait_seconds=120.0)
    acceptance_started_100ns = com._system_filetime_100ns()
    preexisting_pids = com._acad_process_ids()
    tracked_processes: dict[int, TrackedProcess] = {}
    ownership_roots: set[int] = set()
    original_failure: BaseException | None = None
    postexisting_pids: set[int] | None = None
    try:
        session = com.connect_isolated(versioned_prog_id="AutoCAD.Application.26")
        if session.pid in preexisting_pids:
            raise AssertionError("Live MCP acceptance reused a pre-existing AutoCAD process")
        ownership_roots.add(session.pid)
        _track_acceptance_processes(
            com,
            preexisting_pids=preexisting_pids,
            acceptance_started_100ns=acceptance_started_100ns,
            ownership_roots=ownership_roots,
            tracked=tracked_processes,
        )
        root_record = tracked_processes.get(session.pid)
        if root_record is None or root_record[:2] != (
            session.image_path,
            session.creation_time_100ns,
        ):
            raise AssertionError(
                "PID-owned AutoCAD process was not tracked with its exact identity"
            )
        _wait_for_owned_com_readiness(com, timeout_seconds=com.startup_wait_seconds)
        com.open_owned_document(scratch, read_only=False)
        if durable_checkpoint_restore:
            _save_owned_as_native_dwg(com, result_dwg)
        _trigger_owned_bridge_load(com)
        # SendCommand queues NETLOAD asynchronously. Connecting while the extension
        # has created its first pipe object but before the accept loop is scheduled can
        # strand timeout/cancel probes and exhaust the bounded handler slots. A short
        # first-load settling window prevents that startup-only race; operation timeout
        # policy remains unchanged once the bridge is ready.
        time.sleep(30.0)
        _track_acceptance_processes(
            com,
            preexisting_pids=preexisting_pids,
            acceptance_started_100ns=acceptance_started_100ns,
            ownership_roots=ownership_roots,
            tracked=tracked_processes,
        )
        if durable_checkpoint_restore:
            native_seed_sha256 = _sha256(result_dwg)
            workflow = asyncio.run(
                _run_durable_checkpoint_restore_workflow(
                    config_path=config_path,
                    spec=spec,
                    case_name=case_name,
                    native_dwg=result_dwg,
                    checkpoint_root=checkpoint_root,
                    persist_document=lambda: _save_owned_active_dwg(com, result_dwg),
                )
            )
            workflow["native_seed_sha256"] = native_seed_sha256
        else:
            workflow = asyncio.run(
                _run_mcp_workflow(
                    config_path=config_path,
                    spec=spec,
                    case_name=case_name,
                    expected_display_name=scratch.name,
                    expected_path_hash=_bridge_path_hash(scratch),
                )
            )
        _track_acceptance_processes(
            com,
            preexisting_pids=preexisting_pids,
            acceptance_started_100ns=acceptance_started_100ns,
            ownership_roots=ownership_roots,
            tracked=tracked_processes,
        )
        if not durable_checkpoint_restore:
            document = com._require_document()
            document.SaveAs(str(result_dwg))
        if not result_dwg.is_file():
            raise AssertionError("AutoCAD did not persist the committed result DWG")
        workflow["bridge_preflight"] = (
            workflow["session_a"]["adapter"] if durable_checkpoint_restore else workflow["adapter"]
        )
        workflow["owned_process"] = {
            "pid": session.pid,
            "image_name": Path(session.image_path).name,
            "creation_time_100ns": session.creation_time_100ns,
            "preexisting_pids": sorted(preexisting_pids),
        }
        workflow["scratch_sha256_before"] = scratch_hash
        workflow["result_dwg_sha256"] = _sha256(result_dwg)
        workflow["result_dwg_bytes"] = result_dwg.stat().st_size
        workflow["result_name"] = result_dwg.name
    except BaseException as exc:
        original_failure = exc
        raise
    finally:
        cleanup_failure: BaseException | None = None
        try:
            _close_owned_session_preserving_failure(com, original_failure)
        except BaseException as exc:
            cleanup_failure = exc
        try:
            postexisting_pids = _cleanup_acceptance_processes(
                com,
                preexisting_pids=preexisting_pids,
                acceptance_started_100ns=acceptance_started_100ns,
                ownership_roots=ownership_roots,
                tracked=tracked_processes,
            )
        except BaseException:
            if original_failure is None and cleanup_failure is None:
                raise
        if original_failure is None and cleanup_failure is not None:
            raise cleanup_failure

    assert postexisting_pids is not None
    workflow["acceptance_processes"] = [
        {
            "pid": pid,
            "image_name": Path(identity[0]).name,
            "creation_time_100ns": identity[1],
            "parent_pid": identity[2],
        }
        for pid, identity in sorted(tracked_processes.items())
    ]
    workflow["postexisting_pids"] = sorted(postexisting_pids)
    workflow["user_process_preserved"] = postexisting_pids == preexisting_pids
    evidence_path = evidence_path.resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(workflow, indent=2, sort_keys=True), encoding="utf-8")
    return workflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--case-name", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--durable-checkpoint-restore",
        action="store_true",
        help=(
            "Explicitly enable the destructive two-session whole-DWG checkpoint_restore "
            "acceptance on the generated disposable DWG"
        ),
    )
    args = parser.parse_args(argv)
    result = run_acceptance(
        config_path=args.config,
        spec_path=args.spec,
        case_name=args.case_name,
        work_root=args.work_root,
        evidence_path=args.evidence,
        durable_checkpoint_restore=args.durable_checkpoint_restore,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - live entrypoint
    raise SystemExit(main())
