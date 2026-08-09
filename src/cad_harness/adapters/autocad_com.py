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

import contextlib
import math
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from cad_harness.adapters.base import BaseAdapter
from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.errors import (
    AdapterCapabilityMissingError,
    AutoCADBusyError,
    AutoCADNotRunningError,
    ComCallFailedError,
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
from cad_harness.domain.ports.repositories import JobStore
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

_VERSIONED_AUTOCAD_PROG_ID = re.compile(r"AutoCAD\.Application\.\d+(?:\.\d+)?", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class OwnedComSession:
    """Identity proof for an AutoCAD process created by this adapter instance."""

    prog_id: str
    hwnd: int
    pid: int
    image_path: str
    creation_time_100ns: int
    owned: bool = True


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
    ) -> None:
        self.prog_id = PROG_IDS.get(prog_id_key.lower(), PROG_IDS["autocad"])
        self.startup_wait_seconds = startup_wait_seconds
        self.job_store = job_store
        self._app: Any = None
        self._document: Any = None
        self._owned_session: OwnedComSession | None = None

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

        pythoncom.CoInitialize()
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

    def connect_isolated(self, *, versioned_prog_id: str) -> OwnedComSession:
        """Create and prove ownership of a new, version-specific AutoCAD process.

        The returned COM application is intentionally not made available through
        ``_app`` until its HWND maps to a newly created process whose exact image
        name is ``acad.exe``. No application property other than ``HWND`` is read,
        and no visibility, document, or shutdown operation occurs before that proof.
        Callers may then use :meth:`require_owned_application` to perform explicit
        scratch-session setup. This method never attaches to an existing process.
        """
        if _VERSIONED_AUTOCAD_PROG_ID.fullmatch(versioned_prog_id) is None:
            raise ValueError("versioned_prog_id must look like 'AutoCAD.Application.26'")
        if self._app is not None:
            raise ComCallFailedError(
                "Adapter is already connected",
                details={"reason": "connection_already_exists"},
            )

        import pythoncom  # noqa: TID251 - COM is confined to this module
        import win32com.client  # noqa: TID251

        pythoncom.CoInitialize()
        app: Any = None
        try:
            preexisting_pids = self._acad_process_ids()
            dispatch_started_100ns = self._system_filetime_100ns()
            app = win32com.client.DispatchEx(versioned_prog_id)
            hwnd = int(app.HWND)
            pid = self._pid_from_hwnd(hwnd)
            current_acad_pids = self._acad_process_ids()
            image_path, creation_time_100ns = self._process_identity(pid)
            if (
                pid in preexisting_pids
                or pid not in current_acad_pids
                or Path(image_path).name.casefold() != "acad.exe"
                or creation_time_100ns < dispatch_started_100ns
            ):
                raise ComCallFailedError(
                    "Could not prove ownership of the isolated AutoCAD process",
                    required_action="Close the unverified automation instance manually if needed",
                    details={"reason": "isolated_process_ownership_unproven"},
                )

            session = OwnedComSession(
                prog_id=versioned_prog_id,
                hwnd=hwnd,
                pid=pid,
                image_path=image_path,
                creation_time_100ns=creation_time_100ns,
            )
            self._app = app
            self._owned_session = session
            return session
        except Exception:
            # Releasing the automation proxy is the only safe cleanup while process
            # ownership is unproven. In particular, never call Application.Quit.
            app = None
            with contextlib.suppress(Exception):
                pythoncom.CoUninitialize()
            raise

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
        if self._app is None or self._owned_session is None:
            raise ComCallFailedError(
                "No proven isolated AutoCAD session is connected",
                details={"reason": "owned_session_required"},
            )
        return self._app

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
            app.Documents.Open(str(candidate), bool(read_only))
            deadline = time.monotonic() + self.startup_wait_seconds
            while time.monotonic() < deadline:
                try:
                    active = app.ActiveDocument
                    opened = Path(str(active.FullName)).resolve(strict=True)
                    if opened == candidate:
                        document = active
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            if document is None:
                raise ComCallFailedError(
                    "AutoCAD did not activate the requested scratch document",
                    details={"reason": "owned_document_open_timeout"},
                )
            self._document = document
            return self._document_id(document)
        except Exception:
            if document is not None:
                with contextlib.suppress(Exception):
                    document.Close(False)
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
                for index in range(int(app.Documents.Count) - 1, -1, -1):
                    app.Documents.Item(index).Close(False)
                app.Quit()
                self.disconnect()
                return
            except Exception as exc:  # AutoCAD rejects COM calls while startup is still busy.
                last_error = exc
                time.sleep(0.2)
        raise ComCallFailedError(
            "Owned AutoCAD scratch session did not close before the deadline",
            required_action="Close only the PID reported by the isolated acceptance evidence",
            details={"reason": "owned_session_close_timeout"},
        ) from last_error

    def disconnect(self) -> None:
        import pythoncom  # noqa: TID251

        self._app = None
        self._document = None
        self._owned_session = None
        with contextlib.suppress(Exception):  # shutdown is best effort
            pythoncom.CoUninitialize()

    def _require_document(self) -> Any:
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
            state = self._app.GetAcadState()
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
                if bool(self._app.GetAcadState().IsQuiescent):
                    return
            except Exception:
                pass
            time.sleep(0.5)

    # ------------------------------------------------------------------ #
    # COM marshalling helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _variant_doubles(values: list[float]) -> Any:
        """Wrap a flat float list as a COM ``VT_ARRAY | VT_R8`` variant.

        ActiveX rejects plain Python lists for coordinate arguments, so every point
        must go through this.
        """
        import pythoncom  # noqa: TID251
        import win32com.client  # noqa: TID251

        return win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values]
        )

    @classmethod
    def _variant_point3d(cls, x: float, y: float, z: float = 0.0) -> Any:
        return cls._variant_doubles([x, y, z])

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
        available = self._app is not None and self._document is not None
        version: str | None = None
        document_id: str | None = None
        if available:
            try:
                version = str(self._app.Version)
                document_id = self._document_id(self._document)
            except Exception:  # pragma: no cover - environment specific
                available = False
        return AdapterStatus(
            adapter_type=self.adapter_type,
            available=available,
            capabilities=tuple(sorted(self.capabilities, key=lambda c: c.value)),
            cad_application=self.prog_id,
            cad_version=version,
            active_document_id=document_id,
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
                for layer in document.Layers:
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
                styles = tuple(str(style.Name) for style in document.DimStyles)
                text_styles = tuple(str(style.Name) for style in document.TextStyles)

            insunits = int(document.GetVariable("INSUNITS"))
            entity_count = int(document.ModelSpace.Count)
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
            selection = document.ActiveSelectionSet
            count = int(selection.Count)
            limit = min(count, request.max_entities)
            entities: list[EntitySummary] = []
            for index, entity in enumerate(selection):
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

    def _compute_revision(self, document: Any, document_id: str) -> str:
        """Coarse MVP revision: entity count plus a digest of model space handles.

        Good enough to detect that *something* changed; not good enough to be the
        long-term answer, which is why the C# bridge is on the roadmap.
        """
        try:
            model_space = document.ModelSpace
            count = int(model_space.Count)
            handles = [str(entity.Handle) for entity in model_space]
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
        return [self._result_from_entity(operation, entity, count=count) for entity in entities]

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

    def _create_line(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        del document_id
        geometry = operation.geometry
        entity = document.ModelSpace.AddLine(
            self._variant_point3d(*map(float, geometry["start_mm"][:2])),
            self._variant_point3d(*map(float, geometry["end_mm"][:2])),
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_polyline(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        del document_id
        vertices = operation.geometry["vertices_mm"]
        flat = [float(coordinate) for vertex in vertices for coordinate in vertex[:2]]
        entity = document.ModelSpace.AddLightWeightPolyline(self._variant_doubles(flat))
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
        entity = document.ModelSpace.AddCircle(
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
            entity = document.ModelSpace.AddCircle(
                self._variant_point3d(float(center[0]), float(center[1])), radius
            )
            entity.Layer = operation.layer
            entities.append(entity)
        return entities

    def _create_arc(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        del document_id
        geometry = operation.geometry
        center = geometry["center_mm"]
        entity = document.ModelSpace.AddArc(
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
        entity = document.ModelSpace.AddText(
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
        entity = document.ModelSpace.AddPoint(
            self._variant_point3d(float(center[0]), float(center[1]))
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
        entity = document.ModelSpace.AddDimRotated(
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
        entity = document.ModelSpace.AddDimAligned(
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
        entity = document.ModelSpace.AddDimAngular(
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
        entity = document.ModelSpace.AddDimDiametric(
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
        entity = document.ModelSpace.AddDimRadial(
            self._variant_point3d(float(center[0]), float(center[1])),
            self._variant_point3d(float(chord[0]), float(chord[1])),
            float(geometry["leader_length_mm"]),
        )
        entity.Layer = operation.layer
        return [entity]

    def _create_hatch(self, document: Any, document_id: str, operation: Operation) -> list[Any]:
        del document_id
        geometry = operation.geometry
        entity = document.ModelSpace.AddHatch(
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
        entity.Delete()
        return [entity]

    def _resolve_mapped_entity(self, document: Any, document_id: str, operation: Operation) -> Any:
        entity_ref = operation.target_entity_ref
        mappings = self.job_store.entity_mappings_for(document_id) if self.job_store else ()
        if entity_ref is None or not any(mapping.entity_ref == entity_ref for mapping in mappings):
            raise ComCallFailedError(
                "The operation references an entity that is not mapped for this document",
                required_action=(
                    "Re-inspect the drawing and recreate the plan from current mappings"
                ),
                details={
                    "reason": "entity_reference_not_found",
                    "entity_ref": entity_ref,
                    "document_id": document_id,
                },
            )
        handle = entity_ref.removeprefix("acad:handle:")
        try:
            return document.HandleToObject(handle)
        except Exception as exc:
            raise ComCallFailedError(
                "The mapped entity reference no longer resolves in AutoCAD",
                required_action="Reconcile entity mappings against the current drawing",
                details={"reason": "entity_reference_stale", "entity_ref": entity_ref},
            ) from exc

    @staticmethod
    def _variant_objects(values: list[Any]) -> Any:
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
                document.Plot.PlotToFile(request.target_path)
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
