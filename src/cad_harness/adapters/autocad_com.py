"""AutoCAD COM/ActiveX adapter for the MVP (architecture section 16.1).

This is the **only** module allowed to import ``win32com``/``pythoncom``. Ruff enforces
that with a banned-api rule.

Technique reference: the coordinate marshalling below (``VARIANT`` with
``VT_ARRAY | VT_R8``) and the ``GetActiveObject`` -> ``Dispatch`` connection fallback
follow the approach used by the community
[CAD-MCP server](https://github.com/daobataotie/CAD-MCP). What is deliberately *not*
borrowed is its design: primitive drawing tools exposed straight to the LLM, defaults
applied silently, and no preview/approval gate.

Known limitations, stated rather than hidden:

* No real transaction. ``StartUndoMark``/``EndUndoMark`` groups a commit for the user's
  interactive Undo, but a mid-operation failure can leave partial geometry. The adapter
  reports unknown state and never issues command strings; production writes use the bridge.
* ``Handle`` is stable within a session but XData support through ActiveX is uneven, so
  feature identity is always mirrored in the job store.
* Revisions are coarse: entity count plus handle digest, not a database fingerprint.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import math
import multiprocessing
import os
import re
import secrets
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path, PureWindowsPath
from typing import Any, ClassVar, Protocol, cast

from cad_harness.adapters.base import BaseAdapter
from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.errors import (
    AdapterCapabilityMissingError,
    AutoCADBusyError,
    AutoCADNotRunningError,
    ComCallFailedError,
    ReadScopeTooLargeError,
    RollbackNotAvailableError,
    StaleDocumentRevisionError,
    UnknownCommitStateError,
)
from cad_harness.domain.models.document import (
    DocumentSnapshot,
    EntitySummary,
    LayerInfo,
    SelectionSnapshot,
)
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    CircleGeometry,
    DrawingModel,
    EntityRecord,
    LineGeometry,
    PolylineGeometry,
    PolylineVertex,
    UnsupportedEntityCount,
)
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.result import (
    CommitResult,
    CommitStatus,
    EntityResult,
    ExportResult,
    PreviewResult,
    RollbackResult,
)
from cad_harness.domain.ports.autocad_adapter import (
    AdapterCapability,
    AdapterStatus,
    CommitRequest,
    ExportRequest,
    InspectRequest,
    RollbackRequest,
    SelectionRequest,
)
from cad_harness.domain.ports.drawing_source import DrawingReadRequest
from cad_harness.domain.ports.repositories import CancellationTokenPort, JobStore
from cad_harness.domain.value_objects.units import Unit

#: ProgIDs for AutoCAD and the API-compatible alternatives.
PROG_IDS: dict[str, str] = {
    "autocad": "AutoCAD.Application",
    "gstarcad": "GCAD.Application",
    "gcad": "GCAD.Application",
    "zwcad": "ZWCAD.Application",
}

#: AutoCAD accepts only this discrete set of lineweight values (hundredths of a mm).
VALID_LINEWEIGHTS: frozenset[int] = frozenset(
    (
        *(0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50),
        *(53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211),
    )
)

#: AutoCAD ``Units`` enum values that map to a canonical unit we understand.
_INSUNITS_TO_UNIT: dict[int, Unit] = {1: Unit.INCH, 4: Unit.MM, 5: Unit.CM, 6: Unit.M}
_INSUNITS_TO_MM: dict[int, tuple[str, float]] = {
    1: ("inch", 25.4),
    2: ("foot", 304.8),
    4: ("mm", 1.0),
    5: ("cm", 10.0),
    6: ("m", 1000.0),
    7: ("km", 1_000_000.0),
    8: ("microinch", 0.0000254),
    9: ("mil", 0.0254),
    10: ("yard", 914.4),
    11: ("angstrom", 0.0000001),
    12: ("nm", 0.000001),
    13: ("micron", 0.001),
    14: ("dm", 100.0),
}
_COM_SEMANTIC_ENTITY_TYPES = frozenset({"AcDbLine", "AcDbPolyline", "AcDbCircle", "AcDbArc"})
_COM_HANDLE_REF = re.compile(r"acad:handle:[0-9A-F]{1,16}\Z")

_VERSIONED_AUTOCAD_PROG_ID = re.compile(r"AutoCAD\.Application\.\d+(?:\.\d+)?", re.IGNORECASE)
_LOCAL_SERVER_EXE = re.compile(
    r'^\s*(?:"(?P<quoted>[^\"]+?\.exe)"|(?P<plain>.+?\.exe))\s+/Automation\s*$',
    re.IGNORECASE,
)
_STARTUP_IPC_MAX_BYTES = 1024 * 1024
_RPC_MAX_DEPTH = 32
_RPC_MAX_ITEMS = 4096
_RPC_MAX_STRING_BYTES = 32 * 1024
_RPC_MAX_BYTES_VALUE = 256 * 1024
_RPC_OBJECT_CAP = 512
_STARTUP_HELPER_TERMINATE_GRACE_SECONDS = 0.5
_STARTUP_PROCESS_CLEANUP_SECONDS = 5.0
_STARTUP_BINARY_TRUST_SECONDS = 10.0
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_DRIVE_FIXED = 3
_ACCEPTANCE_RECEIPT_NAME = "CAD-HARNESS-INSTALL-RECEIPT.json"
_ACCEPTANCE_CHECKSUM_NAME = "SHA256SUMS.ps1"
_ACCEPTANCE_BRIDGE_RELATIVE = "Contents/Windows/AutoCADHarness.dll"
_ACCEPTANCE_INSTALLER_LOCK_NAME = ".cad-harness-installer.lock"
_ACCEPTANCE_RECEIPT_FIELDS = frozenset(
    {
        "SchemaVersion",
        "Owner",
        "BundleName",
        "ArtifactKind",
        "AutoCADSeries",
        "AppVersion",
        "ProductCode",
        "UpgradeCode",
        "ChecksumManifestSha256",
        "SignerId",
        "Files",
        "Directories",
    }
)

_SCOPE_APPLICATION = "application"
_SCOPE_DOCUMENTS = "documents"
_SCOPE_DOCUMENT = "document"
_SCOPE_ACAD_STATE = "acad_state"
_SCOPE_LAYERS = "layers"
_SCOPE_LAYER = "layer"
_SCOPE_STYLES = "styles"
_SCOPE_STYLE = "style"
_SCOPE_MODEL_SPACE = "model_space"
_SCOPE_SELECTION = "selection"
_SCOPE_ENTITY = "entity"
_SCOPE_PLOT = "plot"
_SCOPE_ITERATOR_PREFIX = "iterator:"

# The isolated helper is a capability RPC service, not a generic IDispatch tunnel.
# Both endpoints enforce these tables so a compromised caller cannot discover or invoke
# a member that the deterministic adapter and the disposable acceptance runner do not use.
_RPC_GET_MEMBERS: dict[str, frozenset[str]] = {
    _SCOPE_APPLICATION: frozenset({"ActiveDocument", "Documents", "HWND", "Version", "Visible"}),
    _SCOPE_DOCUMENTS: frozenset({"Count"}),
    _SCOPE_DOCUMENT: frozenset(
        {
            "ActiveSelectionSet",
            "ActiveSpace",
            "DimStyles",
            "FullName",
            "Layers",
            "ModelSpace",
            "Name",
            "Plot",
            "ReadOnly",
            "TextStyles",
        }
    ),
    _SCOPE_ACAD_STATE: frozenset({"IsQuiescent"}),
    _SCOPE_LAYER: frozenset(
        {"Color", "Freeze", "LayerOn", "Linetype", "Lineweight", "Lock", "Name"}
    ),
    _SCOPE_STYLE: frozenset({"Name"}),
    _SCOPE_MODEL_SPACE: frozenset({"Count"}),
    _SCOPE_SELECTION: frozenset({"Count"}),
    _SCOPE_ENTITY: frozenset(
        {
            "ArcLength",
            "Area",
            "Center",
            "Circumference",
            "Closed",
            "Coordinates",
            "Diameter",
            "EndAngle",
            "EndPoint",
            "EntityName",
            "Handle",
            "Height",
            "InsertionPoint",
            "Layer",
            "Length",
            "Measurement",
            "ObjectName",
            "PatternName",
            "Radius",
            "StartAngle",
            "StartPoint",
            "TextPosition",
            "TextString",
            "Visible",
        }
    ),
}
_RPC_SET_MEMBERS: dict[str, frozenset[str]] = {
    _SCOPE_APPLICATION: frozenset({"Visible"}),
    _SCOPE_ENTITY: frozenset(
        {"Closed", "Color", "EndPoint", "Layer", "StartPoint", "StyleName", "TextOverride"}
    ),
}
_RPC_METHOD_MEMBERS: dict[str, frozenset[str]] = {
    _SCOPE_APPLICATION: frozenset({"GetAcadState", "Quit"}),
    _SCOPE_DOCUMENTS: frozenset({"Item", "Open"}),
    _SCOPE_DOCUMENT: frozenset(
        {
            "Close",
            "EndUndoMark",
            "GetVariable",
            "HandleToObject",
            "Save",
            "SaveAs",
            "SetVariable",
            "StartUndoMark",
        }
    ),
    _SCOPE_MODEL_SPACE: frozenset(
        {
            "AddArc",
            "AddCircle",
            "AddDimAligned",
            "AddDimAngular",
            "AddDimDiametric",
            "AddDimRadial",
            "AddDimRotated",
            "AddHatch",
            "AddLightWeightPolyline",
            "AddLine",
            "AddPoint",
            "AddText",
        }
    ),
    _SCOPE_SELECTION: frozenset({"Clear", "Select"}),
    _SCOPE_ENTITY: frozenset({"AppendOuterLoop", "Delete", "Evaluate", "GetBulge", "Update"}),
    _SCOPE_PLOT: frozenset({"PlotToFile"}),
}
_RPC_CHILD_SCOPES: dict[tuple[str, str], str] = {
    (_SCOPE_APPLICATION, "ActiveDocument"): _SCOPE_DOCUMENT,
    (_SCOPE_APPLICATION, "Documents"): _SCOPE_DOCUMENTS,
    (_SCOPE_APPLICATION, "GetAcadState"): _SCOPE_ACAD_STATE,
    (_SCOPE_DOCUMENTS, "Item"): _SCOPE_DOCUMENT,
    (_SCOPE_DOCUMENTS, "Open"): _SCOPE_DOCUMENT,
    (_SCOPE_DOCUMENT, "ActiveSelectionSet"): _SCOPE_SELECTION,
    (_SCOPE_DOCUMENT, "DimStyles"): _SCOPE_STYLES,
    (_SCOPE_DOCUMENT, "HandleToObject"): _SCOPE_ENTITY,
    (_SCOPE_DOCUMENT, "Layers"): _SCOPE_LAYERS,
    (_SCOPE_DOCUMENT, "ModelSpace"): _SCOPE_MODEL_SPACE,
    (_SCOPE_DOCUMENT, "Plot"): _SCOPE_PLOT,
    (_SCOPE_DOCUMENT, "TextStyles"): _SCOPE_STYLES,
    **{
        (_SCOPE_MODEL_SPACE, member): _SCOPE_ENTITY
        for member in _RPC_METHOD_MEMBERS[_SCOPE_MODEL_SPACE]
    },
}
_RPC_ITER_ITEM_SCOPES: dict[str, str] = {
    _SCOPE_LAYERS: _SCOPE_LAYER,
    _SCOPE_MODEL_SPACE: _SCOPE_ENTITY,
    _SCOPE_SELECTION: _SCOPE_ENTITY,
    _SCOPE_STYLES: _SCOPE_STYLE,
}
_RPC_METHOD_ARITY: dict[tuple[str, str], int] = {
    (_SCOPE_APPLICATION, "GetAcadState"): 0,
    (_SCOPE_APPLICATION, "Quit"): 0,
    (_SCOPE_DOCUMENTS, "Item"): 1,
    (_SCOPE_DOCUMENTS, "Open"): 2,
    (_SCOPE_DOCUMENT, "Close"): 1,
    (_SCOPE_DOCUMENT, "EndUndoMark"): 0,
    (_SCOPE_DOCUMENT, "GetVariable"): 1,
    (_SCOPE_DOCUMENT, "HandleToObject"): 1,
    (_SCOPE_DOCUMENT, "Save"): 0,
    (_SCOPE_DOCUMENT, "SaveAs"): 1,
    (_SCOPE_DOCUMENT, "SetVariable"): 2,
    (_SCOPE_DOCUMENT, "StartUndoMark"): 0,
    (_SCOPE_MODEL_SPACE, "AddArc"): 4,
    (_SCOPE_MODEL_SPACE, "AddCircle"): 2,
    (_SCOPE_MODEL_SPACE, "AddDimAligned"): 3,
    (_SCOPE_MODEL_SPACE, "AddDimAngular"): 4,
    (_SCOPE_MODEL_SPACE, "AddDimDiametric"): 3,
    (_SCOPE_MODEL_SPACE, "AddDimRadial"): 3,
    (_SCOPE_MODEL_SPACE, "AddDimRotated"): 4,
    (_SCOPE_MODEL_SPACE, "AddHatch"): 3,
    (_SCOPE_MODEL_SPACE, "AddLightWeightPolyline"): 1,
    (_SCOPE_MODEL_SPACE, "AddLine"): 2,
    (_SCOPE_MODEL_SPACE, "AddPoint"): 1,
    (_SCOPE_MODEL_SPACE, "AddText"): 3,
    (_SCOPE_SELECTION, "Clear"): 0,
    (_SCOPE_SELECTION, "Select"): 1,
    (_SCOPE_ENTITY, "AppendOuterLoop"): 1,
    (_SCOPE_ENTITY, "Delete"): 0,
    (_SCOPE_ENTITY, "Evaluate"): 0,
    (_SCOPE_ENTITY, "GetBulge"): 1,
    (_SCOPE_ENTITY, "Update"): 0,
    (_SCOPE_PLOT, "PlotToFile"): 1,
}
_ACCEPTANCE_NETLOAD_MEMBER = "SendCommand"
_STARTUP_FAILURE_STAGES = frozenset(
    {
        "helper_initialization",
        "parent_job_handshake",
        "com_initialization",
        "job_open",
        "binary_trust",
        "process_create",
        "process_identity",
        "job_assignment",
        "rot_discovery",
        "dispatch_marshal",
        "identity_verification",
        "parent_adoption",
        "rpc_service",
    }
)


class _StartupJob(Protocol):
    name: str

    def assign_pid(self, pid: int) -> None: ...

    def contains_pid(self, pid: int) -> bool: ...

    def terminate_and_wait(self, timeout_seconds: float) -> bool: ...

    def wait_until_empty(self, timeout_seconds: float) -> bool: ...

    def disarm(self) -> None: ...

    def close(self) -> None: ...


class _StartupProcess(Protocol):
    pid: int | None
    exitcode: int | None

    def start(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


_BinaryTrustVerifier = Callable[[Path, str], Path]


@dataclass(frozen=True, slots=True)
class _RemoteVariant:
    kind: str
    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class OwnedComSession:
    """Identity proof for an AutoCAD process created by this adapter instance."""

    prog_id: str
    hwnd: int
    pid: int
    image_path: str
    creation_time_100ns: int
    owned: bool = True


@dataclass(frozen=True, slots=True)
class _DeletedEntitySnapshot:
    """Receipt fields captured before ActiveX invalidates a deleted object."""

    Handle: str
    ObjectName: str


class _WindowsStartupJob:
    """Named job that kills only the AutoCAD process explicitly assigned to it."""

    def __init__(self, name: str) -> None:
        if sys.platform != "win32":
            raise OSError("AutoCAD startup jobs require Windows")
        import ctypes

        self.name = name
        self._handle = ctypes.windll.kernel32.CreateJobObjectW(None, name)
        if not self._handle:
            raise OSError("Could not create the isolated AutoCAD startup job")
        try:
            self._set_kill_on_close(True)
        except Exception:
            self.close()
            raise

    @classmethod
    def create(cls) -> _WindowsStartupJob:
        return cls(f"Local\\CadHarnessAutoCADStartup-{secrets.token_hex(16)}")

    def _set_kill_on_close(self, enabled: bool) -> None:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        if not self._handle:
            raise OSError("AutoCAD startup job is closed")
        information = ExtendedLimitInformation()
        if enabled:
            information.BasicLimitInformation.LimitFlags = 0x00002000
        if not ctypes.windll.kernel32.SetInformationJobObject(
            self._handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise OSError("Could not configure the isolated AutoCAD startup job")

    def _active_process_count(self) -> int:
        import ctypes
        from ctypes import wintypes

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        if not self._handle:
            return 0
        information = BasicAccountingInformation()
        if not ctypes.windll.kernel32.QueryInformationJobObject(
            self._handle,
            1,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            raise OSError("Could not query the isolated AutoCAD startup job")
        return int(information.ActiveProcesses)

    def terminate_and_wait(self, timeout_seconds: float) -> bool:
        import ctypes

        if not self._handle:
            return True
        if (
            not ctypes.windll.kernel32.TerminateJobObject(self._handle, 1)
            and self._active_process_count() != 0
        ):
            raise OSError("Could not terminate the isolated AutoCAD startup job")
        return self.wait_until_empty(timeout_seconds)

    def wait_until_empty(self, timeout_seconds: float) -> bool:
        if not self._handle:
            return True
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while self._active_process_count() != 0:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def assign_pid(self, pid: int) -> None:
        if not self._handle:
            raise OSError("AutoCAD startup job is closed")
        _assign_process_to_job(self._handle, pid)

    def contains_pid(self, pid: int) -> bool:
        if not self._handle:
            return False
        return _process_is_in_job(pid, self._handle)

    def disarm(self) -> None:
        self._set_kill_on_close(False)

    def close(self) -> None:
        if not self._handle:
            return
        import ctypes

        ctypes.windll.kernel32.CloseHandle(self._handle)
        self._handle = None


def _open_startup_job(name: str) -> int:
    import ctypes

    job_object_assign_process = 0x0001
    job_object_query = 0x0004
    handle = ctypes.windll.kernel32.OpenJobObjectW(
        job_object_assign_process | job_object_query, False, name
    )
    if not handle:
        raise OSError("Could not open the parent-owned AutoCAD startup job")
    return int(handle)


def _assign_process_to_job(job_handle: int, pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    process_set_quota = 0x0100
    process_terminate = 0x0001
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_set_quota | process_terminate | process_query_limited_information,
        False,
        wintypes.DWORD(pid),
    )
    if not handle:
        raise OSError("Could not open the launched AutoCAD process for job assignment")
    try:
        if not ctypes.windll.kernel32.AssignProcessToJobObject(job_handle, handle):
            raise OSError("Could not assign the launched AutoCAD process to its startup job")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _process_is_in_job(pid: int, job_handle: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information,
        False,
        wintypes.DWORD(pid),
    )
    if not handle:
        return False
    try:
        result = wintypes.BOOL()
        if not ctypes.windll.kernel32.IsProcessInJob(handle, job_handle, ctypes.byref(result)):
            raise OSError("Could not verify AutoCAD startup job membership")
        return bool(result.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _windows_file_attributes(path: Path) -> int:
    import ctypes

    attributes = int(ctypes.windll.kernel32.GetFileAttributesW(str(path)))
    if attributes == 0xFFFFFFFF:
        raise OSError("Could not inspect the registered AutoCAD executable path")
    return attributes


def _windows_drive_type(root: str) -> int:
    import ctypes

    return int(ctypes.windll.kernel32.GetDriveTypeW(root))


def _windows_directory() -> Path:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = int(ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)))
    if length <= 0 or length >= len(buffer):
        raise OSError("Could not resolve the trusted Windows directory")
    return Path(buffer.value)


def _validate_local_nonreparse_executable(
    candidate: Path,
    *,
    resolve_path: Callable[[Path], Path] = lambda path: path.resolve(strict=True),
    drive_type: Callable[[str], int] = _windows_drive_type,
    file_attributes: Callable[[Path], int] = _windows_file_attributes,
) -> Path:
    """Resolve an executable only from a fixed local drive without reparse traversal."""
    raw = str(candidate)
    windows_path = PureWindowsPath(raw)
    if (
        not windows_path.is_absolute()
        or not windows_path.drive
        or raw.startswith(("\\\\", "//"))
        or windows_path.drive.startswith("\\\\")
    ):
        raise OSError("AutoCAD LocalServer32 must be an absolute local path")
    root = f"{windows_path.drive}\\"
    if drive_type(root) != _DRIVE_FIXED:
        raise OSError("AutoCAD LocalServer32 must be on a fixed local drive")

    current = Path(root)
    for part in windows_path.parts[1:]:
        current /= part
        if file_attributes(current) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("AutoCAD LocalServer32 may not traverse a reparse point")

    resolved = resolve_path(candidate)
    if (
        not resolved.is_file()
        or resolved.name.casefold() != "acad.exe"
        or str(resolved).casefold() != str(candidate.absolute()).casefold()
    ):
        raise OSError("Versioned AutoCAD LocalServer32 is not a regular acad.exe")
    return resolved


def _run_native_trust_probe(
    powershell: Path,
    encoded_script: str,
    *,
    timeout_seconds: float,
) -> str:
    """Capture one fixed PowerShell trust probe through native Windows process APIs."""
    if sys.platform != "win32":
        raise OSError("AutoCAD binary trust verification requires Windows")
    import ctypes
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

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

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Trust-probe timeout must be positive")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    read_handle = wintypes.HANDLE()
    write_handle = wintypes.HANDLE()
    process = ProcessInformation()
    process_started = False
    process_terminal = False
    security = SecurityAttributes(
        ctypes.sizeof(SecurityAttributes),
        None,
        True,
    )
    try:
        if not kernel32.CreatePipe(
            ctypes.byref(read_handle),
            ctypes.byref(write_handle),
            ctypes.byref(security),
            0,
        ):
            raise ctypes.WinError()
        handle_flag_inherit = 0x00000001
        if not kernel32.SetHandleInformation(read_handle, handle_flag_inherit, 0):
            raise ctypes.WinError()
        startup = StartupInfoW()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
        startup.hStdOutput = write_handle
        startup.hStdError = write_handle
        startup.hStdInput = wintypes.HANDLE()
        command_line = ctypes.create_unicode_buffer(
            f'"{powershell}" -NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded_script}'
        )
        if not kernel32.CreateProcessW(
            str(powershell),
            command_line,
            None,
            None,
            True,
            0x08000000,  # CREATE_NO_WINDOW
            None,
            str(powershell.parent),
            ctypes.byref(startup),
            ctypes.byref(process),
        ):
            raise ctypes.WinError()
        process_started = True
        kernel32.CloseHandle(write_handle)
        write_handle = wintypes.HANDLE()
        wait_result = kernel32.WaitForSingleObject(
            process.hProcess,
            max(1, int(timeout_seconds * 1000)),
        )
        if wait_result == 258:  # WAIT_TIMEOUT
            kernel32.TerminateProcess(process.hProcess, 1)
            kernel32.WaitForSingleObject(process.hProcess, 5_000)
            process_terminal = True
            raise TimeoutError("AutoCAD binary trust verification timed out")
        if wait_result != 0:  # WAIT_OBJECT_0
            raise ctypes.WinError()
        process_terminal = True
        output = bytearray()
        while True:
            buffer = ctypes.create_string_buffer(4096)
            received = wintypes.DWORD()
            if not kernel32.ReadFile(
                read_handle,
                buffer,
                len(buffer),
                ctypes.byref(received),
                None,
            ):
                error = ctypes.get_last_error()
                if error == 109:  # ERROR_BROKEN_PIPE
                    break
                raise ctypes.WinError(error)
            output.extend(buffer.raw[: received.value])
            if len(output) > 64 * 1024:
                raise OSError("AutoCAD binary trust verification output is too large")
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError()
        if exit_code.value != 0:
            raise OSError("AutoCAD binary trust verification failed")
        return bytes(output).decode("utf-8-sig")
    finally:
        if process_started and not process_terminal:
            with contextlib.suppress(Exception):
                kernel32.TerminateProcess(process.hProcess, 1)
                kernel32.WaitForSingleObject(process.hProcess, 5_000)
        for handle in (
            process.hThread,
            process.hProcess,
            write_handle,
            read_handle,
        ):
            if handle:
                with contextlib.suppress(Exception):
                    kernel32.CloseHandle(handle)


def _read_production_binary_evidence(trusted_path: Path) -> dict[str, Any]:
    system_root = _windows_directory()
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file() or _is_reparse_path(powershell):
        raise OSError("Trusted Windows PowerShell is unavailable")
    encoded_path = base64.b64encode(str(trusted_path).encode("utf-16le")).decode("ascii")
    script = (
        "$ProgressPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
        "Import-Module (Join-Path $PSHOME "
        "'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1') "
        "-Force -ErrorAction Stop;"
        f"$p=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{encoded_path}'));"
        "$s=Get-AuthenticodeSignature -LiteralPath $p;"
        "$v=(Get-Item -LiteralPath $p).VersionInfo;"
        "[ordered]@{status=[string]$s.Status;"
        "signer=$s.SignerCertificate.Subject;"
        "timestamp=$s.TimeStamperCertificate.Subject;"
        "company=$v.CompanyName;product=$v.ProductName;"
        "product_version=$v.ProductVersion;file_version=$v.FileVersion;"
        "original_filename=$v.OriginalFilename}|ConvertTo-Json -Compress"
    )
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    output = _run_native_trust_probe(
        powershell,
        encoded_script,
        timeout_seconds=_STARTUP_BINARY_TRUST_SECONDS,
    )
    try:
        evidence = json.loads(output)
    except json.JSONDecodeError as exc:
        raise OSError("AutoCAD binary trust verification returned invalid JSON") from exc
    if not isinstance(evidence, dict):
        raise OSError("AutoCAD binary trust verification returned invalid evidence")
    return cast(dict[str, Any], evidence)


def _verify_production_autocad_binary(
    executable: Path,
    versioned_prog_id: str,
) -> Path:
    """Require a fixed local Autodesk binary with valid Authenticode and R-version."""
    trusted_path = _validate_local_nonreparse_executable(executable)
    expected_release = f"R{versioned_prog_id.split('.')[2]}"
    try:
        evidence = _read_production_binary_evidence(trusted_path)
    except (OSError, TimeoutError, ValueError) as exc:
        raise OSError("AutoCAD binary trust verification failed") from exc
    if not isinstance(evidence, dict):
        raise OSError("AutoCAD binary trust verification returned invalid evidence")
    if not _binary_evidence_satisfies_policy(evidence, expected_release=expected_release):
        raise OSError("Registered acad.exe does not satisfy the production trust policy")
    return trusted_path


def _binary_evidence_satisfies_policy(
    evidence: dict[str, Any],
    *,
    expected_release: str,
) -> bool:
    signer = evidence.get("signer")
    timestamp = evidence.get("timestamp")
    product_version = evidence.get("product_version")
    file_version = evidence.get("file_version")
    original_filename = evidence.get("original_filename")
    return (
        evidence.get("status") == "Valid"
        and isinstance(signer, str)
        and re.search(
            r'(?:^|,\s*)O=(?:"Autodesk, Inc\."|Autodesk, Inc\.)(?=,|$)',
            signer,
        )
        is not None
        and isinstance(timestamp, str)
        and bool(timestamp.strip())
        and evidence.get("company") == "Autodesk, Inc."
        and evidence.get("product") == "AutoCAD"
        and isinstance(original_filename, str)
        and original_filename.casefold() == "acad.exe"
        and isinstance(product_version, str)
        and product_version.startswith(f"{expected_release}.")
        and isinstance(file_version, str)
        and file_version.startswith(f"{expected_release}.")
    )


def _resolve_local_server_executable(
    versioned_prog_id: str,
    *,
    binary_verifier: _BinaryTrustVerifier = _verify_production_autocad_binary,
) -> Path:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{versioned_prog_id}\\CLSID") as key:
            clsid = winreg.QueryValueEx(key, "")[0]
        if not isinstance(clsid, str) or re.fullmatch(r"\{[0-9A-Fa-f-]{36}\}", clsid) is None:
            raise ValueError("invalid AutoCAD CLSID")
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"CLSID\\{clsid}\\LocalServer32") as key:
            command = winreg.QueryValueEx(key, "")[0]
    except OSError as exc:
        raise OSError("Versioned AutoCAD LocalServer32 registration is unavailable") from exc
    if not isinstance(command, str):
        raise OSError("AutoCAD LocalServer32 registration is invalid")
    return _executable_from_local_server_command(
        command,
        versioned_prog_id,
        binary_verifier=binary_verifier,
    )


def _executable_from_local_server_command(
    command: str,
    versioned_prog_id: str,
    *,
    binary_verifier: _BinaryTrustVerifier = _verify_production_autocad_binary,
) -> Path:
    match = _LOCAL_SERVER_EXE.match(command)
    if match is None:
        raise OSError("AutoCAD LocalServer32 executable is invalid")
    executable = Path(match.group("quoted") or match.group("plain"))
    return binary_verifier(executable, versioned_prog_id)


def _autocad_owned_automation_command_line(executable: Path) -> str:
    """Create the registered local server with the fixed COM embedding switches."""
    return f'"{executable}" /Automation -Embedding'


def _create_autocad_process(executable: Path) -> tuple[int, int]:
    """Create the exact registered executable after the helper entered its kill job."""
    import ctypes
    from ctypes import wintypes

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
    command_line = ctypes.create_unicode_buffer(_autocad_owned_automation_command_line(executable))
    if not ctypes.windll.kernel32.CreateProcessW(
        str(executable),
        command_line,
        None,
        None,
        False,
        0,
        None,
        str(executable.parent),
        ctypes.byref(startup),
        ctypes.byref(process),
    ):
        raise OSError("Could not create the registered AutoCAD automation process")
    ctypes.windll.kernel32.CloseHandle(process.hThread)
    return int(process.dwProcessId), int(process.hProcess)


def _terminate_process_handle(process_handle: int) -> None:
    import ctypes

    try:
        if not ctypes.windll.kernel32.TerminateProcess(process_handle, 1):
            raise OSError("Could not terminate the unassigned AutoCAD startup process")
        if ctypes.windll.kernel32.WaitForSingleObject(process_handle, 5_000) != 0:
            raise TimeoutError("Unassigned AutoCAD startup process did not terminate")
    finally:
        ctypes.windll.kernel32.CloseHandle(process_handle)


def _same_windows_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def _application_version_matches_prog_id(version: str, versioned_prog_id: str) -> bool:
    release = versioned_prog_id.split(".")[2]
    return re.match(rf"^R?{re.escape(release)}(?:\.|$)", version.strip(), re.IGNORECASE) is not None


def _workspace_acceptance_plugins_root() -> Path:
    return (
        Path(__file__).resolve(strict=True).parents[3] / "data" / "live-r26" / "ApplicationPlugins"
    ).resolve(strict=True)


def _is_reparse_path(path: Path) -> bool:
    if path.is_symlink():
        return True
    return sys.platform == "win32" and bool(
        _windows_file_attributes(path) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


@dataclass(frozen=True, slots=True)
class _WindowsFileIdentity:
    volume_serial_number: int
    file_index: int
    file_size: int


class _LockedAcceptanceArtifact:
    """One verified file handle that denies concurrent write and delete sharing."""

    __slots__ = ("_identity", "_stream", "path")

    def __init__(self, path: Path, stream: Any, identity: _WindowsFileIdentity) -> None:
        self.path = path
        self._stream = stream
        self._identity = identity

    @property
    def closed(self) -> bool:
        return bool(self._stream.closed)

    @property
    def identity(self) -> _WindowsFileIdentity:
        return self._identity

    def read_limited(self, maximum_bytes: int) -> bytes:
        self._stream.seek(0)
        value = self._stream.read(maximum_bytes + 1)
        self._stream.seek(0)
        if len(value) > maximum_bytes:
            raise OSError("Acceptance bundle metadata file is too large")
        if _windows_file_identity_from_stream(self._stream) != self._identity:
            raise OSError("Acceptance bundle artifact identity changed during verification")
        return bytes(value)

    def sha256(self) -> str:
        digest = hashlib.sha256()
        self._stream.seek(0)
        while chunk := self._stream.read(1024 * 1024):
            digest.update(chunk)
        self._stream.seek(0)
        if _windows_file_identity_from_stream(self._stream) != self._identity:
            raise OSError("Acceptance bundle artifact identity changed during verification")
        return digest.hexdigest()

    def close(self) -> None:
        self._stream.close()


class _AcceptanceBundleLease:
    """Keep the installer fence and trust artifacts locked through NETLOAD proof.

    The transaction lock fences the sanctioned installer/build workflow. It is not an
    ACL boundary against arbitrary same-user code that directly mutates the bundle;
    the post-proof inventory, identity, and hash check detects such persistent drift.
    """

    __slots__ = (
        "_artifacts",
        "_bundle",
        "_expected_directories",
        "_expected_files",
        "_expected_hashes",
        "_installer_lock",
        "bridge_path",
        "closed",
    )

    def __init__(
        self,
        bundle: Path,
        bridge_path: Path,
        installer_lock: _LockedAcceptanceArtifact,
        artifacts: tuple[_LockedAcceptanceArtifact, ...],
        expected_files: frozenset[str],
        expected_directories: frozenset[str],
        expected_hashes: tuple[tuple[_LockedAcceptanceArtifact, str], ...],
    ) -> None:
        self._bundle = bundle
        self.bridge_path = bridge_path
        self._installer_lock = installer_lock
        self._artifacts = artifacts
        self._expected_files = expected_files
        self._expected_directories = expected_directories
        self._expected_hashes = expected_hashes
        self.closed = False

    def __enter__(self) -> _AcceptanceBundleLease:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def revalidate_after_load_proof(self) -> None:
        """Prove the receipt-bound filesystem snapshot still names the locked files."""
        if self.closed:
            raise OSError("Acceptance bundle lease is already closed")
        try:
            current_bundle = self._bundle.resolve(strict=True)
        except OSError as exc:
            raise OSError("Acceptance bundle changed during bridge loading") from exc
        if not _same_windows_path(current_bundle, self._bundle):
            raise OSError("Acceptance bundle changed during bridge loading")
        actual_files, actual_directories = _acceptance_bundle_inventory(self._bundle)
        if actual_files != self._expected_files or actual_directories != self._expected_directories:
            raise OSError("Acceptance bundle changed during bridge loading")
        for artifact, expected_hash in self._expected_hashes:
            if artifact.sha256() != expected_hash:
                raise OSError("Acceptance bundle changed during bridge loading")
            current = _open_locked_acceptance_artifact(artifact.path)
            try:
                if current.identity != artifact.identity or current.sha256() != expected_hash:
                    raise OSError("Acceptance bundle changed during bridge loading")
            finally:
                current.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for artifact in reversed(self._artifacts):
            with contextlib.suppress(Exception):
                artifact.close()
        with contextlib.suppress(Exception):
            self._installer_lock.close()


def _windows_file_identity(handle: int) -> _WindowsFileIdentity:
    if sys.platform != "win32":
        raise OSError("Acceptance bundle locking requires Windows")
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    information = ByHandleFileInformation()
    if not ctypes.windll.kernel32.GetFileInformationByHandle(
        wintypes.HANDLE(handle), ctypes.byref(information)
    ):
        raise ctypes.WinError()
    if int(information.dwFileAttributes) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError("Acceptance bundle artifacts may not be reparse points")
    return _WindowsFileIdentity(
        volume_serial_number=int(information.dwVolumeSerialNumber),
        file_index=(int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow),
        file_size=(int(information.nFileSizeHigh) << 32) | int(information.nFileSizeLow),
    )


def _windows_file_identity_from_stream(stream: Any) -> _WindowsFileIdentity:
    import msvcrt

    return _windows_file_identity(int(msvcrt.get_osfhandle(stream.fileno())))


def _windows_final_path(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(
        wintypes.HANDLE(handle), buffer, len(buffer), 0
    )
    if length == 0 or length >= len(buffer):
        raise ctypes.WinError()
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _open_locked_windows_file(
    path: Path,
    *,
    share_mode: int,
) -> _LockedAcceptanceArtifact:
    """Open an exact local file with the requested Windows share fence."""
    if sys.platform != "win32":
        raise OSError("Acceptance bundle locking requires Windows")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    invalid_handle = ctypes.c_void_p(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        generic_read,
        share_mode,
        None,
        open_existing,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle:
        raise ctypes.WinError()
    opened_handle = int(handle)
    raw_handle: int | None = opened_handle
    try:
        expected = path.resolve(strict=True)
        if not _same_windows_path(_windows_final_path(opened_handle), expected):
            raise OSError("Acceptance bundle artifact resolved to an unexpected file")
        identity = _windows_file_identity(opened_handle)
        descriptor = msvcrt.open_osfhandle(
            opened_handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        raw_handle = None
        try:
            stream = os.fdopen(descriptor, "rb", buffering=0)
        except Exception:
            os.close(descriptor)
            raise
        return _LockedAcceptanceArtifact(expected, stream, identity)
    finally:
        if raw_handle is not None:
            kernel32.CloseHandle(wintypes.HANDLE(raw_handle))


def _open_locked_acceptance_artifact(path: Path) -> _LockedAcceptanceArtifact:
    """Open one receipt-bound file while denying concurrent write and delete."""
    file_share_read = 0x00000001
    return _open_locked_windows_file(path, share_mode=file_share_read)


def _open_exclusive_installer_transaction_lock(
    plugins_root: Path,
) -> _LockedAcceptanceArtifact:
    """Join the installer's persistent root lock with Windows FileShare.None."""
    lock_path = plugins_root / _ACCEPTANCE_INSTALLER_LOCK_NAME
    if not lock_path.is_file():
        raise OSError("Acceptance installer transaction lock is missing")
    if lock_path.parent != plugins_root or _is_reparse_path(lock_path):
        raise OSError("Acceptance installer transaction lock is unsafe")
    try:
        return _open_locked_windows_file(lock_path, share_mode=0)
    except OSError as exc:
        raise OSError("Acceptance install root is busy or its lock is invalid") from exc


