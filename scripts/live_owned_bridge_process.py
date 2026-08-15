"""Launch a disposable AutoCAD bridge process without COM/DCOM activation.

This acceptance helper is deliberately narrower than the production adapter.  It opens
one caller-supplied disposable drawing, runs one fixed NETLOAD startup script, proves the
Named Pipe server belongs to the exact process created under a kill-on-close Job Object,
and then tears that process down.  It never discovers or attaches to a running AutoCAD
instance.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import secrets
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cad_harness.adapters.autocad_com import (
    ComAutoCADAdapter,
    _AcceptanceBundleLease,
    _acquire_acceptance_bundle,
    _is_reparse_path,
    _LockedAcceptanceArtifact,
    _open_locked_acceptance_artifact,
    _resolve_local_server_executable,
    _WindowsStartupJob,
)
from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter
from cad_harness.adapters.named_pipe_transport import resolve_current_user_pipe_name

_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STILL_ACTIVE = 259
_MAX_STARTUP_SECONDS = 300.0
_CHILD_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "appdata",
        "commonprogramfiles",
        "commonprogramfiles(x86)",
        "computername",
        "homedrive",
        "homepath",
        "localappdata",
        "number_of_processors",
        "os",
        "path",
        "pathext",
        "processor_architecture",
        "programdata",
        "programfiles",
        "programfiles(x86)",
        "systemdrive",
        "systemroot",
        "temp",
        "tmp",
        "userdomain",
        "username",
        "userprofile",
        "windir",
    }
)
_CHILD_HARNESS_OVERRIDE_ALLOWLIST = frozenset(
    {
        "CAD_HARNESS_APPROVAL_SECRET",
        "CAD_HARNESS_CHECKPOINT_ROOT",
        "CAD_HARNESS_COMMIT_JOURNAL_ROOT",
        "CAD_HARNESS_DURABLE_RESTORE_VERIFIED",
        "CAD_HARNESS_LIVE_WRITE_VERIFIED",
        "CAD_HARNESS_LOG_LEVEL",
    }
)


class _OwnedJob(Protocol):
    def assign_pid(self, pid: int) -> None: ...

    def contains_pid(self, pid: int) -> bool: ...

    def terminate_and_wait(self, timeout_seconds: float) -> bool: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _CreatedProcess:
    pid: int
    process_handle: int
    thread_handle: int


@dataclass(slots=True)
class OwnedBridgeProcess:
    """Terminally owned live-acceptance process and its retained bundle lease."""

    pid: int
    process_handle: int
    job: _OwnedJob
    bundle_lease: _AcceptanceBundleLease
    script_lease: _LockedAcceptanceArtifact
    launch_directory: Path
    pipe_name: str
    pipe_template: str
    expected_executable: Path
    expected_creation_time_100ns: int
    _closed: bool = False

    def wait_until_ready(self, timeout_seconds: float) -> None:
        timeout_seconds = _bounded_timeout(timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self._assert_exact_process_alive()
            server_pid = _named_pipe_server_pid(self.pipe_name)
            if server_pid is None:
                time.sleep(0.05)
                continue
            if server_pid != self.pid:
                raise RuntimeError("Bridge pipe belongs to another AutoCAD process")
            self.bundle_lease.revalidate_after_load_proof()
            return
        raise TimeoutError("Owned AutoCAD bridge pipe did not become ready")

    def _assert_exact_process_alive(self) -> None:
        if not self.job.contains_pid(self.pid):
            raise RuntimeError("Launched AutoCAD process escaped its ownership Job")
        image_path, creation_time = ComAutoCADAdapter._process_identity(self.pid)
        if not _same_path(image_path, self.expected_executable):
            raise RuntimeError("Launched AutoCAD process image identity changed")
        if creation_time != self.expected_creation_time_100ns:
            raise RuntimeError("Launched AutoCAD process creation identity changed")
        if not _process_is_active(self.process_handle):
            raise RuntimeError("Launched AutoCAD process exited before bridge readiness")

    def close(self, timeout_seconds: float = 30.0) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        try:
            if not self.job.terminate_and_wait(_bounded_timeout(timeout_seconds)):
                errors.append(TimeoutError("Owned AutoCAD Job did not become terminal"))
        except BaseException as exc:  # cleanup must continue through interrupts
            errors.append(exc)
        try:
            _close_handle(self.process_handle)
        except BaseException as exc:
            errors.append(exc)
        try:
            self.job.close()
        except BaseException as exc:
            errors.append(exc)
        try:
            self.bundle_lease.close()
        except BaseException as exc:
            errors.append(exc)
        try:
            self.script_lease.close()
        except BaseException as exc:
            errors.append(exc)
        try:
            _remove_launch_directory(self.launch_directory)
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise RuntimeError("Owned AutoCAD cleanup could not be confirmed") from errors[0]

    def __enter__(self) -> OwnedBridgeProcess:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def launch_owned_bridge_process(
    *,
    drawing_path: Path,
    work_root: Path,
    versioned_prog_id: str = "AutoCAD.Application.26",
    child_environment_overrides: dict[str, str] | None = None,
    job_factory: type[_WindowsStartupJob] = _WindowsStartupJob,
) -> OwnedBridgeProcess:
    """Launch one disposable drawing and load the exact verified bridge bundle."""

    if sys.platform != "win32":
        raise OSError("Owned AutoCAD bridge launch requires Windows")
    work_root = _validated_local_directory(work_root)
    drawing_path = _validated_drawing(drawing_path, work_root)
    pipe_template = f"cadharness.acceptance.{secrets.token_hex(16)}.{{user_sid}}"
    pipe_name = resolve_current_user_pipe_name(pipe_template)
    if _named_pipe_server_pid(pipe_name) is not None:
        raise RuntimeError("Nonce bridge pipe existed before launch; provenance is ambiguous")

    executable = _resolve_local_server_executable(versioned_prog_id)
    lease = _acquire_acceptance_bundle()
    launch_directory: Path | None = None
    script_lease: _LockedAcceptanceArtifact | None = None
    job: _OwnedJob | None = None
    created: _CreatedProcess | None = None
    try:
        launch_directory, startup_script = _create_fixed_startup_script(
            work_root,
            lease.bridge_path,
        )
        script_lease = _open_locked_acceptance_artifact(startup_script)
        command_line = _owned_bridge_command_line(executable, drawing_path, startup_script)
        job = job_factory.create()
        created = _create_suspended_process(
            executable,
            command_line,
            launch_directory,
            environment_overrides={
                **_validated_harness_child_overrides(child_environment_overrides or {}),
                "CAD_HARNESS_BRIDGE_PIPE_NAME_TEMPLATE": pipe_template,
            },
        )
        try:
            job.assign_pid(created.pid)
            if not job.contains_pid(created.pid):
                raise RuntimeError("AutoCAD process was not assigned to its ownership Job")
            image_path, creation_time = ComAutoCADAdapter._process_identity(created.pid)
            if not _same_path(image_path, executable):
                raise RuntimeError("Launched AutoCAD executable identity mismatch")
            if _resume_thread(created.thread_handle) != 1:
                raise RuntimeError("AutoCAD startup thread was not initially suspended")
        finally:
            _close_handle(created.thread_handle)
        return OwnedBridgeProcess(
            pid=created.pid,
            process_handle=created.process_handle,
            job=job,
            bundle_lease=lease,
            script_lease=script_lease,
            launch_directory=launch_directory,
            pipe_name=pipe_name,
            pipe_template=pipe_template,
            expected_executable=executable,
            expected_creation_time_100ns=creation_time,
        )
    except BaseException as primary:
        cleanup_errors: list[BaseException] = []
        if job is not None:
            try:
                if not job.terminate_and_wait(30.0):
                    cleanup_errors.append(
                        TimeoutError("Failed launch left a non-terminal AutoCAD Job")
                    )
            except BaseException as exc:
                cleanup_errors.append(exc)
            try:
                job.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        elif created is not None:
            try:
                _terminate_process(created.process_handle)
            except BaseException as exc:
                cleanup_errors.append(exc)
        if created is not None:
            try:
                _close_handle(created.process_handle)
            except BaseException as exc:
                cleanup_errors.append(exc)
        try:
            lease.close()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if script_lease is not None:
            try:
                script_lease.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if launch_directory is not None:
            try:
                _remove_launch_directory(launch_directory)
            except BaseException as exc:
                cleanup_errors.append(exc)
        for cleanup_error in cleanup_errors:
            primary.add_note(f"cleanup_error={type(cleanup_error).__name__}")
        raise


def _create_fixed_startup_script(work_root: Path, bridge_path: Path) -> tuple[Path, Path]:
    bridge_path = bridge_path.resolve(strict=True)
    if bridge_path.suffix.casefold() != ".dll" or '"' in str(bridge_path):
        raise ValueError("Bridge path is not valid for the fixed startup script")
    launch_directory = work_root / f"owned-bridge-{secrets.token_hex(12)}"
    launch_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    startup_script = launch_directory / "load-bridge.scr"
    payload = f'_.NETLOAD\r\n"{bridge_path}"\r\n'
    with startup_script.open("x", encoding="ascii", newline="") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return launch_directory, startup_script


def _owned_bridge_command_line(executable: Path, drawing: Path, script: Path) -> str:
    parts = (executable, drawing, script)
    if any('"' in str(path) or "\r" in str(path) or "\n" in str(path) for path in parts):
        raise ValueError("AutoCAD launch paths contain unsafe characters")
    return f'"{executable}" "{drawing}" /product ACADM /language "en-US" /nologo /b "{script}"'


def _create_suspended_process(
    executable: Path,
    command_line: str,
    working_directory: Path,
    *,
    environment_overrides: dict[str, str] | None,
) -> _CreatedProcess:
    class StartupInfoW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    startup = StartupInfoW()
    startup.cb = ctypes.sizeof(startup)
    process = ProcessInformation()
    mutable_command = ctypes.create_unicode_buffer(command_line)
    environment = (
        ctypes.create_unicode_buffer(_windows_environment_block(environment_overrides))
        if environment_overrides is not None
        else None
    )
    create_process = ctypes.windll.kernel32.CreateProcessW
    create_process.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(StartupInfoW),
        ctypes.POINTER(ProcessInformation),
    )
    create_process.restype = wintypes.BOOL
    if not create_process(
        str(executable),
        mutable_command,
        None,
        None,
        False,
        _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT,
        ctypes.cast(environment, wintypes.LPVOID) if environment is not None else None,
        str(working_directory),
        ctypes.byref(startup),
        ctypes.byref(process),
    ):
        raise ctypes.WinError()
    return _CreatedProcess(
        pid=int(process.dwProcessId),
        process_handle=int(process.hProcess),
        thread_handle=int(process.hThread),
    )


def _windows_environment_block(overrides: dict[str, str]) -> str:
    """Build a sorted, case-insensitive, double-NUL Windows environment block."""

    merged: dict[str, tuple[str, str]] = {}
    inherited = (
        (key, value)
        for key, value in os.environ.items()
        if key.casefold() in _CHILD_ENVIRONMENT_ALLOWLIST
    )
    for key, value in (*inherited, *overrides.items()):
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("Windows child environment contains an invalid entry")
        merged[key.casefold()] = (key, value)
    entries = [
        f"{key}={value}"
        for key, value in sorted(merged.values(), key=lambda item: item[0].casefold())
    ]
    return "\0".join(entries) + "\0\0"


def _validated_harness_child_overrides(overrides: dict[str, str]) -> dict[str, str]:
    if set(overrides) - _CHILD_HARNESS_OVERRIDE_ALLOWLIST:
        raise ValueError("AutoCAD child environment contains a non-allowlisted override")
    for key, value in overrides.items():
        if not value or "\0" in value or "\r" in value or "\n" in value:
            raise ValueError(f"AutoCAD child environment override is invalid: {key}")
    return dict(overrides)


def _named_pipe_server_pid(pipe_name: str) -> int | None:
    """Observe a pipe server PID without sending a bridge request."""

    open_existing = 3
    file_attribute_normal = 0x00000080
    error_file_not_found = 2
    error_pipe_busy = 231
    invalid_handle = ctypes.c_void_p(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.restype = wintypes.HANDLE
    handle = create_file(pipe_name, 0, 0, None, open_existing, file_attribute_normal, None)
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        if error == error_file_not_found:
            return None
        if error == error_pipe_busy:
            raise OSError("Nonce bridge pipe is busy; server PID cannot be verified")
        raise ctypes.WinError(error)
    try:
        server_pid = wintypes.ULONG()
        if not kernel32.GetNamedPipeServerProcessId(
            wintypes.HANDLE(handle), ctypes.byref(server_pid)
        ):
            raise ctypes.WinError()
        return int(server_pid.value)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _resume_thread(thread_handle: int) -> int:
    result = int(ctypes.windll.kernel32.ResumeThread(thread_handle))
    if result == 0xFFFFFFFF:
        raise ctypes.WinError()
    return result


def _process_is_active(process_handle: int) -> bool:
    exit_code = wintypes.DWORD()
    if not ctypes.windll.kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
        raise ctypes.WinError()
    return int(exit_code.value) == _STILL_ACTIVE


def _terminate_process(process_handle: int) -> None:
    if not ctypes.windll.kernel32.TerminateProcess(process_handle, 1):
        raise ctypes.WinError()
    if ctypes.windll.kernel32.WaitForSingleObject(process_handle, 30_000) != 0:
        raise TimeoutError("Unassigned AutoCAD process did not terminate")


def _close_handle(handle: int) -> None:
    if handle and not ctypes.windll.kernel32.CloseHandle(handle):
        raise ctypes.WinError()


def _validated_local_directory(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if (
        not resolved.is_dir()
        or str(resolved).startswith(("\\\\", "//"))
        or _is_reparse_path(resolved)
    ):
        raise ValueError("Acceptance work root must be an existing local directory")
    return resolved


def _validated_drawing(path: Path, work_root: Path) -> Path:
    resolved = path.resolve(strict=True)
    if resolved.suffix.casefold() not in {".dwg", ".dxf"} or not resolved.is_file():
        raise ValueError("Acceptance drawing must be one existing DWG or DXF")
    if _is_reparse_path(resolved):
        raise ValueError("Acceptance drawing cannot be a reparse point")
    if resolved != work_root and work_root not in resolved.parents:
        raise ValueError("Acceptance drawing must be contained by the work root")
    return resolved


def _remove_launch_directory(path: Path, timeout_seconds: float = 5.0) -> None:
    script = path / "load-bridge.scr"
    if script.exists():
        script.unlink()
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            path.rmdir()
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def _bounded_timeout(value: float) -> float:
    if not 0.0 < value <= _MAX_STARTUP_SECONDS:
        raise ValueError(f"timeout must be > 0 and <= {_MAX_STARTUP_SECONDS:g} seconds")
    return value


def _path_hash(path: Path) -> str:
    normalized = os.path.normcase(os.path.normpath(str(path.resolve()))).encode("utf-8")
    return f"sha256:{hashlib.sha256(normalized).hexdigest()}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drawing", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()
    os.environ["CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT"] = str(args.bundle_root.resolve(strict=True))
    evidence: dict[str, object] = {
        "schema_version": "1.0",
        "operation": "owned_bridge_startup_probe",
        "production_evidence": False,
        "drawing_ref": _path_hash(args.drawing),
    }
    try:
        with launch_owned_bridge_process(
            drawing_path=args.drawing,
            work_root=args.work_root,
        ) as process:
            process.wait_until_ready(args.timeout_seconds)
            status = DotNetBridgeAdapter(
                process.pipe_name,
                timeout_seconds=min(args.timeout_seconds, 30.0),
            ).status()
            if not status.available or status.cad_version is None:
                raise RuntimeError("Owned bridge status handshake was unavailable")
            evidence.update(
                {
                    "ok": True,
                    "bridge_pid_matches_owned_process": True,
                    "bridge_status_available": True,
                    "cad_application": status.cad_application,
                    "cad_version": status.cad_version,
                    "capability_count": len(status.capabilities),
                    "active_document_bound": status.active_document_id is not None,
                    "owned_process_terminal_after_probe": True,
                }
            )
    except BaseException as exc:
        evidence.update({"ok": False, "error_type": type(exc).__name__})
        print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
        return 2
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