def _configured_acceptance_bundle_root() -> Path:
    configured_root = os.environ.get("CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT")
    if not configured_root:
        raise OSError(
            "CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT is required; legacy APPDATA NETLOAD is disabled"
        )
    windows_path = PureWindowsPath(configured_root)
    if (
        not windows_path.is_absolute()
        or configured_root.startswith(("\\\\", "//"))
        or (
            sys.platform == "win32"
            and _windows_drive_type(f"{windows_path.drive}\\") != _DRIVE_FIXED
        )
    ):
        raise OSError("Acceptance bundle root must be on an absolute fixed local drive")
    return Path(configured_root)


def _validated_acceptance_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise OSError("Acceptance bundle inventory contains an invalid relative path")
    if len(value.encode("utf-8")) > _RPC_MAX_STRING_BYTES:
        raise OSError("Acceptance bundle inventory path is too large")
    relative = PureWindowsPath(value)
    parts = relative.parts
    normalized = "/".join(parts)
    if (
        relative.is_absolute()
        or not parts
        or len(parts) > _RPC_MAX_DEPTH
        or normalized != value
        or any(part in {"", ".", ".."} for part in parts)
        or ":" in value
        or "\\" in value
    ):
        raise OSError("Acceptance bundle inventory contains an unsafe relative path")
    return normalized


def _acceptance_bundle_inventory(
    bundle: Path,
    *,
    maximum_entries: int = _RPC_MAX_ITEMS,
) -> tuple[set[str], set[str]]:
    """Enumerate one bounded, regular, non-reparse bundle tree without following links."""
    if maximum_entries < 1:
        raise ValueError("Acceptance bundle inventory limit must be positive")
    files: set[str] = set()
    directories: set[str] = set()
    canonical_paths: set[str] = set()
    pending = [bundle]
    entry_count = 0
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > maximum_entries:
                        raise OSError("Acceptance bundle inventory exceeds its entry limit")
                    candidate = current / entry.name
                    if _is_reparse_path(candidate):
                        raise OSError("Acceptance bundle inventory may not contain reparse points")
                    relative = _validated_acceptance_relative_path(
                        candidate.relative_to(bundle).as_posix()
                    )
                    canonical = relative.casefold()
                    if canonical in canonical_paths:
                        raise OSError("Acceptance bundle inventory contains duplicate paths")
                    canonical_paths.add(canonical)
                    if entry.is_dir(follow_symlinks=False):
                        directories.add(relative)
                        pending.append(candidate)
                    elif entry.is_file(follow_symlinks=False):
                        files.add(relative)
                    else:
                        raise OSError("Acceptance bundle inventory contains a non-regular entry")
        except OSError:
            raise
        except Exception as exc:
            raise OSError("Acceptance bundle inventory is unreadable") from exc
    return files, directories


def _acquire_installed_acceptance_bundle_while_installer_locked(
    bundle_root: Path,
    plugins_root: Path,
    installer_lock: _LockedAcceptanceArtifact,
) -> _AcceptanceBundleLease:
    bundle = bundle_root.resolve(strict=True)
    if bundle.name != "AutoCADHarness.bundle" or bundle.parent != plugins_root:
        raise OSError("Acceptance bundle is outside the dedicated workspace install root")
    bridge = (bundle / "Contents" / "Windows" / "AutoCADHarness.dll").resolve(strict=True)
    receipt_path = (bundle / _ACCEPTANCE_RECEIPT_NAME).resolve(strict=True)
    checksum_path = (bundle / _ACCEPTANCE_CHECKSUM_NAME).resolve(strict=True)
    for candidate in (
        plugins_root.parent.parent,
        plugins_root.parent,
        plugins_root,
        bundle,
        bundle / "Contents",
        bundle / "Contents" / "Windows",
        bridge,
        receipt_path,
        checksum_path,
    ):
        if _is_reparse_path(candidate):
            raise OSError("Acceptance bundle may not traverse a reparse point")
    if not bridge.is_file() or not receipt_path.is_file() or not checksum_path.is_file():
        raise OSError("Acceptance bundle receipt, checksum, or bridge DLL is missing")
    locked: dict[str, _LockedAcceptanceArtifact] = {}
    try:
        for metadata_path in (receipt_path, checksum_path):
            locked[os.path.normcase(str(metadata_path))] = _open_locked_acceptance_artifact(
                metadata_path
            )
        try:
            receipt_bytes = locked[os.path.normcase(str(receipt_path))].read_limited(
                _RPC_MAX_BYTES_VALUE
            )
            receipt = json.loads(receipt_bytes.decode("utf-8"))
            _validate_rpc_tree(receipt)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise OSError("Acceptance bundle receipt is invalid") from exc
        if (
            not isinstance(receipt, dict)
            or set(receipt) != _ACCEPTANCE_RECEIPT_FIELDS
            or (
                receipt.get("SchemaVersion") != "2.0"
                or receipt.get("Owner") != "autocad-mechanical-harness"
                or receipt.get("BundleName") != "AutoCADHarness.bundle"
                or receipt.get("ArtifactKind") != "DEVELOPMENT-UNSIGNED"
                or receipt.get("AutoCADSeries") != "R26.0"
            )
        ):
            raise OSError("Acceptance bundle receipt does not match the development install")
        checksum_bytes = locked[os.path.normcase(str(checksum_path))].read_limited(
            _RPC_MAX_BYTES_VALUE
        )
        checksum_sha256 = hashlib.sha256(checksum_bytes).hexdigest()
        if receipt.get("ChecksumManifestSha256") != checksum_sha256:
            raise OSError("Acceptance bundle receipt does not bind the checksum manifest")
        checksums: dict[str, str] = {}
        checksum_paths: set[str] = set()
        try:
            for line in checksum_bytes.decode("utf-8").splitlines():
                match = re.fullmatch(r"# SHA256 ([0-9a-f]{64}) \*(.+)", line)
                if match is None:
                    raise ValueError("invalid checksum line")
                relative = _validated_acceptance_relative_path(match.group(2))
                canonical = relative.casefold()
                if relative in checksums or canonical in checksum_paths:
                    raise ValueError("unsafe checksum path")
                checksums[relative] = match.group(1)
                checksum_paths.add(canonical)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise OSError("Acceptance bundle checksum manifest is invalid") from exc
        if _ACCEPTANCE_BRIDGE_RELATIVE not in checksums:
            raise OSError("Acceptance bridge DLL is not checksum-bound")
        receipt_files = receipt.get("Files")
        if not isinstance(receipt_files, list):
            raise OSError("Acceptance receipt file inventory does not match its checksums")
        receipt_checksums: dict[str, str] = {}
        receipt_paths: set[str] = set()
        for item in receipt_files:
            if (
                not isinstance(item, dict)
                or set(item) != {"RelativePath", "Sha256"}
                or not isinstance(item["RelativePath"], str)
                or not isinstance(item["Sha256"], str)
            ):
                raise OSError("Acceptance receipt file inventory is invalid")
            relative = _validated_acceptance_relative_path(item["RelativePath"])
            canonical = relative.casefold()
            if relative in receipt_checksums or canonical in receipt_paths:
                raise OSError("Acceptance receipt file inventory contains duplicate paths")
            receipt_checksums[relative] = item["Sha256"]
            receipt_paths.add(canonical)
        if receipt_checksums != checksums:
            raise OSError("Acceptance receipt file inventory does not match its checksums")
        receipt_directories = receipt.get("Directories")
        if not isinstance(receipt_directories, list):
            raise OSError("Acceptance receipt directory inventory is invalid")
        expected_directories: set[str] = set()
        directory_paths: set[str] = set()
        for item in receipt_directories:
            relative = _validated_acceptance_relative_path(item)
            canonical = relative.casefold()
            if relative in expected_directories or canonical in directory_paths:
                raise OSError("Acceptance receipt directory inventory contains duplicate paths")
            expected_directories.add(relative)
            directory_paths.add(canonical)
        for relative, expected_hash in checksums.items():
            lexical = bundle / Path(*PureWindowsPath(relative).parts)
            current = bundle
            for part in PureWindowsPath(relative).parts[:-1]:
                current /= part
                if _is_reparse_path(current):
                    raise OSError("Acceptance checksum traverses an unsafe directory")
            candidate = lexical.resolve(strict=True)
            if (
                bundle not in candidate.parents
                or not candidate.is_file()
                or _is_reparse_path(candidate)
            ):
                raise OSError("Acceptance checksum references an unsafe file")
            key = os.path.normcase(str(candidate))
            artifact = locked.get(key)
            if artifact is None:
                artifact = _open_locked_acceptance_artifact(candidate)
                locked[key] = artifact
            if artifact.sha256() != expected_hash:
                raise OSError("Acceptance bundle checksum verification failed")
        actual_files, actual_directories = _acceptance_bundle_inventory(bundle)
        expected_files = set(checksums) | {
            _ACCEPTANCE_RECEIPT_NAME,
            _ACCEPTANCE_CHECKSUM_NAME,
        }
        if actual_files != expected_files or actual_directories != expected_directories:
            raise OSError("Acceptance bundle filesystem inventory does not match its receipt")
        bridge_key = os.path.normcase(str(bridge))
        if bridge_key not in locked:
            raise OSError("Acceptance bridge DLL lock was not acquired")
        if any(character in str(bridge) for character in ('"', "\r", "\n")):
            raise OSError("The fixed acceptance bridge path is not command-safe")
        artifacts = tuple(locked.values())
        expected_hashes = tuple((artifact, artifact.sha256()) for artifact in artifacts)
        return _AcceptanceBundleLease(
            bundle,
            bridge,
            installer_lock,
            artifacts,
            frozenset(expected_files),
            frozenset(expected_directories),
            expected_hashes,
        )
    except BaseException:
        for artifact in reversed(tuple(locked.values())):
            with contextlib.suppress(Exception):
                artifact.close()
        raise


def _acquire_installed_acceptance_bundle(bundle_root: Path) -> _AcceptanceBundleLease:
    plugins_root = _workspace_acceptance_plugins_root()
    installer_lock = _open_exclusive_installer_transaction_lock(plugins_root)
    try:
        return _acquire_installed_acceptance_bundle_while_installer_locked(
            bundle_root,
            plugins_root,
            installer_lock,
        )
    except BaseException:
        installer_lock.close()
        raise


def _acquire_acceptance_bundle() -> _AcceptanceBundleLease:
    return _acquire_installed_acceptance_bundle(_configured_acceptance_bundle_root())


def _acceptance_bridge_path() -> Path:
    with _acquire_acceptance_bundle() as lease:
        return lease.bridge_path


def _expected_acceptance_netload_command() -> str:
    return f'_.NETLOAD\n"{_acceptance_bridge_path()}"\n'


def _acceptance_netload_request_is_fixed(command: str) -> bool:
    try:
        return command == _expected_acceptance_netload_command()
    except OSError:
        return False


def _acceptance_bridge_pipe_name() -> str:
    from cad_harness.adapters.named_pipe_transport import resolve_current_user_pipe_name

    template = os.environ.get("CAD_HARNESS_BRIDGE_PIPE_NAME_TEMPLATE", "cadharness.{user_sid}")
    return resolve_current_user_pipe_name(template)


class _AcceptanceBridgePipeBusyError(OSError):
    """The bridge pipe exists, but no instance is available for PID verification."""


def _acceptance_bridge_server_pid() -> int | None:
    """Return the exact named-pipe server PID without sending a bridge request."""
    if sys.platform != "win32":
        raise OSError("Acceptance bridge load proof requires Windows")
    import ctypes
    from ctypes import wintypes

    open_existing = 3
    file_attribute_normal = 0x00000080
    error_file_not_found = 2
    error_pipe_busy = 231
    invalid_handle = ctypes.c_void_p(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        _acceptance_bridge_pipe_name(),
        0,
        0,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        if error == error_file_not_found:
            return None
        if error == error_pipe_busy:
            raise _AcceptanceBridgePipeBusyError(
                "Acceptance bridge pipe exists but its server PID cannot be verified"
            )
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


def _assert_acceptance_bridge_absent_before_netload() -> None:
    server_pid = _acceptance_bridge_server_pid()
    if server_pid is not None:
        raise OSError("Acceptance bridge pipe existed before NETLOAD; load provenance is ambiguous")


def _wait_for_owned_acceptance_bridge(
    expected_autocad_pid: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    if expected_autocad_pid <= 0 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Acceptance bridge proof requires a valid PID and timeout")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            server_pid = _acceptance_bridge_server_pid()
        except _AcceptanceBridgePipeBusyError:
            time.sleep(0.05)
            continue
        if server_pid is None:
            time.sleep(0.05)
            continue
        if server_pid != expected_autocad_pid:
            raise OSError("Acceptance bridge pipe belongs to another process")
        return {"bridge_loaded": True, "server_pid": server_pid}
    raise TimeoutError("Owned AutoCAD bridge pipe did not become ready after NETLOAD")


def _find_job_owned_application(
    job_handle: int,
    timeout_seconds: float,
    *,
    expected_pid: int,
    expected_executable: Path,
    versioned_prog_id: str,
) -> tuple[Any, int]:
    import pythoncom  # noqa: TID251 - COM is confined to this module
    import win32com.client  # noqa: TID251

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        table = pythoncom.GetRunningObjectTable()
        monikers = table.EnumRunning()
        while True:
            batch = monikers.Next(1)
            if not batch:
                break
            try:
                app = win32com.client.Dispatch(table.GetObject(batch[0]))
                hwnd = int(app.HWND)
                pid = ComAutoCADAdapter._pid_from_hwnd(hwnd)
            except Exception:
                continue
            if not _process_is_in_job(pid, job_handle):
                continue
            if pid != expected_pid:
                raise OSError("The startup job published an unexpected AutoCAD ROT process")
            image_path, _creation_time_100ns = ComAutoCADAdapter._process_identity(pid)
            if not _same_windows_path(image_path, expected_executable):
                raise OSError("The created AutoCAD ROT application image changed")
            if not _application_version_matches_prog_id(str(app.Version), versioned_prog_id):
                raise OSError(
                    "The created AutoCAD ROT application release does not match its ProgID"
                )
            return app, hwnd
        time.sleep(0.1)
    raise TimeoutError("The launched AutoCAD process did not publish its ROT application")


def _marshal_dispatch(app: Any) -> bytes:
    import pythoncom  # noqa: TID251 - COM is confined to this module

    stream = pythoncom.CreateStreamOnHGlobal()
    pythoncom.CoMarshalInterface(
        stream,
        pythoncom.IID_IDispatch,
        app._oleobj_,
        pythoncom.MSHCTX_LOCAL,
        pythoncom.MSHLFLAGS_NORMAL,
    )
    size = int(stream.Stat()[2])
    if not 0 < size <= _STARTUP_IPC_MAX_BYTES:
        raise OSError("Marshaled AutoCAD application exceeded the startup IPC limit")
    stream.Seek(0, 0)
    return cast(bytes, stream.Read(size))


def _unmarshal_dispatch(marshaled: bytes) -> Any:
    import pythoncom  # noqa: TID251 - COM is confined to this module
    import win32com.client  # noqa: TID251

    stream = pythoncom.CreateStreamOnHGlobal()
    stream.Write(marshaled)
    stream.Seek(0, 0)
    return win32com.client.Dispatch(pythoncom.CoUnmarshalInterface(stream, pythoncom.IID_IDispatch))


def _validate_rpc_tree(value: Any) -> None:
    item_count = 0

    def visit(current: Any, depth: int) -> None:
        nonlocal item_count
        if depth > _RPC_MAX_DEPTH:
            raise ValueError("AutoCAD RPC JSON tree is too deep")
        item_count += 1
        if item_count > _RPC_MAX_ITEMS:
            raise ValueError("AutoCAD RPC JSON tree has too many items")
        if current is None or isinstance(current, bool | int):
            return
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("AutoCAD RPC JSON contains a non-finite number")
            return
        if isinstance(current, str):
            if len(current.encode("utf-8")) > _RPC_MAX_STRING_BYTES:
                raise ValueError("AutoCAD RPC JSON string is too large")
            return
        if isinstance(current, bytes):
            if len(current) > _RPC_MAX_BYTES_VALUE:
                raise ValueError("AutoCAD RPC byte value is too large")
            return
        if isinstance(current, list | tuple):
            if len(current) > _RPC_MAX_ITEMS:
                raise ValueError("AutoCAD RPC JSON collection is too large")
            for item in current:
                visit(item, depth + 1)
            return
        if isinstance(current, dict):
            if len(current) > _RPC_MAX_ITEMS:
                raise ValueError("AutoCAD RPC JSON object is too large")
            for key, item in current.items():
                if not isinstance(key, str):
                    raise ValueError("AutoCAD RPC JSON object keys must be strings")
                visit(key, depth + 1)
                visit(item, depth + 1)
            return
        raise ValueError(f"Unsupported AutoCAD RPC JSON value: {type(current).__name__}")

    visit(value, 0)


def _encode_remote_argument(value: Any, owner: _ComProcessOwner) -> Any:
    item_count = 0

    def encode(current: Any, depth: int) -> Any:
        nonlocal item_count
        if depth > _RPC_MAX_DEPTH:
            raise ValueError("AutoCAD RPC argument tree is too deep")
        item_count += 1
        if item_count > _RPC_MAX_ITEMS:
            raise ValueError("AutoCAD RPC argument tree has too many items")
        if isinstance(current, _ComRemoteObject):
            current._assert_live()
            if current._owner is not owner:
                raise ComCallFailedError(
                    "COM object belongs to another isolated apartment",
                    details={"reason": "com_apartment_ownership_mismatch"},
                )
            return {"$remote_object": current._object_id}
        if isinstance(current, _RemoteVariant):
            return {
                "$variant": current.kind,
                "values": [encode(item, depth + 1) for item in current.values],
            }
        if current is None or isinstance(current, bool | int):
            return current
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("AutoCAD RPC arguments must be finite")
            return current
        if isinstance(current, str):
            if len(current.encode("utf-8")) > _RPC_MAX_STRING_BYTES:
                raise ValueError("AutoCAD RPC argument string is too large")
            return current
        if isinstance(current, list | tuple):
            return [encode(item, depth + 1) for item in current]
        if isinstance(current, dict) and all(isinstance(key, str) for key in current):
            return {key: encode(item, depth + 1) for key, item in current.items()}
        raise TypeError(f"Unsupported isolated COM argument type: {type(current).__name__}")

    encoded = encode(value, 0)
    _validate_rpc_tree(encoded)
    return encoded


def _is_known_remote_scope(scope: str) -> bool:
    return scope in {
        _SCOPE_APPLICATION,
        _SCOPE_DOCUMENTS,
        _SCOPE_DOCUMENT,
        _SCOPE_ACAD_STATE,
        _SCOPE_LAYERS,
        _SCOPE_LAYER,
        _SCOPE_STYLES,
        _SCOPE_STYLE,
        _SCOPE_MODEL_SPACE,
        _SCOPE_SELECTION,
        _SCOPE_ENTITY,
        _SCOPE_PLOT,
    } or (
        scope.startswith(_SCOPE_ITERATOR_PREFIX)
        and scope.removeprefix(_SCOPE_ITERATOR_PREFIX) in _RPC_ITER_ITEM_SCOPES.values()
    )


def _decode_remote_value(value: Any, owner: _ComProcessOwner) -> Any:
    if isinstance(value, dict) and set(value) == {"$remote_object", "scope"}:
        object_id = value["$remote_object"]
        scope = value["scope"]
        if (
            isinstance(object_id, bool)
            or not isinstance(object_id, int)
            or object_id <= 0
            or not isinstance(scope, str)
            or not _is_known_remote_scope(scope)
        ):
            raise ComCallFailedError(
                "Isolated COM helper returned invalid object metadata",
                details={"reason": "isolated_com_invalid_response"},
            )
        return _ComRemoteObject(owner, object_id, scope)
    if isinstance(value, list):
        return tuple(_decode_remote_value(item, owner) for item in value)
    if isinstance(value, dict):
        return {key: _decode_remote_value(item, owner) for key, item in value.items()}
    return value


class _ComProcessOwner:
    """Own one killable process whose single COM apartment holds every proxy."""

    def __init__(
        self,
        connection: Any,
        process: _StartupProcess,
        job: _StartupJob,
        *,
        call_timeout_seconds: float,
        expected_autocad_pid: int,
    ) -> None:
        self._connection = connection
        self._process = process
        self._job = job
        self._call_timeout_seconds = call_timeout_seconds
        self._expected_autocad_pid = expected_autocad_pid
        self._thread_id = threading.get_ident()
        self._closed = False
        self._cleanup_state: tuple[bool, bool] | None = None
        self._acceptance_bundle_lease: _AcceptanceBundleLease | None = None

    def _assert_owner_thread(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise ComCallFailedError(
                "Isolated COM apartment was accessed from a non-owner thread",
                details={"reason": "com_apartment_ownership_mismatch"},
            )

    def request(self, payload: dict[str, Any]) -> Any:
        self._assert_owner_thread()
        if self._closed:
            raise ComCallFailedError(
                "Isolated COM apartment is closed",
                details={"reason": "isolated_com_owner_closed"},
            )
        try:
            self._connection.send_bytes(_startup_message(payload))
            if not self._connection.poll(self._call_timeout_seconds):
                raise TimeoutError
            envelope = _read_startup_message(self._connection)
        except BaseException as exc:
            helper_terminal, job_terminal = self._terminal_close()
            if not isinstance(exc, Exception):
                raise
            raise ComCallFailedError(
                "Isolated COM call did not complete before its deadline",
                details={
                    "reason": (
                        "isolated_com_call_timeout"
                        if helper_terminal and job_terminal
                        else "isolated_com_cleanup_unconfirmed"
                    ),
                    "helper_terminal": helper_terminal,
                    "job_terminal": job_terminal,
                },
            ) from exc
        if set(envelope) == {"stage", "value"} and envelope.get("stage") == "result":
            return _decode_remote_value(envelope["value"], self)
        if envelope == {"stage": "stopped"}:
            return None
        helper_terminal, job_terminal = self._terminal_close()
        raise ComCallFailedError(
            "Isolated COM helper rejected the operation",
            details={
                "reason": (
                    "isolated_com_call_failed"
                    if helper_terminal and job_terminal
                    else "isolated_com_cleanup_unconfirmed"
                ),
                "operation": payload.get("op", "unknown"),
                "helper_terminal": helper_terminal,
                "job_terminal": job_terminal,
            },
        )

    def acceptance_netload(self, object_id: int, command: str) -> None:
        """Hold the exact verified bundle until its pipe is proven in our AutoCAD PID."""
        self._assert_owner_thread()
        if self._closed:
            raise ComCallFailedError(
                "Isolated COM apartment is closed",
                details={"reason": "isolated_com_owner_closed"},
            )
        lease = _acquire_acceptance_bundle()
        expected_command = f'_.NETLOAD\n"{lease.bridge_path}"\n'
        if command != expected_command:
            lease.close()
            raise ComCallFailedError(
                "Only the fixed disposable-acceptance NETLOAD operation is permitted",
                details={"reason": "isolated_com_member_not_allowed"},
            )
        if self._acceptance_bundle_lease is not None:
            lease.close()
            raise ComCallFailedError(
                "An acceptance NETLOAD operation is already active",
                details={"reason": "isolated_com_call_failed"},
            )
        self._acceptance_bundle_lease = lease
        expected_evidence = {
            "bridge_loaded": True,
            "server_pid": self._expected_autocad_pid,
        }
        try:
            evidence = self.request({"op": "acceptance_netload", "object_id": object_id})
        except BaseException:
            self._terminal_close()
            raise
        if evidence != expected_evidence:
            helper_terminal, job_terminal = self._terminal_close()
            raise ComCallFailedError(
                "Isolated COM helper returned invalid bridge-load evidence",
                details={
                    "reason": (
                        "isolated_com_invalid_response"
                        if helper_terminal and job_terminal
                        else "isolated_com_cleanup_unconfirmed"
                    ),
                    "helper_terminal": helper_terminal,
                    "job_terminal": job_terminal,
                },
            )
        try:
            lease.revalidate_after_load_proof()
        except BaseException as exc:
            helper_terminal, job_terminal = self._terminal_close()
            if not isinstance(exc, Exception):
                raise
            raise ComCallFailedError(
                "Acceptance bundle changed while the bridge was loading",
                details={
                    "reason": (
                        "acceptance_bundle_changed_during_load"
                        if helper_terminal and job_terminal
                        else "isolated_com_cleanup_unconfirmed"
                    ),
                    "helper_terminal": helper_terminal,
                    "job_terminal": job_terminal,
                },
            ) from exc
        self._release_acceptance_bundle_lease()

    def _release_acceptance_bundle_lease(self) -> None:
        lease = self._acceptance_bundle_lease
        self._acceptance_bundle_lease = None
        if lease is not None:
            lease.close()

    def shutdown(self) -> None:
        self._assert_owner_thread()
        if self._closed:
            return
        helper_terminal, job_terminal = self._terminal_close()
        if not helper_terminal or not job_terminal:
            raise ComCallFailedError(
                "Isolated AutoCAD process cleanup could not be confirmed",
                details={
                    "reason": "isolated_com_cleanup_unconfirmed",
                    "helper_terminal": helper_terminal,
                    "job_terminal": job_terminal,
                },
            )

    def shutdown_after_verified_quit(self) -> None:
        """Release COM, prove the job empty, then and only then disarm kill-on-close."""
        self._assert_owner_thread()
        if self._closed:
            return
        try:
            self.request({"op": "shutdown"})
            self._process.join(self._call_timeout_seconds)
            helper_terminal = not self._process.is_alive()
            job_terminal = helper_terminal and self._job.wait_until_empty(
                self._call_timeout_seconds
            )
        except Exception:
            if self._closed:
                raise
            helper_terminal = False
            job_terminal = False
        if helper_terminal and job_terminal:
            self._closed = True
            self._cleanup_state = (True, True)
            self._release_acceptance_bundle_lease()
            with contextlib.suppress(Exception):
                self._connection.close()
            try:
                self._job.disarm()
            finally:
                self._job.close()
            return
        forced_helper, forced_job = self._terminal_close()
        raise ComCallFailedError(
            "Owned AutoCAD did not exit cleanly after Quit",
            details={
                "reason": "owned_session_graceful_exit_unconfirmed",
                "helper_terminal": forced_helper,
                "job_terminal": forced_job,
            },
        )

    def _terminal_close(self) -> tuple[bool, bool]:
        if self._closed:
            self._release_acceptance_bundle_lease()
            return self._cleanup_state or (False, False)
        self._closed = True
        with contextlib.suppress(Exception):
            self._connection.close()
        try:
            job_terminal = self._job.terminate_and_wait(_STARTUP_PROCESS_CLEANUP_SECONDS)
        except Exception:
            job_terminal = False
        try:
            helper_terminal = _terminate_startup_helper(
                self._process, _STARTUP_HELPER_TERMINATE_GRACE_SECONDS
            )
            if helper_terminal and not job_terminal:
                with contextlib.suppress(Exception):
                    job_terminal = self._job.wait_until_empty(_STARTUP_PROCESS_CLEANUP_SECONDS)
            self._cleanup_state = (helper_terminal, job_terminal)
        finally:
            self._job.close()
            self._release_acceptance_bundle_lease()
        return self._cleanup_state


class _ComRemoteMember:
    __slots__ = ("_member", "_object_id", "_owner", "_scope")

    def __init__(
        self,
        owner: _ComProcessOwner,
        object_id: int,
        scope: str,
        member: str,
    ) -> None:
        self._owner = owner
        self._object_id = object_id
        self._scope = scope
        self._member = member

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._owner.request(
            {
                "op": "invoke",
                "object_id": self._object_id,
                "name": self._member,
                "args": [_encode_remote_argument(arg, self._owner) for arg in args],
                "kwargs": {
                    key: _encode_remote_argument(value, self._owner)
                    for key, value in kwargs.items()
                },
            }
        )


class _AcceptanceNetloadMember:
    __slots__ = ("_object_id", "_owner")

    def __init__(self, owner: _ComProcessOwner, object_id: int) -> None:
        self._owner = owner
        self._object_id = object_id

    def __call__(self, command: str) -> Any:
        return self._owner.acceptance_netload(self._object_id, command)


class _ComRemoteIterator:
    def __init__(self, owner: _ComProcessOwner, iterator_id: int) -> None:
        self._owner = owner
        self._iterator_id = iterator_id
        self._closed = False
        self._last_value: Any = None

    def __enter__(self) -> _ComRemoteIterator:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def __iter__(self) -> _ComRemoteIterator:
        return self

    def __next__(self) -> Any:
        if self._closed:
            raise StopIteration
        _release_remote(self._last_value)
        self._last_value = None
        value = self._owner.request({"op": "next", "object_id": self._iterator_id})
        if isinstance(value, dict) and value == {"$stop_iteration": True}:
            self._closed = True
            raise StopIteration
        self._last_value = value
        return value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _release_remote(self._last_value)
        self._last_value = None
        with contextlib.suppress(ComCallFailedError):
            self._owner.request({"op": "release", "object_id": self._iterator_id})


class _ComRemoteObject:
    __slots__ = ("_object_id", "_owner", "_released", "_scope")

    def __init__(self, owner: _ComProcessOwner, object_id: int, scope: str) -> None:
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_object_id", object_id)
        object.__setattr__(self, "_scope", scope)
        object.__setattr__(self, "_released", False)

    def __enter__(self) -> _ComRemoteObject:
        self._assert_live()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()

    def _assert_live(self) -> None:
        if self._released:
            raise ComCallFailedError(
                "Isolated COM object has been released",
                details={"reason": "isolated_com_object_released"},
            )

    def release(self) -> None:
        self._assert_live()
        self._owner.request({"op": "release", "object_id": self._object_id})
        object.__setattr__(self, "_released", True)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        self._assert_live()
        if self._scope == _SCOPE_DOCUMENT and name == _ACCEPTANCE_NETLOAD_MEMBER:
            return _AcceptanceNetloadMember(self._owner, self._object_id)
        if name in _RPC_METHOD_MEMBERS.get(self._scope, frozenset()):
            return _ComRemoteMember(self._owner, self._object_id, self._scope, name)
        if name not in _RPC_GET_MEMBERS.get(self._scope, frozenset()):
            raise ComCallFailedError(
                "Isolated COM member is outside the adapter capability",
                details={"reason": "isolated_com_member_not_allowed", "member": name},
            )
        return self._owner.request({"op": "getattr", "object_id": self._object_id, "name": name})

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._assert_live()
        if name not in _RPC_SET_MEMBERS.get(self._scope, frozenset()):
            raise ComCallFailedError(
                "Isolated COM member is outside the adapter capability",
                details={"reason": "isolated_com_member_not_allowed", "member": name},
            )
        self._owner.request(
            {
                "op": "setattr",
                "object_id": self._object_id,
                "name": name,
                "value": _encode_remote_argument(value, self._owner),
            }
        )

    def __iter__(self) -> _ComRemoteIterator:
        self._assert_live()
        if self._scope not in _RPC_ITER_ITEM_SCOPES:
            raise ComCallFailedError(
                "Isolated COM object is not iterable in this adapter capability",
                details={"reason": "isolated_com_member_not_allowed"},
            )
        iterator = self._owner.request({"op": "iter", "object_id": self._object_id})
        if not isinstance(iterator, _ComRemoteObject):
            raise ComCallFailedError(
                "Isolated COM helper returned an invalid iterator",
                details={"reason": "isolated_com_invalid_response"},
            )
        return _ComRemoteIterator(self._owner, iterator._object_id)


def _release_remote(value: Any) -> None:
    if isinstance(value, _ComRemoteObject) and not value._released:
        with contextlib.suppress(ComCallFailedError):
            value.release()


@contextlib.contextmanager
def _scoped_remote(value: Any) -> Iterator[Any]:
    try:
        yield value
    finally:
        _release_remote(value)


@contextlib.contextmanager
def _scoped_iterator(value: Any) -> Iterator[Iterator[Any]]:
    """Close RPC iterators without requiring local COM iterators to be context managers."""
    iterator = iter(value)
    try:
        yield iterator
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()


def _startup_message(payload: dict[str, Any]) -> bytes:
    _validate_rpc_tree(payload)
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(encoded) > _STARTUP_IPC_MAX_BYTES:
        raise ValueError("AutoCAD startup IPC message is too large")
    return encoded


def _send_startup_progress(connection: Connection, failure_stage: str) -> None:
    if failure_stage not in _STARTUP_FAILURE_STAGES:
        raise ValueError("Invalid isolated startup progress stage")
    connection.send_bytes(
        _startup_message({"stage": "startup_progress", "failure_stage": failure_stage})
    )


def _read_startup_message(connection: Any) -> dict[str, Any]:
    try:
        raw = connection.recv_bytes(_STARTUP_IPC_MAX_BYTES)
        if len(raw) > _RPC_MAX_BYTES_VALUE:
            raise ValueError("AutoCAD RPC byte message is too large")
        payload = json.loads(raw.decode("ascii"))
        _validate_rpc_tree(payload)
    except (EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ComCallFailedError(
            "Isolated AutoCAD startup helper returned no valid response",
            details={"reason": "isolated_startup_invalid_response"},
        ) from exc
    if not isinstance(payload, dict):
        raise ComCallFailedError(
            "Isolated AutoCAD startup helper returned no valid response",
            details={"reason": "isolated_startup_invalid_response"},
        )
    return cast(dict[str, Any], payload)


@dataclass(slots=True)
class _WorkerObjectEntry:
    value: Any
    scope: str


class _WorkerObjectRegistry:
    """Bounded monotonic registry; released IDispatch references are never resurrected."""

    def __init__(self, app: Any, *, object_cap: int = _RPC_OBJECT_CAP) -> None:
        if object_cap < 1:
            raise ValueError("AutoCAD RPC object cap must include the application root")
        self._object_cap = object_cap
        self._entries: dict[int, _WorkerObjectEntry] = {
            1: _WorkerObjectEntry(app, _SCOPE_APPLICATION)
        }
        self._next_object_id = 2

    @property
    def size(self) -> int:
        return len(self._entries)

    def get(self, object_id: int) -> _WorkerObjectEntry:
        try:
            return self._entries[object_id]
        except KeyError as exc:
            raise ValueError("Unknown isolated COM object reference") from exc

    def register(self, value: Any, scope: str) -> dict[str, Any]:
        if not _is_known_remote_scope(scope):
            raise ValueError("Unknown isolated COM object scope")
        if len(self._entries) >= self._object_cap:
            raise ValueError("Isolated COM object registry cap reached")
        object_id = self._next_object_id
        self._next_object_id += 1
        self._entries[object_id] = _WorkerObjectEntry(value, scope)
        return {"$remote_object": object_id, "scope": scope}

    def release(self, object_id: int) -> None:
        if object_id == 1:
            raise ValueError("The isolated COM application root cannot be released early")
        self._entries.pop(object_id, None)

    def clear(self) -> None:
        for object_id in sorted(self._entries, reverse=True):
            self._entries.pop(object_id, None)


def _worker_encode_value(
    value: Any,
    registry: _WorkerObjectRegistry,
    *,
    remote_scope: str | None = None,
    _depth: int = 0,
    _item_count: list[int] | None = None,
) -> Any:
    item_count = _item_count if _item_count is not None else [0]
    if _depth > _RPC_MAX_DEPTH:
        raise ValueError("AutoCAD RPC result tree is too deep")
    item_count[0] += 1
    if item_count[0] > _RPC_MAX_ITEMS:
        raise ValueError("AutoCAD RPC result tree has too many items")
    if remote_scope is not None:
        if value is None:
            raise ValueError("AutoCAD RPC object result is unexpectedly empty")
        return registry.register(value, remote_scope)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > _RPC_MAX_STRING_BYTES:
            raise ValueError("AutoCAD RPC result string is too large")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite COM result is not valid IPC data")
        return value
    if isinstance(value, list | tuple):
        return [
            _worker_encode_value(
                item,
                registry,
                _depth=_depth + 1,
                _item_count=item_count,
            )
            for item in value
        ]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {
            key: _worker_encode_value(
                item,
                registry,
                _depth=_depth + 1,
                _item_count=item_count,
            )
            for key, item in value.items()
        }
    raise ValueError("COM returned an object without an allowed capability scope")


def _worker_decode_argument(value: Any, registry: _WorkerObjectRegistry) -> Any:
    if isinstance(value, dict) and set(value) == {"$remote_object"}:
        object_id = value["$remote_object"]
        if isinstance(object_id, bool) or not isinstance(object_id, int):
            raise ValueError("Unknown isolated COM object reference")
        return registry.get(object_id).value
    if isinstance(value, dict) and set(value) == {"$variant", "values"}:
        import pythoncom  # noqa: TID251 - COM is confined to this module

        kind = value["$variant"]
        values = [_worker_decode_argument(item, registry) for item in value["values"]]
        if kind == "doubles":
            return pythoncom.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, values)
        if kind == "objects":
            return pythoncom.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, values)
        raise ValueError("Unknown isolated COM VARIANT kind")
    if isinstance(value, list):
        return [_worker_decode_argument(item, registry) for item in value]
    if isinstance(value, dict):
        return {key: _worker_decode_argument(item, registry) for key, item in value.items()}
    return value


def _validate_worker_invocation(scope: str, name: Any, args: Any, kwargs: Any) -> None:
    if not isinstance(name, str) or name not in _RPC_METHOD_MEMBERS.get(scope, frozenset()):
        raise ValueError("Invalid isolated COM member invocation")
    if not isinstance(args, list) or not isinstance(kwargs, dict) or kwargs:
        raise ValueError("Invalid isolated COM invocation arguments")
    if len(args) != _RPC_METHOD_ARITY[(scope, name)]:
        raise ValueError("Invalid isolated COM invocation arity")
    if (scope, name) == (_SCOPE_DOCUMENT, "GetVariable") and args != ["INSUNITS"]:
        raise ValueError("Only the INSUNITS query is allowed")
    if (scope, name) == (_SCOPE_DOCUMENT, "SetVariable") and args != ["FILEDIA", 0]:
        raise ValueError("Only acceptance FILEDIA suppression is allowed")
    if (scope, name) == (_SCOPE_SELECTION, "Select") and args != [5]:
        raise ValueError("Only the acceptance select-all mode is allowed")
    if (scope, name) == (_SCOPE_DOCUMENT, "HandleToObject") and (
        not isinstance(args[0], str) or re.fullmatch(r"[0-9A-Fa-f]+", args[0]) is None
    ):
        raise ValueError("Invalid AutoCAD handle")
    if (scope, name) == (_SCOPE_DOCUMENTS, "Item") and (
        isinstance(args[0], bool) or not isinstance(args[0], int) or args[0] < 0
    ):
        raise ValueError("Invalid AutoCAD document index")
    if (scope, name) == (_SCOPE_DOCUMENTS, "Open"):
        raw_path, read_only = args
        path = PureWindowsPath(raw_path) if isinstance(raw_path, str) else PureWindowsPath()
        if (
            not isinstance(raw_path, str)
            or not path.is_absolute()
            or raw_path.startswith(("\\\\", "//"))
            or path.suffix.casefold() not in {".dwg", ".dxf"}
            or not isinstance(read_only, bool)
        ):
            raise ValueError("Invalid owned AutoCAD document open request")
    if (scope, name) in {
        (_SCOPE_DOCUMENT, "SaveAs"),
        (_SCOPE_PLOT, "PlotToFile"),
    } and (not isinstance(args[0], str) or not PureWindowsPath(args[0]).is_absolute()):
        raise ValueError("AutoCAD export requires an absolute local path")


def _serve_isolated_com_apartment(
    connection: Connection,
    app: Any,
    *,
    owned_autocad_pid: int | None = None,
    netload_timeout_seconds: float = 30.0,
) -> None:
    registry = _WorkerObjectRegistry(app)
    try:
        while True:
            try:
                request = _read_startup_message(connection)
            except ComCallFailedError:
                return
            operation = request.get("op")
            try:
                if operation == "shutdown" and set(request) == {"op"}:
                    return
                object_id = request.get("object_id")
                if isinstance(object_id, bool) or not isinstance(object_id, int):
                    raise ValueError("Unknown isolated COM object reference")
                if operation == "release" and set(request) == {"op", "object_id"}:
                    registry.release(object_id)
                    encoded_result: Any = None
                else:
                    entry = registry.get(object_id)
                    target = entry.value
                    scope = entry.scope
                    result_scope: str | None = None
                    if operation == "getattr" and set(request) == {
                        "op",
                        "object_id",
                        "name",
                    }:
                        name = request["name"]
                        if not isinstance(name, str) or name not in _RPC_GET_MEMBERS.get(
                            scope, frozenset()
                        ):
                            raise ValueError("Invalid isolated COM member")
                        result = getattr(target, name)
                        result_scope = _RPC_CHILD_SCOPES.get((scope, name))
                    elif operation == "setattr" and set(request) == {
                        "op",
                        "object_id",
                        "name",
                        "value",
                    }:
                        name = request["name"]
                        if not isinstance(name, str) or name not in _RPC_SET_MEMBERS.get(
                            scope, frozenset()
                        ):
                            raise ValueError("Invalid isolated COM member")
                        setattr(
                            target,
                            name,
                            _worker_decode_argument(request["value"], registry),
                        )
                        result = None
                    elif operation == "invoke" and set(request) == {
                        "op",
                        "object_id",
                        "name",
                        "args",
                        "kwargs",
                    }:
                        args = _worker_decode_argument(request["args"], registry)
                        kwargs = _worker_decode_argument(request["kwargs"], registry)
                        name = request["name"]
                        _validate_worker_invocation(scope, name, args, kwargs)
                        result = getattr(target, name)(*args, **kwargs)
                        result_scope = _RPC_CHILD_SCOPES.get((scope, name))
                    elif operation == "acceptance_netload" and set(request) == {
                        "op",
                        "object_id",
                    }:
                        if scope != _SCOPE_DOCUMENT:
                            raise ValueError("NETLOAD requires the owned acceptance document")
                        if owned_autocad_pid is None:
                            raise ValueError("NETLOAD requires an exact owned AutoCAD PID")
                        _assert_acceptance_bridge_absent_before_netload()
                        fixed_command = _expected_acceptance_netload_command()
                        getattr(target, _ACCEPTANCE_NETLOAD_MEMBER)(fixed_command)
                        result = _wait_for_owned_acceptance_bridge(
                            owned_autocad_pid,
                            netload_timeout_seconds,
                        )
                    elif operation == "iter" and set(request) == {"op", "object_id"}:
                        item_scope = _RPC_ITER_ITEM_SCOPES.get(scope)
                        if item_scope is None:
                            raise ValueError("Invalid isolated COM iteration")
                        result = iter(target)
                        result_scope = f"{_SCOPE_ITERATOR_PREFIX}{item_scope}"
                    elif operation == "next" and set(request) == {"op", "object_id"}:
                        if not scope.startswith(_SCOPE_ITERATOR_PREFIX):
                            raise ValueError("Invalid isolated COM iterator")
                        try:
                            result = next(target)
                        except StopIteration:
                            registry.release(object_id)
                            connection.send_bytes(
                                _startup_message(
                                    {
                                        "stage": "result",
                                        "value": {"$stop_iteration": True},
                                    }
                                )
                            )
                            continue
                        result_scope = scope.removeprefix(_SCOPE_ITERATOR_PREFIX)
                    else:
                        raise ValueError("Invalid isolated COM operation")
                    encoded_result = _worker_encode_value(
                        result,
                        registry,
                        remote_scope=result_scope,
                    )
                connection.send_bytes(
                    _startup_message({"stage": "result", "value": encoded_result})
                )
            except Exception:
                with contextlib.suppress(Exception):
                    connection.send_bytes(_startup_message({"stage": "rpc_error"}))
    finally:
        registry.clear()


def _isolated_autocad_startup_worker(
    connection: Connection,
    versioned_prog_id: str,
    job_name: str,
    timeout_seconds: float,
) -> None:
    """Launch AutoCAD in the parent-owned job and marshal only its IDispatch proxy."""
    import ctypes

    job_handle: int | None = None
    pythoncom_initialized = False
    process_pid: int | None = None
    process_handle: int | None = None
    assigned_to_job = False
    app: Any = None
    failure_stage = "helper_initialization"
    try:
        import pythoncom  # noqa: TID251 - COM is confined to this module

        failure_stage = "parent_job_handshake"
        connection.send_bytes(_startup_message({"stage": "helper_ready"}))
        if not connection.poll(timeout_seconds):
            raise TimeoutError("Parent did not arm the isolated AutoCAD startup job")
        if _read_startup_message(connection) != {"stage": "job_assigned"}:
            raise ValueError("Parent returned an invalid startup-job response")
        failure_stage = "com_initialization"
        pythoncom.CoInitialize()
        pythoncom_initialized = True
        failure_stage = "job_open"
        job_handle = _open_startup_job(job_name)
        failure_stage = "binary_trust"
        _send_startup_progress(connection, failure_stage)
        executable = _resolve_local_server_executable(versioned_prog_id)
        failure_stage = "process_create"
        _send_startup_progress(connection, failure_stage)
        process_pid, process_handle = _create_autocad_process(executable)
        failure_stage = "process_identity"
        _send_startup_progress(connection, failure_stage)
        created_image_path, created_creation_time_100ns = ComAutoCADAdapter._process_identity(
            process_pid
        )
        if not _same_windows_path(created_image_path, executable):
            raise OSError("The created AutoCAD process image is not the trusted executable")
        failure_stage = "job_assignment"
        _send_startup_progress(connection, failure_stage)
        if not _process_is_in_job(process_pid, job_handle):
            _assign_process_to_job(job_handle, process_pid)
        assigned_to_job = True
        ctypes.windll.kernel32.CloseHandle(process_handle)
        process_handle = None
        failure_stage = "rot_discovery"
        _send_startup_progress(connection, failure_stage)
        app, rot_hwnd = _find_job_owned_application(
            job_handle,
            timeout_seconds,
            expected_pid=process_pid,
            expected_executable=executable,
            versioned_prog_id=versioned_prog_id,
        )
        failure_stage = "dispatch_marshal"
        _send_startup_progress(connection, failure_stage)
        marshaled = _marshal_dispatch(app)
        app = None
        app = _unmarshal_dispatch(marshaled)
        failure_stage = "identity_verification"
        _send_startup_progress(connection, failure_stage)
        hwnd = int(app.HWND)
        if hwnd != rot_hwnd:
            raise OSError("Marshaled AutoCAD application identity changed")
        pid = ComAutoCADAdapter._pid_from_hwnd(hwnd)
        image_path, creation_time_100ns = ComAutoCADAdapter._process_identity(pid)
        if (
            pid != process_pid
            or not _same_windows_path(image_path, executable)
            or (image_path, creation_time_100ns)
            != (created_image_path, created_creation_time_100ns)
            or not _application_version_matches_prog_id(str(app.Version), versioned_prog_id)
        ):
            raise OSError("The marshaled AutoCAD application identity changed")
        failure_stage = "parent_adoption"
        connection.send_bytes(
            _startup_message(
                {
                    "stage": "ready",
                    "marshal_size": len(marshaled),
                    "app_object_id": 1,
                    "session": {
                        "prog_id": versioned_prog_id,
                        "hwnd": hwnd,
                        "pid": process_pid,
                        "image_path": created_image_path,
                        "creation_time_100ns": created_creation_time_100ns,
                    },
                }
            )
        )
        if not connection.poll(timeout_seconds):
            raise TimeoutError("Parent did not adopt the isolated AutoCAD application")
        if _read_startup_message(connection) != {"stage": "adopted"}:
            raise ValueError("Parent returned an invalid AutoCAD adoption response")
        connection.send_bytes(_startup_message({"stage": "owner_ready"}))
        failure_stage = "rpc_service"
        _serve_isolated_com_apartment(
            connection,
            app,
            owned_autocad_pid=process_pid,
            netload_timeout_seconds=timeout_seconds,
        )
        app = None
        if job_handle is not None:
            ctypes.windll.kernel32.CloseHandle(job_handle)
            job_handle = None
        if pythoncom_initialized:
            pythoncom.CoUninitialize()
            pythoncom_initialized = False
        connection.send_bytes(_startup_message({"stage": "stopped"}))
    except Exception:
        if process_handle is not None and not assigned_to_job:
            with contextlib.suppress(Exception):
                _terminate_process_handle(process_handle)
            process_handle = None
        with contextlib.suppress(Exception):
            connection.send_bytes(
                _startup_message({"stage": "error", "failure_stage": failure_stage})
            )
    finally:
        app = None
        if job_handle is not None:
            ctypes.windll.kernel32.CloseHandle(job_handle)
        connection.close()
        if pythoncom_initialized:
            with contextlib.suppress(Exception):
                pythoncom.CoUninitialize()


def _terminate_startup_helper(process: _StartupProcess, timeout_seconds: float) -> bool:
    try:
        alive = process.is_alive()
    except Exception:
        return False
    if not alive:
        try:
            process.join(0.0)
        except Exception:
            return False
        return True
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    with contextlib.suppress(Exception):
        process.terminate()
    with contextlib.suppress(Exception):
        process.join(min(_STARTUP_HELPER_TERMINATE_GRACE_SECONDS, max(0.0, timeout_seconds)))
    try:
        alive = process.is_alive()
    except Exception:
        alive = True
    if alive:
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            process.join(max(0.0, deadline - time.monotonic()))
    try:
        return not process.is_alive()
    except Exception:
        return False


def _owned_session_from_payload(payload: Any) -> OwnedComSession:
    if not isinstance(payload, dict) or set(payload) != {
        "prog_id",
        "hwnd",
        "pid",
        "image_path",
        "creation_time_100ns",
    }:
        raise ComCallFailedError(
            "Isolated AutoCAD startup helper returned invalid ownership metadata",
            details={"reason": "isolated_startup_invalid_response"},
        )
    if (
        not isinstance(payload["prog_id"], str)
        or isinstance(payload["hwnd"], bool)
        or not isinstance(payload["hwnd"], int)
        or isinstance(payload["pid"], bool)
        or not isinstance(payload["pid"], int)
        or not isinstance(payload["image_path"], str)
        or isinstance(payload["creation_time_100ns"], bool)
        or not isinstance(payload["creation_time_100ns"], int)
    ):
        raise ComCallFailedError(
            "Isolated AutoCAD startup helper returned invalid ownership metadata",
            details={"reason": "isolated_startup_invalid_response"},
        )
    return OwnedComSession(**payload)


def _run_isolated_startup_boundary(
    *,
    versioned_prog_id: str,
    timeout_seconds: float,
    preexisting_pids: set[int],
    dispatch_started_100ns: int,
    current_pids: Callable[[], set[int]],
    pid_from_hwnd: Callable[[int], int],
    process_identity: Callable[[int], tuple[str, int]],
    worker_target: Callable[[Connection, str, str, float], None] = (
        _isolated_autocad_startup_worker
    ),
    job_factory: Callable[[], _StartupJob] = _WindowsStartupJob.create,
) -> tuple[Any, OwnedComSession, _ComProcessOwner]:
    """Return a process-owned COM proxy or terminally clean its exact startup job."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("startup timeout must be positive")
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=True)
    job = job_factory()
    process = cast(
        _StartupProcess,
        context.Process(
            target=worker_target,
            args=(child_connection, versioned_prog_id, job.name, timeout_seconds),
            name="cad-harness-autocad-startup",
            daemon=False,
        ),
    )
    terminal_deadline = time.monotonic() + timeout_seconds
    cleanup_reserve = min(
        _STARTUP_HELPER_TERMINATE_GRACE_SECONDS,
        max(0.01, timeout_seconds * 0.25),
    )
    startup_deadline = terminal_deadline - cleanup_reserve
    app: Any = None
    remote_owner: _ComProcessOwner | None = None
    handed_off = False
    original_failure: BaseException | None = None
    try:
        try:
            process.start()
            child_connection.close()
            if not parent_connection.poll(max(0.0, startup_deadline - time.monotonic())):
                raise ComCallFailedError(
                    "Isolated AutoCAD startup exceeded its configured timeout",
                    details={
                        "reason": "isolated_startup_timeout",
                        "timeout_seconds": timeout_seconds,
                    },
                )
            helper_envelope = _read_startup_message(parent_connection)
            if helper_envelope != {"stage": "helper_ready"} or process.pid is None:
                raise ComCallFailedError(
                    "Isolated AutoCAD startup helper failed before job assignment",
                    details={"reason": "isolated_startup_failed"},
                )
            job.assign_pid(process.pid)
            parent_connection.send_bytes(_startup_message({"stage": "job_assigned"}))
            last_failure_stage: str | None = None
            while True:
                if not parent_connection.poll(max(0.0, startup_deadline - time.monotonic())):
                    details: dict[str, Any] = {
                        "reason": "isolated_startup_timeout",
                        "timeout_seconds": timeout_seconds,
                    }
                    if last_failure_stage is not None:
                        details["failure_stage"] = last_failure_stage
                    raise ComCallFailedError(
                        "Isolated AutoCAD startup exceeded its configured timeout",
                        details=details,
                    )
                envelope = _read_startup_message(parent_connection)
                if envelope.get("stage") != "startup_progress":
                    break
                progress_stage = envelope.get("failure_stage")
                if (
                    set(envelope) != {"stage", "failure_stage"}
                    or not isinstance(progress_stage, str)
                    or progress_stage not in _STARTUP_FAILURE_STAGES
                ):
                    raise ComCallFailedError(
                        "Isolated AutoCAD startup helper returned invalid progress",
                        details={"reason": "isolated_startup_invalid_response"},
                    )
                last_failure_stage = progress_stage
            if envelope.get("stage") == "error":
                failure_stage = envelope.get("failure_stage")
                if (
                    set(envelope) == {"stage", "failure_stage"}
                    and isinstance(failure_stage, str)
                    and failure_stage in _STARTUP_FAILURE_STAGES
                ):
                    raise ComCallFailedError(
                        "Isolated AutoCAD startup helper failed",
                        details={
                            "reason": "isolated_startup_failed",
                            "failure_stage": failure_stage,
                        },
                    )
            if envelope.get("stage") != "ready" or set(envelope) != {
                "stage",
                "marshal_size",
                "app_object_id",
                "session",
            }:
                raise ComCallFailedError(
                    "Isolated AutoCAD startup helper failed",
                    details={"reason": "isolated_startup_failed"},
                )
            session = _owned_session_from_payload(envelope["session"])
            marshal_size = envelope["marshal_size"]
            app_object_id = envelope["app_object_id"]
            if (
                isinstance(marshal_size, bool)
                or not isinstance(marshal_size, int)
                or not 0 < marshal_size <= _STARTUP_IPC_MAX_BYTES
                or isinstance(app_object_id, bool)
                or not isinstance(app_object_id, int)
                or app_object_id <= 0
            ):
                raise ComCallFailedError(
                    "Isolated AutoCAD startup helper returned invalid COM data",
                    details={"reason": "isolated_startup_invalid_response"},
                )
            # COM's service-control activation can return a new AutoCAD process
            # rather than the manually launched local-server PID.  Before the
            # helper is allowed to serve any application call, independently bind
            # that exact HWND/PID/creation identity to the parent-owned kill job.
            # A pre-existing application is rejected before any job mutation.
            session_pid = pid_from_hwnd(session.hwnd)
            session_identity = process_identity(session_pid)
            if (
                session.prog_id != versioned_prog_id
                or session_pid != session.pid
                or session_pid in preexisting_pids
                or session_pid not in current_pids()
                or session_identity != (session.image_path, session.creation_time_100ns)
                or Path(session.image_path).name.casefold() != "acad.exe"
                or session.creation_time_100ns < dispatch_started_100ns
            ):
                raise ComCallFailedError(
                    "Could not prove ownership of the isolated AutoCAD process",
                    details={"reason": "isolated_process_ownership_unproven"},
                )
            if not job.contains_pid(session_pid):
                job.assign_pid(session_pid)
            if not job.contains_pid(session_pid):
                raise ComCallFailedError(
                    "Could not bind the isolated AutoCAD process to terminal cleanup",
                    details={"reason": "isolated_process_ownership_unproven"},
                )
            parent_connection.send_bytes(_startup_message({"stage": "adopted"}))
            if not parent_connection.poll(max(0.0, startup_deadline - time.monotonic())):
                raise ComCallFailedError(
                    "Isolated AutoCAD owner did not become ready before the deadline",
                    details={"reason": "isolated_startup_timeout"},
                )
            if _read_startup_message(parent_connection) != {"stage": "owner_ready"}:
                raise ComCallFailedError(
                    "Isolated AutoCAD owner returned an invalid response",
                    details={"reason": "isolated_startup_invalid_response"},
                )
            if not process.is_alive() or process.exitcode is not None:
                raise ComCallFailedError(
                    "Isolated AutoCAD COM owner terminated during startup",
                    details={"reason": "isolated_startup_helper_not_terminal"},
                )
            remaining = max(0.001, startup_deadline - time.monotonic())
            remote_owner = _ComProcessOwner(
                parent_connection,
                process,
                job,
                call_timeout_seconds=remaining,
                expected_autocad_pid=session.pid,
            )
            app = _ComRemoteObject(remote_owner, app_object_id, _SCOPE_APPLICATION)
            # The helper already read and verified the COM HWND before publishing
            # this session.  Re-reading it through RPC here can block while AutoCAD
            # finishes initialization and adds no ownership evidence: the parent can
            # independently map the exact handshake HWND to a live OS process.
            hwnd = session.hwnd
            pid = pid_from_hwnd(hwnd)
            identity = process_identity(pid)
            if (
                session.prog_id != versioned_prog_id
                or hwnd != session.hwnd
                or pid != session.pid
                or pid in preexisting_pids
                or pid not in current_pids()
                or identity != (session.image_path, session.creation_time_100ns)
                or not job.contains_pid(pid)
                or Path(session.image_path).name.casefold() != "acad.exe"
                or session.creation_time_100ns < dispatch_started_100ns
            ):
                raise ComCallFailedError(
                    "Could not prove ownership of the isolated AutoCAD process",
                    details={"reason": "isolated_process_ownership_unproven"},
                )
            remote_owner._call_timeout_seconds = timeout_seconds
            handed_off = True
            return app, session, remote_owner
        except BaseException as exc:
            original_failure = exc
            raise
    except BaseException as exc:
        original_failure = exc
        raise
    finally:
        try:
            with contextlib.suppress(Exception):
                child_connection.close()
            if not handed_off:
                if remote_owner is not None:
                    helper_terminal, cleanup_confirmed = remote_owner._terminal_close()
                else:
                    remaining = max(0.0, terminal_deadline - time.monotonic())
                    cleanup_confirmed = False
                    try:
                        cleanup_confirmed = job.terminate_and_wait(
                            min(_STARTUP_PROCESS_CLEANUP_SECONDS, remaining)
                        )
                    except Exception:
                        cleanup_confirmed = False
                    helper_terminal = _terminate_startup_helper(
                        process, max(0.0, terminal_deadline - time.monotonic())
                    )
                    with contextlib.suppress(Exception):
                        parent_connection.close()
                app = None
                if not helper_terminal or not cleanup_confirmed:
                    raise ComCallFailedError(
                        "Isolated AutoCAD startup cleanup could not be confirmed",
                        details={
                            "reason": "isolated_startup_cleanup_unconfirmed",
                            "helper_terminal": helper_terminal,
                            "job_terminal": cleanup_confirmed,
                        },
                    ) from original_failure
        finally:
            if not handed_off:
                job.close()


class ComAutoCADAdapter(BaseAdapter):
    """Thin ActiveX mapping. Contains no engineering decisions."""

    adapter_type = "com"
    supported_operations = frozenset(OperationType)
    capabilities = frozenset(
        {
            AdapterCapability.INSPECT_DOCUMENT,
            AdapterCapability.INSPECT_SELECTION,
            AdapterCapability.COMMIT,
            AdapterCapability.EXPORT,
            # Deliberately absent: ATOMIC_TRANSACTION, DOCUMENT_LOCK, STABLE_METADATA,
            # IN_VIEWPORT_PREVIEW. Those arrive with the C# bridge.
        }
    )

    OPERATION_DISPATCH: ClassVar[dict[OperationType, str]] = {
        OperationType.CREATE_LINE: "_create_line",
        OperationType.CREATE_POLYLINE: "_create_polyline",
        OperationType.CREATE_CLOSED_POLYLINE: "_create_closed_polyline",
        OperationType.CREATE_CIRCLE: "_create_circle",
        OperationType.CREATE_CIRCLES: "_create_circles",
        OperationType.CREATE_ARC: "_create_arc",
        OperationType.CREATE_TEXT: "_create_text",
        OperationType.CREATE_CENTERLINE: "_create_centerline",
        OperationType.CREATE_CENTERMARK: "_create_centermark",
        OperationType.CREATE_LINEAR_DIMENSION: "_create_linear_dimension",
        OperationType.CREATE_ALIGNED_DIMENSION: "_create_aligned_dimension",
        OperationType.CREATE_ANGULAR_DIMENSION: "_create_angular_dimension",
        OperationType.CREATE_DIAMETER_DIMENSION: "_create_diameter_dimension",
        OperationType.CREATE_RADIUS_DIMENSION: "_create_radius_dimension",
        OperationType.CREATE_HATCH: "_create_hatch",
        OperationType.UPDATE_ENTITY: "_update_entity",
        OperationType.DELETE_ENTITY: "_delete_entity",
    }

    def __init__(
        self,
        prog_id_key: str = "autocad",
        *,
        startup_wait_seconds: float = 20.0,
        job_store: JobStore | None = None,
        write_enabled: bool = True,
    ) -> None:
        self.prog_id = PROG_IDS.get(prog_id_key.lower(), PROG_IDS["autocad"])
        self.startup_wait_seconds = startup_wait_seconds
        self.job_store = job_store
        self._app: Any = None
        self._document: Any = None
        self._owned_session: OwnedComSession | None = None
        self._remote_com_owner: _ComProcessOwner | None = None
        self._com_owner_thread_id: int | None = None
        if not write_enabled:
            self.capabilities = self.capabilities - {AdapterCapability.COMMIT}

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def connect(self, *, launch_if_missing: bool = False) -> None:
        """Attach to a running instance, optionally launching one.

        Launching is opt-in: silently starting AutoCAD from a background MCP server
        surprises users and can hijack their session.
        """
        import pythoncom  # noqa: TID251 - COM is confined to this module
        import win32com.client  # noqa: TID251

        if self._app is not None or self._com_owner_thread_id is not None:
            raise ComCallFailedError(
                "Adapter is already connected",
                details={"reason": "connection_already_exists"},
            )
        try:
            pythoncom.CoInitialize()
            self._com_owner_thread_id = threading.get_ident()
            try:
                self._app = win32com.client.GetActiveObject(self.prog_id)
            except Exception as exc:
                if not launch_if_missing:
                    raise AutoCADNotRunningError(
                        "No running AutoCAD instance found",
                        required_action="Start AutoCAD, open the target drawing, then retry",
                        details={"prog_id": self.prog_id},
                    ) from exc
                try:
                    self._app = win32com.client.Dispatch(self.prog_id)
                    self._app.Visible = True
                    self._wait_until_quiescent(timeout_seconds=self.startup_wait_seconds)
                except Exception as launch_exc:  # pragma: no cover - environment specific
                    raise AutoCADNotRunningError(
                        "Could not start AutoCAD",
                        details={"prog_id": self.prog_id},
                    ) from launch_exc

            try:
                if self._app.Documents.Count == 0:
                    raise AutoCADNotRunningError(
                        "AutoCAD is running but has no open document",
                        required_action="Open the target drawing in AutoCAD, then retry",
                    )
                self._document = self._app.ActiveDocument
            except AutoCADNotRunningError:
                raise
            except Exception as exc:
                raise ComCallFailedError(
                    "Could not obtain the active AutoCAD document",
                    details={"prog_id": self.prog_id},
                ) from exc
        except Exception:
            self._app = None
            self._document = None
            if self._com_owner_thread_id == threading.get_ident():
                with contextlib.suppress(Exception):
                    pythoncom.CoUninitialize()
            self._com_owner_thread_id = None
            raise

    def connect_isolated(self, *, versioned_prog_id: str) -> OwnedComSession:
        """Create and prove ownership of a new, version-specific AutoCAD process.

        Startup runs in a killable helper and the registered ``acad.exe`` is assigned
        to a parent-owned Windows Job before any COM readiness call. ROT lookup,
        IDispatch marshal/unmarshal and HWND access remain inside that owned process;
        the parent receives a deadline-bound RPC proxy plus ownership metadata. A
        timeout terminates the helper and that exact job; pre-existing AutoCAD PIDs
        are never members of it and are never cleanup targets.
        """
        if _VERSIONED_AUTOCAD_PROG_ID.fullmatch(versioned_prog_id) is None:
            raise ValueError("versioned_prog_id must look like 'AutoCAD.Application.26'")
        if self._app is not None or self._com_owner_thread_id is not None:
            raise ComCallFailedError(
                "Adapter is already connected",
                details={"reason": "connection_already_exists"},
            )

        preexisting_pids = self._acad_process_ids()
        dispatch_started_100ns = self._system_filetime_100ns()
        app, session, remote_owner = _run_isolated_startup_boundary(
            versioned_prog_id=versioned_prog_id,
            timeout_seconds=self.startup_wait_seconds,
            preexisting_pids=preexisting_pids,
            dispatch_started_100ns=dispatch_started_100ns,
            current_pids=self._acad_process_ids,
            pid_from_hwnd=self._pid_from_hwnd,
            process_identity=self._process_identity,
        )
        self._app = app
        self._owned_session = session
        self._remote_com_owner = remote_owner
        self._com_owner_thread_id = threading.get_ident()
        return session

    @staticmethod
    def _acad_process_ids() -> set[int]:
        """Return PIDs whose exact Windows image name is ``acad.exe``."""
        if sys.platform != "win32":
            return set()
        import ctypes
        from ctypes import wintypes

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

        snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if snapshot == invalid_handle:
            raise OSError("Could not enumerate AutoCAD processes")
        result: set[int] = set()
        try:
            entry = ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(ProcessEntry32W)
            has_entry = bool(ctypes.windll.kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
            while has_entry:
                if str(entry.szExeFile).casefold() == "acad.exe":
                    result.add(int(entry.th32ProcessID))
                has_entry = bool(
                    ctypes.windll.kernel32.Process32NextW(snapshot, ctypes.byref(entry))
                )
            return result
        finally:
            ctypes.windll.kernel32.CloseHandle(snapshot)

    @staticmethod
    def _pid_from_hwnd(hwnd: int) -> int:
        """Map an AutoCAD top-level HWND to its owning process ID."""
        if sys.platform != "win32":
            raise OSError("HWND process mapping requires Windows")
        import ctypes
        from ctypes import wintypes

        pid = wintypes.DWORD()
        thread_id = ctypes.windll.user32.GetWindowThreadProcessId(
            wintypes.HWND(hwnd), ctypes.byref(pid)
        )
        if thread_id == 0 or pid.value == 0:
            raise OSError("Could not map the AutoCAD HWND to a process")
        return int(pid.value)

    @staticmethod
    def _system_filetime_100ns() -> int:
        """Return current Windows system time using the process-creation clock."""
        if sys.platform != "win32":
            raise OSError("Windows process identity requires Windows")
        import ctypes
        from ctypes import wintypes

        now = wintypes.FILETIME()
        ctypes.windll.kernel32.GetSystemTimeAsFileTime(ctypes.byref(now))
        return (int(now.dwHighDateTime) << 32) | int(now.dwLowDateTime)

    @staticmethod
    def _process_identity(pid: int) -> tuple[str, int]:
        """Return exact image path and creation FILETIME for PID-reuse resistance."""
        if sys.platform != "win32":
            raise OSError("Windows process identity requires Windows")
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, wintypes.DWORD(pid)
        )
        if not handle:
            raise OSError("Could not open the AutoCAD process for identity verification")
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
                raise OSError("Could not read the AutoCAD process creation time")
            capacity = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(capacity)
            ):
                raise OSError("Could not read the AutoCAD process image path")
            creation = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            return buffer.value, creation
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    @property
    def owned_session(self) -> OwnedComSession | None:
        """Return process identity only when isolated-session ownership was proven."""
        return self._owned_session

    def require_owned_application(self) -> Any:
        """Expose the raw COM application only after isolated ownership proof."""
        self._assert_com_owner_thread()
        if self._app is None or self._owned_session is None:
            raise ComCallFailedError(
                "No proven isolated AutoCAD session is connected",
                details={"reason": "owned_session_required"},
            )
        return self._app

    def _assert_com_owner_thread(self) -> None:
        if (
            self._com_owner_thread_id is not None
            and self._com_owner_thread_id != threading.get_ident()
        ):
            raise ComCallFailedError(
                "COM adapter was accessed from a non-owner thread",
                details={"reason": "com_apartment_ownership_mismatch"},
            )

    def _require_current_owned_application(self) -> Any:
        app = self.require_owned_application()
        session = self._owned_session
        assert session is not None
        try:
            hwnd = int(app.HWND)
            pid = self._pid_from_hwnd(hwnd)
            identity = self._process_identity(pid)
        except Exception as exc:
            raise ComCallFailedError(
                "Could not revalidate the isolated AutoCAD process",
                details={"reason": "owned_process_identity_unavailable"},
            ) from exc
        if (
            hwnd != session.hwnd
            or pid != session.pid
            or identity != (session.image_path, session.creation_time_100ns)
        ):
            raise ComCallFailedError(
                "The isolated AutoCAD process identity changed",
                details={"reason": "owned_process_identity_changed"},
            )
        return app

    def open_owned_document(self, path: Path, *, read_only: bool = True) -> str:
        """Open one absolute scratch DWG/DXF only after process ownership is revalidated."""
        candidate = path.resolve(strict=True)
        if not candidate.is_file() or candidate.suffix.casefold() not in {".dwg", ".dxf"}:
            raise ValueError("Owned COM sessions open one existing DWG or DXF file")
        app = self._require_current_owned_application()
        document: Any = None
        try:
            self._wait_until_quiescent(timeout_seconds=self.startup_wait_seconds)
            with _scoped_remote(app.Documents) as documents:
                opened_result = documents.Open(str(candidate), bool(read_only))
                _release_remote(opened_result)
            deadline = time.monotonic() + self.startup_wait_seconds
            while time.monotonic() < deadline:
                active: Any = None
                try:
                    active = app.ActiveDocument
                    opened = Path(str(active.FullName)).resolve(strict=True)
                    if opened == candidate:
                        document = active
                        active = None
                        break
                except Exception:
                    pass
                finally:
                    _release_remote(active)
                time.sleep(0.1)
            if document is None:
                raise ComCallFailedError(
                    "AutoCAD did not activate the requested scratch document",
                    details={"reason": "owned_document_open_timeout"},
                )
            _release_remote(self._document)
            self._document = document
            return self._document_id(document)
        except Exception:
            if document is not None:
                with contextlib.suppress(Exception):
                    document.Close(False)
                _release_remote(document)
            self._document = None
            raise

    def close_owned_session(self) -> None:
        """Close without saving only while the original PID/start identity still matches."""
        deadline = time.monotonic() + self.startup_wait_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                app = self._require_current_owned_application()
                self._wait_until_quiescent(timeout_seconds=min(2.0, self.startup_wait_seconds))
                with _scoped_remote(app.Documents) as documents:
                    for index in range(int(documents.Count) - 1, -1, -1):
                        with _scoped_remote(documents.Item(index)) as document:
                            document.Close(False)
                app.Quit()
                remote_owner = self._remote_com_owner
                if remote_owner is not None:
                    remote_owner.shutdown_after_verified_quit()
                    self._app = None
                    self._document = None
                    self._owned_session = None
                    self._remote_com_owner = None
                    self._com_owner_thread_id = None
                else:
                    self.disconnect()
                return
            except AutoCADBusyError as exc:
                last_error = exc
                time.sleep(0.2)
            except ComCallFailedError:
                # An isolated RPC failure terminally closes its helper and kill job.
                # Retrying through the now-closed owner only hides the original
                # failure for the full startup timeout.
                self.disconnect()
                raise
            except Exception as exc:  # Local COM can reject calls during startup.
                last_error = exc
                time.sleep(0.2)
        raise ComCallFailedError(
            "Owned AutoCAD scratch session did not close before the deadline",
            required_action="Close only the PID reported by the isolated acceptance evidence",
            details={"reason": "owned_session_close_timeout"},
        ) from last_error

    def disconnect(self) -> None:
        import pythoncom  # noqa: TID251

        self._assert_com_owner_thread()
        remote_owner = self._remote_com_owner
        self._app = None
        self._document = None
        self._owned_session = None
        self._remote_com_owner = None
        if remote_owner is not None:
            try:
                remote_owner.shutdown()
            finally:
                self._com_owner_thread_id = None
            return
        if self._com_owner_thread_id is not None:
            try:
                pythoncom.CoUninitialize()
            finally:
                self._com_owner_thread_id = None

    def _require_document(self) -> Any:
        self._assert_com_owner_thread()
        if self._document is None:
            raise AutoCADNotRunningError(
                "Adapter is not connected to a document",
                required_action="Call connect() before any document operation",
            )
        self._assert_quiescent()
        return self._document

    def _assert_quiescent(self) -> None:
        """Refuse to write while AutoCAD is mid-command."""
        try:
            with _scoped_remote(self._app.GetAcadState()) as state:
                quiescent = bool(state.IsQuiescent)
        except Exception:
            # Older builds may not expose GetAcadState; proceed and let the call fail.
            return
        if not quiescent:
            raise AutoCADBusyError(
                "AutoCAD is busy with another command",
                required_action="Finish the active AutoCAD command, then retry",
            )

    def _wait_until_quiescent(self, *, timeout_seconds: float) -> None:
        import time

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                with _scoped_remote(self._app.GetAcadState()) as state:
                    if bool(state.IsQuiescent):
                        return
            except Exception:
                pass
            time.sleep(0.5)

    # ------------------------------------------------------------------ #
    # COM marshalling helpers
    # ------------------------------------------------------------------ #

    def _variant_doubles(self, values: list[float]) -> Any:
        """Wrap a flat float list as a COM ``VT_ARRAY | VT_R8`` variant.

        ActiveX rejects plain Python lists for coordinate arguments, so every point
        must go through this.
        """
        if self._remote_com_owner is not None:
            return _RemoteVariant("doubles", tuple(float(value) for value in values))

        import pythoncom  # noqa: TID251
        import win32com.client  # noqa: TID251

        return win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values]
        )

    def _variant_point3d(self, x: float, y: float, z: float = 0.0) -> Any:
        return self._variant_doubles([x, y, z])

    @staticmethod
    def validate_lineweight(lineweight: int | None) -> int | None:
        """Return ``lineweight`` only if AutoCAD accepts it; otherwise ``None``.

        Unlike the reference implementation, an invalid value is not silently coerced
        to 0 - the caller is told the value was dropped.
        """
        if lineweight is None:
            return None
        return lineweight if lineweight in VALID_LINEWEIGHTS else None

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def status(self) -> AdapterStatus:
        self._assert_com_owner_thread()
        available = self._app is not None and self._document is not None
        version: str | None = None
        document_id: str | None = None
        process_id: int | None = None
        if available:
            try:
                version = str(self._app.Version)
                document_id = self._document_id(self._document)
                process_id = self._pid_from_hwnd(int(self._app.HWND))
                if process_id <= 0:
                    raise OSError("AutoCAD window returned an invalid process id")
            except Exception:  # pragma: no cover - environment specific
                available = False
                process_id = None
        return AdapterStatus(
            adapter_type=self.adapter_type,
            available=available,
            capabilities=tuple(sorted(self.capabilities, key=lambda c: c.value)),
            cad_application=self.prog_id,
            cad_version=version,
            active_document_id=document_id,
            process_id=process_id,
            message=None if available else "Not connected. Call connect() first.",
        )

    @staticmethod
    def _document_id(document: Any) -> str:
        """Stable per-file identity derived from the normalized full path."""
        try:
            full_name = str(document.FullName) or str(document.Name)
        except Exception:
            full_name = "unsaved-document"
        return f"doc_{sha256_of(full_name.strip().lower()).removeprefix('sha256:')[:26].upper()}"

    @staticmethod
    def _com_entity_type(entity: Any) -> str:
        """Read the ActiveX entity discriminator across late-bound AutoCAD versions."""
        for attribute in ("ObjectName", "EntityName"):
            with contextlib.suppress(Exception):
                value = str(getattr(entity, attribute)).strip()
                if value:
                    return value
        return "AcDbUnknownEntity"

    def inspect_document(self, request: InspectRequest) -> DocumentSnapshot:
        self.require(AdapterCapability.INSPECT_DOCUMENT)
        document = self._require_document()
        try:
            layers: list[LayerInfo] = []
            if request.include_layers:
                with (
                    _scoped_remote(document.Layers) as layer_collection,
                    _scoped_iterator(layer_collection) as layer_iterator,
                ):
                    for layer in layer_iterator:
                        layers.append(
                            LayerInfo(
                                name=str(layer.Name),
                                color_index=int(layer.Color),
                                linetype=str(layer.Linetype),
                                lineweight=int(layer.Lineweight),
                                frozen=bool(layer.Freeze),
                                off=not bool(layer.LayerOn),
                                locked=bool(layer.Lock),
                            )
                        )

            styles: tuple[str, ...] = ()
            text_styles: tuple[str, ...] = ()
            if request.include_styles:
                with (
                    _scoped_remote(document.DimStyles) as dim_styles,
                    _scoped_iterator(dim_styles) as style_iterator,
                ):
                    styles = tuple(str(style.Name) for style in style_iterator)
                with (
                    _scoped_remote(document.TextStyles) as raw_text_styles,
                    _scoped_iterator(raw_text_styles) as text_style_iterator,
                ):
                    text_styles = tuple(str(style.Name) for style in text_style_iterator)

            insunits = int(document.GetVariable("INSUNITS"))
            with _scoped_remote(document.ModelSpace) as model_space:
                entity_count = int(model_space.Count)
            document_id = self._document_id(document)

            return DocumentSnapshot(
                document_id=document_id,
                revision=self._compute_revision(document, document_id),
                path_hash=sha256_of(str(document.Name).strip().lower()),
                display_name=str(document.Name),
                units=_INSUNITS_TO_UNIT.get(insunits, Unit.MM),
                active_space="model" if int(document.ActiveSpace) == 1 else "paper",
                layers=tuple(layers),
                dimension_styles=styles,
                text_styles=text_styles,
                entity_count=entity_count,
                read_only=bool(document.ReadOnly),
            )
        except ComCallFailedError:
            raise
        except Exception as exc:
            raise ComCallFailedError("Document inspection failed") from exc

    def inspect_selection(self, request: SelectionRequest) -> SelectionSnapshot:
        self.require(AdapterCapability.INSPECT_SELECTION)
        document = self._require_document()
        try:
            with _scoped_remote(document.ActiveSelectionSet) as selection:
                count = int(selection.Count)
                limit = min(count, request.max_entities)
                entities: list[EntitySummary] = []
                with _scoped_iterator(selection) as selection_iterator:
                    for index, entity in enumerate(selection_iterator):
                        if index >= limit:
                            break
                        entities.append(
                            EntitySummary(
                                entity_ref=f"acad:handle:{entity.Handle}",
                                entity_type=self._com_entity_type(entity),
                                layer=str(entity.Layer),
                            )
                        )
            return SelectionSnapshot(
                document_id=self._document_id(document),
                revision=self._compute_revision(document, self._document_id(document)),
                entities=tuple(entities),
                truncated=count > limit,
            )
        except Exception as exc:
            raise ComCallFailedError("Selection inspection failed") from exc

    def inspect_semantic_drawing(
        self,
        request: DrawingReadRequest,
        deadline: CancellationTokenPort | None = None,
    ) -> DrawingModel:
        """Read bounded model-space geometry through the same COM revision authority.

        ActiveX exposes many entity families incompletely.  This path therefore
        supports only geometry whose full 2D contract can be observed without
        invention. Unsupported entities are counted and omitted. The revision is
        checked again after enumeration so remediation can never bind a mixed-state
        model to an older COM revision.
        """
        if request.scope is None or request.scope.kind != "model_space":
            raise AdapterCapabilityMissingError(
                "COM semantic reading currently supports model space only",
                required_action="Read model space or use the .NET bridge for another scope",
                details={
                    "adapter_type": self.adapter_type,
                    "missing_capability": "semantic_scoped_geometry_read",
                },
            )
        document = self._require_document()
        document_id = self._document_id(document)
        if document_id != request.source.ref:
            raise ComCallFailedError(
                "COM semantic read targeted a different active document",
                required_action="Activate the requested drawing and retry",
                details={"requested_document_id": request.source.ref},
            )
        if deadline is not None:
            deadline.checkpoint()
        before = self.inspect_document(
            InspectRequest(document_id=document_id, include_layers=True, include_styles=True)
        )
        unit_code = int(document.GetVariable("INSUNITS"))
        source_unit_code, factor = _INSUNITS_TO_MM.get(unit_code, ("unknown", None))
        scale = factor if factor is not None else 1.0
        unsupported: Counter[str] = Counter()
        records: list[EntityRecord] = []
        try:
            with _scoped_remote(document.ModelSpace) as model_space:
                observed_count = int(model_space.Count)
                if observed_count > request.max_entities:
                    raise ReadScopeTooLargeError(
                        "COM model space exceeds the approved semantic-read budget",
                        required_action="Narrow the read scope or increase the configured limit",
                        details={
                            "entity_count": observed_count,
                            "limit": request.max_entities,
                        },
                    )
                with _scoped_iterator(model_space) as iterator:
                    for entity in iterator:
                        if deadline is not None:
                            deadline.checkpoint()
                        entity_type = self._com_entity_type(entity)
                        if entity_type not in _COM_SEMANTIC_ENTITY_TYPES:
                            unsupported[entity_type] += 1
                            continue
                        record = self._semantic_entity_record(entity, entity_type, scale)
                        if record is None:
                            unsupported[f"{entity_type}:unsupported_geometry"] += 1
                            continue
                        records.append(record)
        except (AdapterCapabilityMissingError, ReadScopeTooLargeError):
            raise
        except Exception as exc:
            raise ComCallFailedError(
                "COM semantic model-space inspection failed",
                required_action="Finish the active AutoCAD command and retry the read",
            ) from exc
        if deadline is not None:
            deadline.checkpoint()
        after = self.inspect_document(
            InspectRequest(document_id=document_id, include_layers=True, include_styles=True)
        )
        if before.revision != after.revision:
            raise ComCallFailedError(
                "The active drawing changed during COM semantic inspection",
                required_action="Freeze drawing edits and read the drawing again",
            )
        unsupported_records = tuple(
            UnsupportedEntityCount(entity_type=name, count=count)
            for name, count in sorted(unsupported.items())
        )
        return DrawingModel(
            document_id=document_id,
            revision=after.revision,
            display_name=after.display_name,
            source_unit_code=source_unit_code,
            to_mm_factor=factor,
            geometry_normalized=factor is not None,
            scope=request.scope,
            entities=tuple(records),
            layers=after.layers,
            dimension_styles=after.dimension_styles,
            text_styles=after.text_styles,
            unsupported=unsupported_records,
            coverage_complete=not unsupported_records,
            arc_chord_tolerance_mm=0.01,
        )

    @classmethod
    def _semantic_entity_record(
        cls, entity: Any, entity_type: str, scale: float
    ) -> EntityRecord | None:
        geometry: LineGeometry | CircleGeometry | ArcGeometry | PolylineGeometry
        if entity_type == "AcDbLine":
            start = cls._scaled_point(entity.StartPoint, scale)
            end = cls._scaled_point(entity.EndPoint, scale)
            geometry = LineGeometry(start_mm=start, end_mm=end)
            bounds = cls._point_bounds((start, end))
        elif entity_type == "AcDbCircle":
            center = cls._scaled_point(entity.Center, scale)
            radius = float(entity.Radius) * scale
            geometry = CircleGeometry(center_mm=center, radius_mm=radius)
            bounds = (
                center[0] - radius,
                center[1] - radius,
                center[0] + radius,
                center[1] + radius,
            )
        elif entity_type == "AcDbArc":
            center = cls._scaled_point(entity.Center, scale)
            radius = float(entity.Radius) * scale
            start_angle = math.degrees(float(entity.StartAngle))
            end_angle = math.degrees(float(entity.EndAngle))
            geometry = ArcGeometry(
                center_mm=center,
                radius_mm=radius,
                start_angle_deg=start_angle,
                end_angle_deg=end_angle,
            )
            bounds = cls._arc_bounds(center, radius, start_angle, end_angle)
        else:
            coordinates = tuple(float(value) for value in entity.Coordinates)
            if len(coordinates) < 4 or len(coordinates) % 2:
                return None
            points = tuple(
                (coordinates[index] * scale, coordinates[index + 1] * scale)
                for index in range(0, len(coordinates), 2)
            )
            vertices: list[PolylineVertex] = []
            for index, point in enumerate(points):
                bulge = float(entity.GetBulge(index))
                if not math.isfinite(bulge):
                    return None
                # Exact curved-polyline bounds require OCS-aware bulge handling. Fail
                # closed until that contract is implemented instead of flattening arcs.
                if not math.isclose(bulge, 0.0, rel_tol=0.0, abs_tol=1e-12):
                    return None
                vertices.append(PolylineVertex(point_mm=point, bulge=bulge))
            geometry = PolylineGeometry(vertices=tuple(vertices), closed=bool(entity.Closed))
            bounds = cls._point_bounds(points)
        return EntityRecord(
            entity_ref=f"acad:handle:{str(entity.Handle).strip().upper()}",
            entity_type=entity_type,
            layer=str(entity.Layer),
            visible=bool(entity.Visible),
            space="model",
            geometry=geometry,
            bounding_box_mm=bounds,
        )

    @staticmethod
    def _scaled_point(value: Any, scale: float) -> tuple[float, float]:
        return (float(value[0]) * scale, float(value[1]) * scale)

    @staticmethod
    def _point_bounds(points: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float]:
        return (
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        )

    @classmethod
    def _arc_bounds(
        cls,
        center: tuple[float, float],
        radius: float,
        start_angle_deg: float,
        end_angle_deg: float,
    ) -> tuple[float, float, float, float]:
        start = math.radians(start_angle_deg) % math.tau
        end = math.radians(end_angle_deg) % math.tau
        sweep = (end - start) % math.tau
        angles = [start, end]
        for angle in (0.0, math.pi / 2.0, math.pi, 3.0 * math.pi / 2.0):
            if (angle - start) % math.tau <= sweep + 1e-12:
                angles.append(angle)
        points = tuple(
            (
                center[0] + radius * math.cos(angle),
                center[1] + radius * math.sin(angle),
            )
            for angle in angles
        )
        return cls._point_bounds(points)

    def _compute_revision(self, document: Any, document_id: str) -> str:
        """Coarse MVP revision: entity count plus a digest of model space handles.

        Good enough to detect that *something* changed; not good enough to be the
        long-term answer, which is why the C# bridge is on the roadmap.
        """
        try:
            with _scoped_remote(document.ModelSpace) as model_space:
                count = int(model_space.Count)
                with _scoped_iterator(model_space) as model_space_iterator:
                    handles = [str(entity.Handle) for entity in model_space_iterator]
            return self.revision_from(document_id, [count, sorted(handles)])
        except Exception as exc:
            raise ComCallFailedError("Could not compute a document revision") from exc

    def validate_revision(self, document_id: str, expected_revision: str) -> bool:
        document = self._require_document()
        return (
            self._document_id(document) == document_id
            and self._compute_revision(document, document_id) == expected_revision
        )

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    def preview(self, plan: OperationPlan) -> PreviewResult:
        """Not supported. Preview goes through :class:`DxfPreviewAdapter` under COM.

        Drawing preview geometry into the live document and erasing it again risks
        leaving artifacts behind, which violates "preview must not modify the DWG".
        """
        raise AdapterCapabilityMissingError(
            "The COM adapter cannot preview inside AutoCAD",
            required_action="Generate the preview with the DXF preview adapter",
            details={"adapter_type": self.adapter_type},
        )

    def commit(self, request: CommitRequest) -> CommitResult:
        self.require(AdapterCapability.COMMIT)
        document = self._require_document()
        document_id = self._document_id(document)

        previous_revision = self._compute_revision(document, document_id)
        if previous_revision != request.expected_revision:
            raise StaleDocumentRevisionError(
                "Document changed since the plan was approved",
                required_action="Re-inspect, regenerate preview, validate and approve again",
                details={
                    "expected_revision": request.expected_revision,
                    "actual_revision": previous_revision,
                },
            )

        undo_group = f"cad-harness-{request.plan.job_id}"
        results: list[EntityResult] = []
        document.StartUndoMark()
        try:
            for operation in request.plan.operations:
                results.extend(self._execute(document, document_id, operation))
        except Exception as exc:
            with contextlib.suppress(Exception):
                document.EndUndoMark()
            raise UnknownCommitStateError(
                "Commit failed part-way through the plan",
                required_action="Reconcile the job against the checkpoint before retrying",
                details={"completed_operations": len(results)},
            ) from exc
        document.EndUndoMark()

        return CommitResult(
            job_id=request.plan.job_id,
            plan_hash=request.plan.plan_hash or request.plan.compute_hash(),
            status=CommitStatus.COMMITTED,
            entity_results=tuple(results),
            previous_revision=previous_revision,
            new_revision=self._compute_revision(document, document_id),
            undo_group=undo_group,
        )

    def _execute(self, document: Any, document_id: str, operation: Operation) -> list[EntityResult]:
        """Dispatch exactly once; handlers only translate resolved plan fields to COM."""
        try:
            handler_name = self.OPERATION_DISPATCH[operation.type]
        except KeyError as exc:
            raise AdapterCapabilityMissingError(
                f"The COM adapter has no mapping for operation type '{operation.type.value}'",
                required_action="Add the mapping or compile a plan the adapter supports",
                details={"operation_type": operation.type.value, "adapter_type": self.adapter_type},
            ) from exc

        handler = getattr(self, handler_name)
        entities = handler(document, document_id, operation)
        count = len(entities)
        try:
            return [self._result_from_entity(operation, entity, count=count) for entity in entities]
        finally:
            for entity in entities:
                _release_remote(entity)

    def _result_from_entity(self, operation: Operation, entity: Any, *, count: int) -> EntityResult:
        measurement_handlers = {
            OperationType.CREATE_LINE: self._measure_line,
            OperationType.CREATE_POLYLINE: self._measure_polyline,
            OperationType.CREATE_CLOSED_POLYLINE: self._measure_polyline,
            OperationType.CREATE_CIRCLE: self._measure_circle,
            OperationType.CREATE_CIRCLES: self._measure_circle,
            OperationType.CREATE_ARC: self._measure_arc,
            OperationType.CREATE_TEXT: self._measure_text,
            OperationType.CREATE_CENTERLINE: self._measure_line,
            OperationType.CREATE_CENTERMARK: self._measure_point,
            OperationType.CREATE_LINEAR_DIMENSION: self._measure_dimension,
            OperationType.CREATE_ALIGNED_DIMENSION: self._measure_dimension,
            OperationType.CREATE_ANGULAR_DIMENSION: self._measure_dimension,
            OperationType.CREATE_DIAMETER_DIMENSION: self._measure_dimension,
            OperationType.CREATE_RADIUS_DIMENSION: self._measure_dimension,
            OperationType.CREATE_HATCH: self._measure_hatch,
            OperationType.UPDATE_ENTITY: self._measure_generic,
            OperationType.DELETE_ENTITY: self._measure_deleted,
        }
        measurements = measurement_handlers[operation.type](entity)
        if operation.type is OperationType.CREATE_CIRCLES:
            measurements["count"] = count
        return EntityResult(
            operation_id=operation.operation_id,
            feature_id=operation.feature_id,
            entity_ref=f"acad:handle:{entity.Handle}",
            entity_type=str(entity.ObjectName),
            measurements=measurements,
        )

    @staticmethod
    def _point(value: Any) -> list[float]:
        return [float(value[0]), float(value[1])]

    @staticmethod
    def _invoke_model_space(document: Any, member: str, *args: Any) -> Any:
        with _scoped_remote(document.ModelSpace) as model_space:
            return getattr(model_space, member)(*args)

    def _create_line(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        del document_id
        geometry = operation.geometry
        entity = self._invoke_model_space(
            document,
            "AddLine",
            self._variant_point3d(*map(float, geometry["start_mm"][:2])),
            self._variant_point3d(*map(float, geometry["end_mm"][:2])),
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_polyline(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        del document_id
        vertices = operation.geometry["vertices_mm"]
        flat = [float(coordinate) for vertex in vertices for coordinate in vertex[:2]]
        entity = self._invoke_model_space(
            document, "AddLightWeightPolyline", self._variant_doubles(flat)
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_closed_polyline(
        self, document: Any, document_id: str, operation: Operation
    ) -> list[Any]:
        entities = self._create_polyline(document, document_id, operation)
        entities[0].Closed = True
        return entities

    def _create_circle(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        del document_id
        geometry = operation.geometry
        center = geometry["center_mm"]
        entity = self._invoke_model_space(
            document,
            "AddCircle",
            self._variant_point3d(float(center[0]), float(center[1])),
            float(geometry["diameter_mm"]) / 2.0,
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_circles(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        del document_id
        geometry = operation.geometry
        radius = float(geometry["diameter_mm"]) / 2.0
        entities = []
        for center in geometry["centers_mm"]:
            entity = self._invoke_model_space(
                document,
                "AddCircle",
                self._variant_point3d(float(center[0]), float(center[1])),
                radius,
            )
            entity.Layer = operation.layer
            entities.append(entity)
        return entities

    def _create_arc(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        del document_id
        geometry = operation.geometry
        center = geometry["center_mm"]
        entity = self._invoke_model_space(
            document,
            "AddArc",
            self._variant_point3d(float(center[0]), float(center[1])),
            float(geometry["radius_mm"]),
            math.radians(float(geometry["start_angle_deg"])),
            math.radians(float(geometry["end_angle_deg"])),
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_text(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        del document_id
        geometry = operation.geometry
        point = geometry["insertion_point_mm"]
        entity = self._invoke_model_space(
            document,
            "AddText",
            str(geometry["text"]),
            self._variant_point3d(float(point[0]), float(point[1])),
            float(geometry["height_mm"]),
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_centerline(
        self, document: Any, document_id: str, operation: Operation
    ) -> list[Any]:
        return self._create_line(document, document_id, operation)

    def _create_centermark(
        self, document: Any, document_id: str, operation: Operation
    ) -> list[Any]:
        del document_id
        center = operation.geometry["center_mm"]
        entity = self._invoke_model_space(
            document, "AddPoint", self._variant_point3d(float(center[0]), float(center[1]))
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_linear_dimension(
        self, document: Any, document_id: str, operation: Operation
    ) -> list[Any]:
        del document_id
        geometry = operation.geometry
        start = geometry["extension_line_1_mm"]
        end = geometry["extension_line_2_mm"]
        dimension_line = geometry["dimension_line_point_mm"]
        entity = self._invoke_model_space(
            document,
            "AddDimRotated",
            self._variant_point3d(float(start[0]), float(start[1])),
            self._variant_point3d(float(end[0]), float(end[1])),
            self._variant_point3d(float(dimension_line[0]), float(dimension_line[1])),
            math.radians(float(geometry["rotation_deg"])),
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_aligned_dimension(
        self, document: Any, document_id: str, operation: Operation
    ) -> list[Any]:
        del document_id
        geometry = operation.geometry
        start = geometry["extension_line_1_mm"]
        end = geometry["extension_line_2_mm"]
        dimension_line = geometry["dimension_line_point_mm"]
        entity = self._invoke_model_space(
            document,
            "AddDimAligned",
            self._variant_point3d(float(start[0]), float(start[1])),
            self._variant_point3d(float(end[0]), float(end[1])),
            self._variant_point3d(float(dimension_line[0]), float(dimension_line[1])),
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_angular_dimension(
        self, document: Any, document_id: str, operation: Operation
    ) -> list[Any]:
        del document_id
        geometry = operation.geometry
        vertex = geometry["vertex_mm"]
        first = geometry["first_end_point_mm"]
        second = geometry["second_end_point_mm"]
        text_point = geometry["text_point_mm"]
        entity = self._invoke_model_space(
            document,
            "AddDimAngular",
            self._variant_point3d(float(vertex[0]), float(vertex[1])),
            self._variant_point3d(float(first[0]), float(first[1])),
            self._variant_point3d(float(second[0]), float(second[1])),
            self._variant_point3d(float(text_point[0]), float(text_point[1])),
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_diameter_dimension(
        self, document: Any, document_id: str, operation: Operation
    ) -> list[Any]:
        del document_id
        geometry = operation.geometry
        chord = geometry["chord_point_mm"]
        far_chord = geometry["far_chord_point_mm"]
        entity = self._invoke_model_space(
            document,
            "AddDimDiametric",
            self._variant_point3d(float(chord[0]), float(chord[1])),
            self._variant_point3d(float(far_chord[0]), float(far_chord[1])),
            float(geometry["leader_length_mm"]),
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_radius_dimension(
        self, document: Any, document_id: str, operation: Operation
    ) -> list[Any]:
        del document_id
        geometry = operation.geometry
        center = geometry["center_mm"]
        chord = geometry["chord_point_mm"]
        entity = self._invoke_model_space(
            document,
            "AddDimRadial",
            self._variant_point3d(float(center[0]), float(center[1])),
            self._variant_point3d(float(chord[0]), float(chord[1])),
            float(geometry["leader_length_mm"]),
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_hatch(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        del document_id
        geometry = operation.geometry
        entity = self._invoke_model_space(
            document,
            "AddHatch",
            int(geometry["pattern_type"]),
            str(geometry["pattern_name"]),
            bool(geometry["associative"]),
        )
        handles = [str(item).removeprefix("acad:handle:") for item in geometry["boundary_refs"]]
        boundary = [document.HandleToObject(handle) for handle in handles]
        entity.AppendOuterLoop(self._variant_objects(boundary))
        entity.Layer = operation.layer
        entity.Evaluate()
        return [entity]

    def _update_entity(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        entity = self._resolve_mapped_entity(document, document_id, operation)
        entity.Layer = operation.layer
        for name, value in operation.geometry["properties"].items():
            setattr(entity, name, value)
        entity.Update()
        return [entity]

    def _delete_entity(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        entity = self._resolve_mapped_entity(document, document_id, operation)
        try:
            snapshot = _DeletedEntitySnapshot(
                Handle=str(entity.Handle),
                ObjectName=self._com_entity_type(entity),
            )
            entity.Delete()
            return [snapshot]
        finally:
            # A successfully deleted ActiveX proxy is no longer readable. Release it
            # here and return only the pre-delete receipt fields to the result mapper.
            _release_remote(entity)

    def _resolve_mapped_entity(self, document: Any, document_id: str, operation: Operation) -> Any:
        entity_ref = operation.target_entity_ref
        mappings = self.job_store.entity_mappings_for(document_id) if self.job_store else ()
        mapped = entity_ref is not None and any(
            mapping.entity_ref == entity_ref for mapping in mappings
        )
        trusted_remediation_ref = (
            entity_ref is not None
            and operation.feature_id.startswith("remediation:")
            and _COM_HANDLE_REF.fullmatch(entity_ref) is not None
        )
        if not mapped and not trusted_remediation_ref:
            raise ComCallFailedError(
                "The operation references an entity that is not mapped or audit-bound",
                required_action=(
                    "Re-inspect and audit the drawing, then recreate the approved remediation plan"
                ),
                details={
                    "reason": "entity_reference_not_found",
                    "entity_ref": entity_ref,
                    "document_id": document_id,
                },
            )
        assert entity_ref is not None
        handle = entity_ref.removeprefix("acad:handle:")
        try:
            return document.HandleToObject(handle)
        except Exception as exc:
            raise ComCallFailedError(
                "The mapped entity reference no longer resolves in AutoCAD",
                required_action="Reconcile entity mappings against the current drawing",
                details={"reason": "entity_reference_stale", "entity_ref": entity_ref},
            ) from exc

    def _variant_objects(self, values: list[Any]) -> Any:
        if self._remote_com_owner is not None:
            return _RemoteVariant("objects", tuple(values))

        import pythoncom  # noqa: TID251
        import win32com.client  # noqa: TID251

        return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, values)

    @classmethod
    def _measure_line(cls, entity: Any) -> dict[str, Any]:
        return {
            "start_mm": cls._point(entity.StartPoint),
            "end_mm": cls._point(entity.EndPoint),
            "length_mm": float(entity.Length),
        }

    @classmethod
    def _measure_polyline(cls, entity: Any) -> dict[str, Any]:
        coordinates = [float(value) for value in entity.Coordinates]
        xs = coordinates[0::2]
        ys = coordinates[1::2]
        try:
            area = float(entity.Area)
        except Exception:  # pragma: no cover - open polylines have no area
            area = 0.0
        return {
            "closed": bool(entity.Closed),
            "vertex_count": len(coordinates) // 2,
            "width_mm": max(xs) - min(xs),
            "height_mm": max(ys) - min(ys),
            "area_mm2": area,
            "perimeter_mm": float(entity.Length),
        }

    @classmethod
    def _measure_circle(cls, entity: Any) -> dict[str, Any]:
        return {
            "layer": str(entity.Layer),
            "center_mm": cls._point(entity.Center),
            "diameter_mm": float(entity.Diameter),
            "radius_mm": float(entity.Radius),
            "area_mm2": float(entity.Area),
            "circumference_mm": float(entity.Circumference),
        }

    @classmethod
    def _measure_arc(cls, entity: Any) -> dict[str, Any]:
        return {
            "center_mm": cls._point(entity.Center),
            "radius_mm": float(entity.Radius),
            "length_mm": float(entity.ArcLength),
            "start_angle_deg": math.degrees(float(entity.StartAngle)),
            "end_angle_deg": math.degrees(float(entity.EndAngle)),
        }

    @classmethod
    def _measure_text(cls, entity: Any) -> dict[str, Any]:
        return {
            "text": str(entity.TextString),
            "insertion_point_mm": cls._point(entity.InsertionPoint),
            "height_mm": float(entity.Height),
        }

    @classmethod
    def _measure_point(cls, entity: Any) -> dict[str, Any]:
        return {"center_mm": cls._point(entity.Coordinates)}

    @classmethod
    def _measure_dimension(cls, entity: Any) -> dict[str, Any]:
        measurements: dict[str, Any] = {"measurement_mm": float(entity.Measurement)}
        with contextlib.suppress(Exception):
            measurements["text_position_mm"] = cls._point(entity.TextPosition)
        return measurements

    @staticmethod
    def _measure_hatch(entity: Any) -> dict[str, Any]:
        return {"area_mm2": float(entity.Area), "pattern_name": str(entity.PatternName)}

    @staticmethod
    def _measure_generic(entity: Any) -> dict[str, Any]:
        return {"layer": str(entity.Layer)}

    @staticmethod
    def _measure_deleted(entity: Any) -> dict[str, Any]:
        del entity
        return {"deleted": True}

    def rollback(self, request: RollbackRequest) -> RollbackResult:
        """Fail closed: ActiveX exposes no typed atomic checkpoint-restore operation."""
        raise RollbackNotAvailableError(
            "The COM adapter cannot restore a checkpoint safely",
            required_action="Restore the checkpoint in AutoCAD or use the verified bridge adapter",
            details={"checkpoint_id": request.checkpoint_id, "job_id": request.job_id},
        )

    def export(self, request: ExportRequest) -> ExportResult:
        self.require(AdapterCapability.EXPORT)
        document = self._require_document()
        try:
            if request.format.lower() in {"dwg", "dxf"}:
                document.SaveAs(request.target_path)
            elif request.format.lower() == "pdf":
                with _scoped_remote(document.Plot) as plot:
                    plot.PlotToFile(request.target_path)
            else:
                raise AdapterCapabilityMissingError(
                    f"Unsupported export format '{request.format}'",
                    details={"supported": ["dwg", "dxf", "pdf"]},
                )
        except AdapterCapabilityMissingError:
            raise
        except Exception as exc:
            raise ComCallFailedError("Export failed", details={"format": request.format}) from exc

        return ExportResult(
            document_id=self._document_id(document),
            format=request.format,
            artifact_ref=request.target_path,
        )
