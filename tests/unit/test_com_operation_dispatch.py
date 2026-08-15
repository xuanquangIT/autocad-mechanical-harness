"""Unit coverage for COM operation dispatch without requiring AutoCAD."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import math
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

import cad_harness.adapters.autocad_com as autocad_com_module
from cad_harness.adapters.autocad_com import ComAutoCADAdapter, OwnedComSession
from cad_harness.domain.errors import ComCallFailedError
from cad_harness.domain.models.operation_plan import Operation, OperationType


class _Entity:
    ObjectName = "AcDbEntity"
    Layer = "OBJECT"
    Closed = False
    Coordinates = (0.0, 0.0, 10.0, 0.0, 10.0, 5.0)
    Area = 50.0
    Length = 25.0
    StartPoint = (0.0, 0.0, 0.0)
    EndPoint = (10.0, 0.0, 0.0)
    Center = (5.0, 5.0, 0.0)
    Diameter = 10.0
    Radius = 5.0
    Circumference = math.pi * 10.0
    ArcLength = math.pi * 5.0
    StartAngle = 0.0
    EndAngle = math.pi
    TextString = "NOTE"
    InsertionPoint = (1.0, 2.0, 0.0)
    Height = 2.5
    Measurement = 10.0
    TextPosition = (5.0, 2.0, 0.0)
    PatternName = "ANSI31"

    def __init__(self, handle: str) -> None:
        self.Handle = handle
        self.deleted = False

    def AppendOuterLoop(self, values: Any) -> None:  # noqa: N802
        self.boundary = values

    def Evaluate(self) -> None:  # noqa: N802
        self.evaluated = True

    def Update(self) -> None:  # noqa: N802
        self.updated = True

    def Delete(self) -> None:  # noqa: N802
        self.deleted = True


class _ModelSpace:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.counter = 0

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("Add"):
            raise AttributeError(name)

        def add(*args: Any) -> _Entity:
            del args
            self.calls.append(name)
            self.counter += 1
            entity = _Entity(f"{self.counter:X}")
            entity.ObjectName = {
                "AddLine": "AcDbLine",
                "AddLightWeightPolyline": "AcDbPolyline",
                "AddCircle": "AcDbCircle",
                "AddArc": "AcDbArc",
                "AddText": "AcDbText",
                "AddPoint": "AcDbPoint",
                "AddDimRotated": "AcDbRotatedDimension",
                "AddDimAligned": "AcDbAlignedDimension",
                "AddDimAngular": "AcDb2LineAngularDimension",
                "AddDimDiametric": "AcDbDiametricDimension",
                "AddDimRadial": "AcDbRadialDimension",
                "AddHatch": "AcDbHatch",
            }[name]
            return entity

        return add


class _Document:
    def __init__(self) -> None:
        self.ModelSpace = _ModelSpace()
        self.boundary = _Entity("BOUNDARY")

    def HandleToObject(self, handle: str) -> _Entity:  # noqa: N802
        assert handle == "BOUNDARY"
        return self.boundary


class _TestAdapter(ComAutoCADAdapter):
    @staticmethod
    def _variant_doubles(values: list[float]) -> list[float]:
        return values

    @classmethod
    def _variant_point3d(cls, x: float, y: float, z: float = 0.0) -> list[float]:
        return [x, y, z]

    @staticmethod
    def _variant_objects(values: list[Any]) -> list[Any]:
        return values


def test_status_reports_the_exact_window_process_id(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Application:
        Version = "26.0"
        HWND = 4242

    adapter = _TestAdapter()
    adapter._app = _Application()
    adapter._document = _Document()
    monkeypatch.setattr(adapter, "_pid_from_hwnd", lambda hwnd: 9260 if hwnd == 4242 else 0)

    status = adapter.status()

    assert status.available is True
    assert status.process_id == 9260
    assert status.cad_version == "26.0"


def test_scoped_iterator_supports_plain_local_iterators_and_closes_rpc_iterators() -> None:
    with autocad_com_module._scoped_iterator([1, 2]) as plain:
        assert list(plain) == [1, 2]

    closed = False

    class _ClosableIterator:
        def __iter__(self) -> _ClosableIterator:
            return self

        def __next__(self) -> int:
            raise StopIteration

        def close(self) -> None:
            nonlocal closed
            closed = True

    with autocad_com_module._scoped_iterator(_ClosableIterator()):
        pass
    assert closed is True


def _operation(kind: OperationType, geometry: dict[str, Any]) -> Operation:
    return Operation(
        operation_id=f"op-{kind.value}",
        feature_id="feature-dispatch",
        type=kind,
        layer="OBJECT",
        geometry=geometry,
        expected={"measurement_mm": -999.0},
    )


_CREATE_EXAMPLES = [
    (OperationType.CREATE_LINE, {"start_mm": [0, 0], "end_mm": [10, 0]}, "AddLine"),
    (OperationType.CREATE_POLYLINE, {"vertices_mm": [[0, 0], [10, 0]]}, "AddLightWeightPolyline"),
    (
        OperationType.CREATE_CLOSED_POLYLINE,
        {"vertices_mm": [[0, 0], [10, 0], [10, 5]]},
        "AddLightWeightPolyline",
    ),
    (OperationType.CREATE_CIRCLE, {"center_mm": [5, 5], "diameter_mm": 10}, "AddCircle"),
    (
        OperationType.CREATE_CIRCLES,
        {"centers_mm": [[5, 5], [15, 5]], "diameter_mm": 10},
        "AddCircle",
    ),
    (
        OperationType.CREATE_ARC,
        {"center_mm": [5, 5], "radius_mm": 5, "start_angle_deg": 0, "end_angle_deg": 180},
        "AddArc",
    ),
    (
        OperationType.CREATE_TEXT,
        {"text": "NOTE", "insertion_point_mm": [1, 2], "height_mm": 2.5},
        "AddText",
    ),
    (OperationType.CREATE_CENTERLINE, {"start_mm": [0, 0], "end_mm": [10, 0]}, "AddLine"),
    (OperationType.CREATE_CENTERMARK, {"center_mm": [5, 5]}, "AddPoint"),
    (
        OperationType.CREATE_LINEAR_DIMENSION,
        {
            "extension_line_1_mm": [0, 0],
            "extension_line_2_mm": [10, 0],
            "dimension_line_point_mm": [5, 2],
            "rotation_deg": 0,
        },
        "AddDimRotated",
    ),
    (
        OperationType.CREATE_ALIGNED_DIMENSION,
        {
            "extension_line_1_mm": [0, 0],
            "extension_line_2_mm": [10, 0],
            "dimension_line_point_mm": [5, 2],
        },
        "AddDimAligned",
    ),
    (
        OperationType.CREATE_ANGULAR_DIMENSION,
        {
            "vertex_mm": [0, 0],
            "first_end_point_mm": [10, 0],
            "second_end_point_mm": [0, 10],
            "text_point_mm": [3, 3],
        },
        "AddDimAngular",
    ),
]

_CREATE_EXAMPLES += [
    (
        OperationType.CREATE_DIAMETER_DIMENSION,
        {"chord_point_mm": [10, 5], "far_chord_point_mm": [0, 5], "leader_length_mm": 3},
        "AddDimDiametric",
    ),
    (
        OperationType.CREATE_RADIUS_DIMENSION,
        {"center_mm": [5, 5], "chord_point_mm": [10, 5], "leader_length_mm": 3},
        "AddDimRadial",
    ),
    (
        OperationType.CREATE_HATCH,
        {
            "pattern_type": 0,
            "pattern_name": "ANSI31",
            "associative": True,
            "boundary_refs": ["acad:handle:BOUNDARY"],
        },
        "AddHatch",
    ),
]


@pytest.mark.parametrize(("kind", "geometry", "expected_call"), _CREATE_EXAMPLES)
def test_each_create_operation_has_a_dispatch_handler(
    kind: OperationType, geometry: dict[str, Any], expected_call: str
) -> None:
    adapter = _TestAdapter()
    document = _Document()

    results = adapter._execute(document, "doc-dispatch", _operation(kind, geometry))

    assert expected_call in document.ModelSpace.calls
    assert results
    assert results[0].entity_ref.startswith("acad:handle:")
    assert results[0].measurements != {"measurement_mm": -999.0}


def test_dispatch_table_covers_the_complete_operation_vocabulary() -> None:
    assert set(ComAutoCADAdapter.OPERATION_DISPATCH) == set(OperationType)
    assert len(set(ComAutoCADAdapter.OPERATION_DISPATCH.values())) == len(OperationType)
    assert ComAutoCADAdapter.supported_operations == frozenset(OperationType)
    for handler_name in ComAutoCADAdapter.OPERATION_DISPATCH.values():
        assert callable(getattr(ComAutoCADAdapter, handler_name))


def test_each_adapter_declares_its_operation_support() -> None:
    from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter
    from cad_harness.adapters.dxf_preview import DxfPreviewAdapter
    from cad_harness.adapters.fake import FakeAutoCADAdapter

    declarations = (
        FakeAutoCADAdapter.supported_operations,
        DxfPreviewAdapter.supported_operations,
        ComAutoCADAdapter.supported_operations,
        DotNetBridgeAdapter.supported_operations,
    )
    assert all(isinstance(declaration, frozenset) for declaration in declarations)
    assert FakeAutoCADAdapter.supported_operations == frozenset(OperationType)
    assert DxfPreviewAdapter.renderable_operations.isdisjoint(
        DxfPreviewAdapter.unrenderable_operations
    )
    assert (
        DxfPreviewAdapter.renderable_operations | DxfPreviewAdapter.unrenderable_operations
        == frozenset(OperationType)
    )
    assert DotNetBridgeAdapter.supported_operations == frozenset()


def test_handlers_do_not_consume_business_policy_or_derive_geometry() -> None:
    # Radians/degrees and diameter/radius conversion are representation mappings
    # required by ActiveX. Coordinate derivation and engineering policy are forbidden.
    forbidden = (
        ".expected",
        "tolerance",
        "approval",
        "cad_harness.geometry",
        "layer_map",
        ".layers",
        "_ensure_layer",
        "math.sin",
        "math.cos",
        "math.hypot",
        "math.sqrt",
    )
    for handler_name in ComAutoCADAdapter.OPERATION_DISPATCH.values():
        source = inspect.getsource(getattr(ComAutoCADAdapter, handler_name)).lower()
        assert not any(token in source for token in forbidden)
    assert not hasattr(ComAutoCADAdapter, "_ensure_layer")


def test_missing_entity_mapping_is_an_entity_reference_error() -> None:
    adapter = _TestAdapter()
    operation = Operation(
        operation_id="op-update",
        feature_id="feature",
        type=OperationType.UPDATE_ENTITY,
        layer="OBJECT",
        geometry={"properties": {}},
        target_entity_ref="acad:handle:UNKNOWN",
    )

    with pytest.raises(ComCallFailedError) as captured:
        adapter._execute(_Document(), "doc-dispatch", operation)

    assert captured.value.details["reason"] == "entity_reference_not_found"


class _FakePythonCom(ModuleType):
    IID_IDispatch = "IID_IDispatch"
    MSHCTX_LOCAL = 0
    MSHLFLAGS_NORMAL = 0

    def __init__(self) -> None:
        super().__init__("pythoncom")
        self.initialize_calls = 0
        self.uninitialize_calls = 0
        self.rot: Any = None

    def CoInitialize(self) -> None:  # noqa: N802
        self.initialize_calls += 1

    def CoUninitialize(self) -> None:  # noqa: N802
        self.uninitialize_calls += 1

    def CreateStreamOnHGlobal(self) -> _FakeComStream:  # noqa: N802
        return _FakeComStream()

    def CoMarshalInterface(self, stream: Any, *_args: Any) -> None:  # noqa: N802
        stream.Write(b"marshaled-dispatch")

    def CoUnmarshalInterface(self, stream: Any, _iid: Any) -> object:  # noqa: N802
        stream.Seek(0, 0)
        assert stream.Read(1024) == b"marshaled-dispatch"
        return object()

    def GetRunningObjectTable(self) -> Any:  # noqa: N802
        return self.rot


class _FakeComStream:
    def __init__(self) -> None:
        self.data = b""
        self.position = 0

    def Write(self, value: bytes) -> None:  # noqa: N802
        self.data = value
        self.position = len(value)

    def Seek(self, offset: int, _origin: int) -> None:  # noqa: N802
        self.position = offset

    def Read(self, size: int) -> bytes:  # noqa: N802
        value = self.data[self.position : self.position + size]
        self.position += len(value)
        return value

    def Stat(self) -> tuple[None, None, int]:  # noqa: N802
        return None, None, len(self.data)


class _FakeMonikers:
    def __init__(self) -> None:
        self.returned = False

    def Next(self, _count: int) -> list[str]:  # noqa: N802
        if self.returned:
            return []
        self.returned = True
        return ["acad-moniker"]


class _FakeRot:
    def EnumRunning(self) -> _FakeMonikers:  # noqa: N802
        return _FakeMonikers()

    def GetObject(self, moniker: str) -> object:  # noqa: N802
        assert moniker == "acad-moniker"
        return object()


class _IsolatedApp:
    def __init__(self, hwnd: int) -> None:
        self.HWND = hwnd
        self.Version = "R26.0"
        self._oleobj_ = object()
        self.quit_calls = 0

    def Quit(self) -> None:  # noqa: N802
        self.quit_calls += 1


class _FakeComClient(ModuleType):
    def __init__(self, app: _IsolatedApp) -> None:
        super().__init__("win32com.client")
        self.app = app
        self.dispatch_ids: list[str] = []
        self.dispatch_calls = 0

    def DispatchEx(self, prog_id: str) -> _IsolatedApp:  # noqa: N802
        self.dispatch_ids.append(prog_id)
        return self.app

    def Dispatch(self, _dispatch: Any) -> _IsolatedApp:  # noqa: N802
        self.dispatch_calls += 1
        return self.app


class _FakeWin32Com(ModuleType):
    __path__: list[str]
    client: _FakeComClient


def _install_fake_com_modules(
    monkeypatch: pytest.MonkeyPatch, app: _IsolatedApp
) -> tuple[_FakePythonCom, _FakeComClient]:
    pythoncom = _FakePythonCom()
    client = _FakeComClient(app)
    win32com = _FakeWin32Com("win32com")
    win32com.__path__ = []
    win32com.client = client
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    return pythoncom, client


def test_rot_lookup_and_dispatch_marshalling_succeed_in_owned_apartment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _IsolatedApp(hwnd=4321)
    pythoncom, client = _install_fake_com_modules(monkeypatch, app)
    pythoncom.rot = _FakeRot()
    monkeypatch.setattr(ComAutoCADAdapter, "_pid_from_hwnd", staticmethod(lambda _hwnd: 200))
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_process_identity",
        staticmethod(lambda _pid: (r"D:\CAD\AutoCAD 2027\acad.exe", 1_001)),
    )
    monkeypatch.setattr(autocad_com_module, "_process_is_in_job", lambda pid, _job: pid == 200)

    returned, hwnd = autocad_com_module._find_job_owned_application(
        123,
        0.2,
        expected_pid=200,
        expected_executable=Path(r"D:\CAD\AutoCAD 2027\acad.exe"),
        versioned_prog_id="AutoCAD.Application.26",
    )
    marshaled = autocad_com_module._marshal_dispatch(returned)
    unmarshaled = autocad_com_module._unmarshal_dispatch(marshaled)

    assert returned is app
    assert unmarshaled is app
    assert hwnd == 4321
    assert marshaled == b"marshaled-dispatch"
    assert client.dispatch_calls == 2


def test_registered_server_command_uses_injected_trust_verifier(tmp_path: Path) -> None:
    executable = tmp_path / "acad.exe"
    executable.write_bytes(b"test-only")
    calls: list[tuple[Path, str]] = []

    def verifier(path: Path, prog_id: str) -> Path:
        calls.append((path, prog_id))
        return path

    returned = autocad_com_module._executable_from_local_server_command(
        f'"{executable}" /Automation',
        "AutoCAD.Application.26",
        binary_verifier=verifier,
    )

    assert returned == executable
    assert calls == [(executable, "AutoCAD.Application.26")]


@pytest.mark.parametrize(
    "tail",
    [
        "",
        " /Embedding",
        " /Automation -Embedding",
        " /Automation /b attacker.scr",
    ],
)
def test_registered_server_command_rejects_noncanonical_arguments(
    tail: str,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "acad.exe"
    executable.write_bytes(b"test-only")

    with pytest.raises(OSError, match="executable is invalid"):
        autocad_com_module._executable_from_local_server_command(
            f'"{executable}"{tail}',
            "AutoCAD.Application.26",
            binary_verifier=lambda path, _prog_id: path,
        )


def test_manual_owned_automation_launch_uses_only_fixed_switch() -> None:
    executable = Path(r"D:\CAD\AutoCAD 2027\acad.exe")

    assert autocad_com_module._autocad_owned_automation_command_line(executable) == (
        r'"D:\CAD\AutoCAD 2027\acad.exe" /Automation -Embedding'
    )

    assert (
        autocad_com_module._executable_from_local_server_command(
            r"D:\CAD\AutoCAD 2027\acad.exe /automation",
            "AutoCAD.Application.26",
            binary_verifier=lambda path, _prog_id: path,
        )
        == executable
    )


def test_local_binary_policy_rejects_reparse_traversal(tmp_path: Path) -> None:
    executable = tmp_path / "nested" / "acad.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"test-only")

    with pytest.raises(OSError, match="reparse"):
        autocad_com_module._validate_local_nonreparse_executable(
            executable,
            resolve_path=lambda path: path,
            drive_type=lambda _root: autocad_com_module._DRIVE_FIXED,
            file_attributes=lambda path: (
                autocad_com_module._FILE_ATTRIBUTE_REPARSE_POINT
                if str(path).casefold() == str(executable.parent).casefold()
                else 0
            ),
        )


def test_local_binary_policy_accepts_only_regular_fixed_local_path(tmp_path: Path) -> None:
    executable = tmp_path / "acad.exe"
    executable.write_bytes(b"test-only")

    assert (
        autocad_com_module._validate_local_nonreparse_executable(
            executable,
            resolve_path=lambda path: path,
            drive_type=lambda _root: autocad_com_module._DRIVE_FIXED,
            file_attributes=lambda _path: 0,
        )
        == executable
    )

    with pytest.raises(OSError, match="absolute local"):
        autocad_com_module._validate_local_nonreparse_executable(
            Path(r"\\server\share\acad.exe"),
            resolve_path=lambda path: path,
            drive_type=lambda _root: autocad_com_module._DRIVE_FIXED,
            file_attributes=lambda _path: 0,
        )


def test_production_binary_policy_enforces_signature_product_and_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "acad.exe"
    executable.write_bytes(b"test-only")
    monkeypatch.setattr(
        autocad_com_module,
        "_validate_local_nonreparse_executable",
        lambda path: path,
    )
    evidence = {
        "status": "Valid",
        "signer": 'CN="Autodesk, Inc.", O="Autodesk, Inc.", C=US',
        "timestamp": "CN=Trusted Timestamp",
        "company": "Autodesk, Inc.",
        "product": "AutoCAD",
        "product_version": "R26.0.118.0.0",
        "file_version": "R26.0.118.0.0",
        "original_filename": "ACAD.EXE",
    }

    monkeypatch.setattr(
        autocad_com_module,
        "_read_production_binary_evidence",
        lambda _path: evidence,
    )
    assert (
        autocad_com_module._verify_production_autocad_binary(executable, "AutoCAD.Application.26")
        == executable
    )

    evidence["product_version"] = "R25.0.0.0"
    with pytest.raises(OSError, match="trust policy"):
        autocad_com_module._verify_production_autocad_binary(executable, "AutoCAD.Application.26")


def test_native_trust_probe_imports_fixed_system_module_without_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    powershell = tmp_path / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"test-only")
    captured: dict[str, Any] = {}

    def probe(path: Path, encoded_script: str, *, timeout_seconds: float) -> str:
        captured.update(
            path=path,
            script=base64.b64decode(encoded_script).decode("utf-16le"),
            timeout_seconds=timeout_seconds,
        )
        return json.dumps(
            {
                "status": "Valid",
                "signer": "Autodesk",
                "timestamp": "Timestamp",
                "company": "Autodesk, Inc.",
                "product": "AutoCAD",
                "product_version": "R26.0",
                "file_version": "R26.0",
                "original_filename": "ACAD.EXE",
            }
        )

    monkeypatch.setattr(autocad_com_module, "_windows_directory", lambda: tmp_path)
    monkeypatch.setattr(autocad_com_module, "_is_reparse_path", lambda _path: False)
    monkeypatch.setattr(autocad_com_module, "_run_native_trust_probe", probe)

    evidence = autocad_com_module._read_production_binary_evidence(tmp_path / "acad.exe")

    assert evidence["status"] == "Valid"
    assert captured["path"] == powershell
    assert captured["timeout_seconds"] == autocad_com_module._STARTUP_BINARY_TRUST_SECONDS
    script = cast(str, captured["script"])
    assert "$ProgressPreference='SilentlyContinue'" in script
    assert "Microsoft.PowerShell.Security.psd1" in script
    assert "-Force -ErrorAction Stop" in script


class _FakeRemoteOwner:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def test_close_owned_session_fails_fast_after_terminal_remote_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _IsolatedApp(hwnd=4321)
    _install_fake_com_modules(monkeypatch, app)
    owner = _FakeRemoteOwner()
    adapter = ComAutoCADAdapter(startup_wait_seconds=5.0)
    adapter._app = app
    adapter._owned_session = OwnedComSession(
        prog_id="AutoCAD.Application.26",
        hwnd=4321,
        pid=200,
        image_path=r"D:\CAD\AutoCAD 2027\acad.exe",
        creation_time_100ns=1_001,
    )
    adapter._remote_com_owner = cast(Any, owner)
    adapter._com_owner_thread_id = threading.get_ident()
    monkeypatch.setattr(adapter, "_require_current_owned_application", lambda: app)
    monkeypatch.setattr(
        adapter,
        "_wait_until_quiescent",
        lambda **_kwargs: (_ for _ in ()).throw(
            ComCallFailedError(
                "terminal",
                details={"reason": "isolated_com_owner_closed"},
            )
        ),
    )

    started = time.monotonic()
    with pytest.raises(ComCallFailedError, match="terminal"):
        adapter.close_owned_session()

    assert time.monotonic() - started < 0.5
    assert owner.shutdown_calls == 1
    assert adapter.owned_session is None


def test_isolated_connect_proves_new_exact_acad_process_before_exposing_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _IsolatedApp(hwnd=4321)
    pythoncom, client = _install_fake_com_modules(monkeypatch, app)
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_acad_process_ids",
        staticmethod(lambda: {100}),
    )
    monkeypatch.setattr(ComAutoCADAdapter, "_system_filetime_100ns", staticmethod(lambda: 1_000))
    session = OwnedComSession(
        prog_id="AutoCAD.Application.26",
        hwnd=4321,
        pid=200,
        image_path=r"D:\CAD\acad.exe",
        creation_time_100ns=1_001,
    )
    boundary_calls: list[dict[str, Any]] = []

    remote_owner: Any = _FakeRemoteOwner()

    def boundary(**kwargs: Any) -> Any:
        boundary_calls.append(kwargs)
        return app, session, remote_owner

    monkeypatch.setattr(autocad_com_module, "_run_isolated_startup_boundary", boundary)
    adapter = ComAutoCADAdapter()

    returned = adapter.connect_isolated(versioned_prog_id="AutoCAD.Application.26")

    assert returned is session
    assert returned.owned is True
    assert adapter.owned_session is returned
    assert adapter.require_owned_application() is app
    assert adapter._document is None
    assert client.dispatch_ids == []
    assert boundary_calls[0]["preexisting_pids"] == {100}
    assert boundary_calls[0]["dispatch_started_100ns"] == 1_000
    assert pythoncom.initialize_calls == 0

    adapter.disconnect()
    assert app.quit_calls == 0
    assert pythoncom.uninitialize_calls == 0
    assert remote_owner.shutdown_calls == 1
    assert adapter.owned_session is None


@pytest.mark.parametrize(
    ("preexisting", "current"),
    [({200}, {200}), ({100}, {100})],
    ids=["returned-pid-was-preexisting", "returned-pid-is-not-acad-exe"],
)
def test_isolated_connect_fails_closed_when_process_ownership_is_unproven(
    monkeypatch: pytest.MonkeyPatch,
    preexisting: set[int],
    current: set[int],
) -> None:
    app = _IsolatedApp(hwnd=4321)
    pythoncom, _ = _install_fake_com_modules(monkeypatch, app)
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_acad_process_ids",
        staticmethod(lambda: set(preexisting)),
    )
    monkeypatch.setattr(ComAutoCADAdapter, "_system_filetime_100ns", staticmethod(lambda: 1_000))

    def boundary(**_kwargs: Any) -> tuple[Any, OwnedComSession]:
        assert current in ({200}, {100})
        raise ComCallFailedError(
            "Could not prove ownership of the isolated AutoCAD process",
            details={"reason": "isolated_process_ownership_unproven"},
        )

    monkeypatch.setattr(autocad_com_module, "_run_isolated_startup_boundary", boundary)
    adapter = ComAutoCADAdapter()

    with pytest.raises(ComCallFailedError) as captured:
        adapter.connect_isolated(versioned_prog_id="AutoCAD.Application.26")

    assert captured.value.details["reason"] == "isolated_process_ownership_unproven"
    assert adapter.owned_session is None
    assert adapter._app is None
    assert app.quit_calls == 0
    assert pythoncom.initialize_calls == 0
    assert pythoncom.uninitialize_calls == 0


def test_local_com_disconnect_rejects_non_owner_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _IsolatedApp(hwnd=4321)
    pythoncom, _ = _install_fake_com_modules(monkeypatch, app)
    adapter = ComAutoCADAdapter()
    adapter._app = app
    adapter._com_owner_thread_id = threading.get_ident()
    failures: list[ComCallFailedError] = []

    def disconnect_elsewhere() -> None:
        try:
            adapter.disconnect()
        except ComCallFailedError as exc:
            failures.append(exc)

    thread = threading.Thread(target=disconnect_elsewhere)
    thread.start()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert failures[0].details["reason"] == "com_apartment_ownership_mismatch"
    assert adapter._app is app
    assert pythoncom.uninitialize_calls == 0

    adapter.disconnect()
    assert pythoncom.uninitialize_calls == 1


class _FakeStartupJob:
    name = "fake-startup-job"

    def __init__(self) -> None:
        self.assigned_pids: list[int] = []
        self.member_pids: set[int] = set()
        self.terminate_calls: list[float] = []
        self.disarmed = False
        self.closed = False
        self.empty = True

    def assign_pid(self, pid: int) -> None:
        self.assigned_pids.append(pid)
        self.member_pids.add(pid)

    def contains_pid(self, pid: int) -> bool:
        return pid in self.member_pids

    def terminate_and_wait(self, timeout_seconds: float) -> bool:
        self.terminate_calls.append(timeout_seconds)
        return True

    def wait_until_empty(self, _timeout_seconds: float) -> bool:
        return self.empty

    def disarm(self) -> None:
        self.disarmed = True

    def close(self) -> None:
        self.closed = True


def _blocking_startup_worker(
    connection: Any,
    marker_path: str,
    _job_name: str,
    timeout_seconds: float,
) -> None:
    connection.send_bytes(autocad_com_module._startup_message({"stage": "helper_ready"}))
    if not connection.poll(timeout_seconds):
        connection.close()
        return
    assert autocad_com_module._read_startup_message(connection) == {"stage": "job_assigned"}
    connection.send_bytes(
        autocad_com_module._startup_message(
            {"stage": "startup_progress", "failure_stage": "rot_discovery"}
        )
    )
    time.sleep(timeout_seconds + 0.5)
    Path(marker_path).write_text("late side effect", encoding="utf-8")


def _successful_remote_startup_worker(
    connection: Any,
    versioned_prog_id: str,
    _job_name: str,
    timeout_seconds: float,
) -> None:
    connection.send_bytes(autocad_com_module._startup_message({"stage": "helper_ready"}))
    if not connection.poll(timeout_seconds):
        return
    if autocad_com_module._read_startup_message(connection) != {"stage": "job_assigned"}:
        return
    connection.send_bytes(
        autocad_com_module._startup_message(
            {
                "stage": "ready",
                "marshal_size": len(b"marshaled-dispatch"),
                "app_object_id": 1,
                "session": {
                    "prog_id": versioned_prog_id,
                    "hwnd": 4321,
                    "pid": 200,
                    "image_path": r"D:\CAD\AutoCAD 2027\acad.exe",
                    "creation_time_100ns": 1_001,
                },
            }
        )
    )
    if not connection.poll(timeout_seconds):
        return
    if autocad_com_module._read_startup_message(connection) != {"stage": "adopted"}:
        return
    connection.send_bytes(autocad_com_module._startup_message({"stage": "owner_ready"}))
    while True:
        request = autocad_com_module._read_startup_message(connection)
        if request == {"op": "shutdown"}:
            connection.send_bytes(autocad_com_module._startup_message({"stage": "stopped"}))
            return
        if request == {"op": "getattr", "object_id": 1, "name": "HWND"}:
            connection.send_bytes(
                autocad_com_module._startup_message({"stage": "result", "value": 4321})
            )
            continue
        connection.send_bytes(autocad_com_module._startup_message({"stage": "rpc_error"}))


def _blocking_hwnd_startup_worker(
    connection: Any,
    marker_path: str,
    _job_name: str,
    timeout_seconds: float,
) -> None:
    connection.send_bytes(autocad_com_module._startup_message({"stage": "helper_ready"}))
    if not connection.poll(timeout_seconds):
        return
    autocad_com_module._read_startup_message(connection)
    connection.send_bytes(
        autocad_com_module._startup_message(
            {
                "stage": "ready",
                "marshal_size": 8,
                "app_object_id": 1,
                "session": {
                    "prog_id": marker_path,
                    "hwnd": 4321,
                    "pid": 200,
                    "image_path": r"D:\CAD\AutoCAD 2027\acad.exe",
                    "creation_time_100ns": 1_001,
                },
            }
        )
    )
    if not connection.poll(timeout_seconds):
        return
    autocad_com_module._read_startup_message(connection)
    connection.send_bytes(autocad_com_module._startup_message({"stage": "owner_ready"}))
    if connection.poll(timeout_seconds):
        request = autocad_com_module._read_startup_message(connection)
        if request == {"op": "shutdown"}:
            connection.send_bytes(autocad_com_module._startup_message({"stage": "stopped"}))
            return
        time.sleep(timeout_seconds + 0.5)
        Path(marker_path).write_text("late hwnd", encoding="utf-8")


def _error_startup_worker(
    connection: Any,
    _versioned_prog_id: str,
    _job_name: str,
    timeout_seconds: float,
) -> None:
    connection.send_bytes(autocad_com_module._startup_message({"stage": "helper_ready"}))
    if connection.poll(timeout_seconds):
        autocad_com_module._read_startup_message(connection)
        connection.send_bytes(
            autocad_com_module._startup_message(
                {"stage": "error", "failure_stage": "rot_discovery"}
            )
        )


def test_startup_helper_reports_only_bounded_failure_stage() -> None:
    job = _FakeStartupJob()

    with pytest.raises(ComCallFailedError) as captured:
        autocad_com_module._run_isolated_startup_boundary(
            versioned_prog_id="AutoCAD.Application.26",
            timeout_seconds=1.5,
            preexisting_pids={40688},
            dispatch_started_100ns=1_000,
            current_pids=lambda: {40688},
            pid_from_hwnd=lambda _hwnd: 0,
            process_identity=lambda _pid: ("", 0),
            worker_target=_error_startup_worker,
            job_factory=lambda: job,
        )

    assert captured.value.details == {
        "reason": "isolated_startup_failed",
        "failure_stage": "rot_discovery",
    }
    assert job.terminate_calls
    assert job.closed is True


def _post_startup_rpc_timeout_worker(
    connection: Any,
    marker_path: str,
    _job_name: str,
    timeout_seconds: float,
) -> None:
    connection.send_bytes(autocad_com_module._startup_message({"stage": "helper_ready"}))
    if not connection.poll(timeout_seconds):
        return
    if autocad_com_module._read_startup_message(connection) != {"stage": "job_assigned"}:
        return
    connection.send_bytes(
        autocad_com_module._startup_message(
            {
                "stage": "ready",
                "marshal_size": 8,
                "app_object_id": 1,
                "session": {
                    "prog_id": marker_path,
                    "hwnd": 4321,
                    "pid": 200,
                    "image_path": r"D:\CAD\AutoCAD 2027\acad.exe",
                    "creation_time_100ns": 1_001,
                },
            }
        )
    )
    if not connection.poll(timeout_seconds):
        return
    autocad_com_module._read_startup_message(connection)
    connection.send_bytes(autocad_com_module._startup_message({"stage": "owner_ready"}))
    while True:
        request = autocad_com_module._read_startup_message(connection)
        if request == {"op": "getattr", "object_id": 1, "name": "HWND"}:
            connection.send_bytes(
                autocad_com_module._startup_message({"stage": "result", "value": 4321})
            )
            continue
        if request == {"op": "getattr", "object_id": 1, "name": "Version"}:
            time.sleep(timeout_seconds + 0.5)
            Path(marker_path).write_text("late rpc effect", encoding="utf-8")
            return
        return


def _graceful_remote_worker(
    connection: Any,
    versioned_prog_id: str,
    _job_name: str,
    timeout_seconds: float,
) -> None:
    connection.send_bytes(autocad_com_module._startup_message({"stage": "helper_ready"}))
    if not connection.poll(timeout_seconds):
        return
    if autocad_com_module._read_startup_message(connection) != {"stage": "job_assigned"}:
        return
    connection.send_bytes(
        autocad_com_module._startup_message(
            {
                "stage": "ready",
                "marshal_size": 8,
                "app_object_id": 1,
                "session": {
                    "prog_id": versioned_prog_id,
                    "hwnd": 4321,
                    "pid": 200,
                    "image_path": r"D:\CAD\AutoCAD 2027\acad.exe",
                    "creation_time_100ns": 1_001,
                },
            }
        )
    )
    if not connection.poll(timeout_seconds):
        return
    autocad_com_module._read_startup_message(connection)
    connection.send_bytes(autocad_com_module._startup_message({"stage": "owner_ready"}))
    while True:
        request = autocad_com_module._read_startup_message(connection)
        if request == {"op": "getattr", "object_id": 1, "name": "HWND"}:
            connection.send_bytes(
                autocad_com_module._startup_message({"stage": "result", "value": 4321})
            )
        elif request == {
            "op": "invoke",
            "object_id": 1,
            "name": "Quit",
            "args": [],
            "kwargs": {},
        }:
            connection.send_bytes(
                autocad_com_module._startup_message({"stage": "result", "value": None})
            )
        elif request == {"op": "shutdown"}:
            connection.send_bytes(autocad_com_module._startup_message({"stage": "stopped"}))
            return
        else:
            connection.send_bytes(autocad_com_module._startup_message({"stage": "rpc_error"}))


def test_isolated_startup_success_keeps_com_in_remote_owned_process() -> None:
    job = _FakeStartupJob()

    app, session, owner = autocad_com_module._run_isolated_startup_boundary(
        versioned_prog_id="AutoCAD.Application.26",
        timeout_seconds=2.0,
        preexisting_pids={40688},
        dispatch_started_100ns=1_000,
        current_pids=lambda: {40688, 200},
        pid_from_hwnd=lambda hwnd: 200 if hwnd == 4321 else 0,
        process_identity=lambda pid: (
            (r"D:\CAD\AutoCAD 2027\acad.exe", 1_001) if pid == 200 else ("", 0)
        ),
        worker_target=_successful_remote_startup_worker,
        job_factory=lambda: job,
    )

    assert int(app.HWND) == 4321
    assert session.pid == 200
    assert job.disarmed is False
    assert job.closed is False
    failures: list[ComCallFailedError] = []

    def access_elsewhere() -> None:
        try:
            _ = app.HWND
        except ComCallFailedError as exc:
            failures.append(exc)

    thread = threading.Thread(target=access_elsewhere)
    thread.start()
    thread.join(timeout=1.0)
    assert failures[0].details["reason"] == "com_apartment_ownership_mismatch"
    owner.shutdown()
    assert job.terminate_calls
    assert job.disarmed is False
    assert job.closed is True


def test_isolated_startup_timeout_is_terminal_and_has_no_late_process_side_effect(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "late-marker.txt"
    job = _FakeStartupJob()
    started = time.monotonic()

    with pytest.raises(ComCallFailedError) as captured:
        autocad_com_module._run_isolated_startup_boundary(
            versioned_prog_id=str(marker),
            timeout_seconds=1.5,
            preexisting_pids={40688},
            dispatch_started_100ns=1_000,
            current_pids=lambda: {40688},
            pid_from_hwnd=lambda _hwnd: 0,
            process_identity=lambda _pid: ("", 0),
            worker_target=_blocking_startup_worker,
            job_factory=lambda: job,
        )

    elapsed = time.monotonic() - started
    assert captured.value.details["reason"] == "isolated_startup_timeout"
    assert captured.value.details["failure_stage"] == "rot_discovery"
    assert elapsed < 1.65
    assert len(job.assigned_pids) == 1
    assert job.terminate_calls
    assert job.disarmed is False
    assert job.closed is True
    time.sleep(0.75)
    assert not marker.exists()


def test_isolated_startup_does_not_repeat_the_verified_hwnd_com_call(tmp_path: Path) -> None:
    marker = tmp_path / "late-hwnd-marker.txt"
    job = _FakeStartupJob()

    _app, session, owner = autocad_com_module._run_isolated_startup_boundary(
        versioned_prog_id=str(marker),
        timeout_seconds=1.5,
        preexisting_pids=set(),
        dispatch_started_100ns=1_000,
        current_pids=lambda: {200},
        pid_from_hwnd=lambda _hwnd: 200,
        process_identity=lambda _pid: (r"D:\CAD\AutoCAD 2027\acad.exe", 1_001),
        worker_target=_blocking_hwnd_startup_worker,
        job_factory=lambda: job,
    )

    assert session.pid == 200
    owner.shutdown()
    assert job.closed is True
    time.sleep(0.75)
    assert not marker.exists()


def test_helper_cleanup_attempts_terminate_kill_and_join_after_exceptions() -> None:
    class _ExplodingProcess:
        pid: int | None = 1
        exitcode: int | None = None

        def __init__(self) -> None:
            self.calls: list[str] = []

        def start(self) -> None:
            self.calls.append("start")

        def is_alive(self) -> bool:
            self.calls.append("is_alive")
            return True

        def terminate(self) -> None:
            self.calls.append("terminate")
            raise OSError("terminate failed")

        def join(self, _timeout: float | None = None) -> None:
            self.calls.append("join")
            raise OSError("join failed")

        def kill(self) -> None:
            self.calls.append("kill")
            raise OSError("kill failed")

    process = _ExplodingProcess()

    assert autocad_com_module._terminate_startup_helper(process, 0.01) is False
    assert "terminate" in process.calls
    assert "kill" in process.calls
    assert process.calls.count("join") == 2


def test_startup_job_close_runs_when_every_cleanup_step_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingJob(_FakeStartupJob):
        def terminate_and_wait(self, timeout_seconds: float) -> bool:
            self.terminate_calls.append(timeout_seconds)
            raise RuntimeError("job terminate failed")

    job = _ExplodingJob()

    def exploding_helper_cleanup(process: Any, _timeout_seconds: float) -> bool:
        process.join(1.0)
        raise RuntimeError("helper terminate/kill/join failed")

    monkeypatch.setattr(
        autocad_com_module,
        "_terminate_startup_helper",
        exploding_helper_cleanup,
    )

    with pytest.raises(RuntimeError, match="helper terminate/kill/join failed"):
        autocad_com_module._run_isolated_startup_boundary(
            versioned_prog_id="AutoCAD.Application.26",
            timeout_seconds=1.5,
            preexisting_pids=set(),
            dispatch_started_100ns=1_000,
            current_pids=set,
            pid_from_hwnd=lambda _hwnd: 0,
            process_identity=lambda _pid: ("", 0),
            worker_target=_error_startup_worker,
            job_factory=lambda: job,
        )

    assert job.terminate_calls
    assert job.closed is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects are Windows-only")
def test_startup_job_terminates_only_its_assigned_blocking_process(tmp_path: Path) -> None:
    marker = tmp_path / "job-late-marker.txt"
    unowned_process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(5.0)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,sys,time;time.sleep(1.0);"
                "pathlib.Path(sys.argv[1]).write_text('late',encoding='utf-8')"
            ),
            str(marker),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    job = autocad_com_module._WindowsStartupJob.create()
    try:
        job.assign_pid(process.pid)
        assert job.terminate_and_wait(0.5) is True
        process.wait(timeout=0.5)
        assert unowned_process.poll() is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=0.5)
        job.close()
        if unowned_process.poll() is None:
            unowned_process.kill()
            unowned_process.wait(timeout=0.5)
    time.sleep(1.1)
    assert not marker.exists()


def test_isolated_connect_rejects_unversioned_progid_before_com_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _IsolatedApp(hwnd=4321)
    pythoncom, client = _install_fake_com_modules(monkeypatch, app)

    with pytest.raises(ValueError, match="versioned_prog_id"):
        ComAutoCADAdapter().connect_isolated(versioned_prog_id="AutoCAD.Application")

    assert client.dispatch_ids == []
    assert pythoncom.initialize_calls == 0


def test_post_startup_rpc_timeout_kills_armed_job_and_has_no_late_effect(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "late-rpc-marker.txt"
    job = _FakeStartupJob()
    app, _session, _owner = autocad_com_module._run_isolated_startup_boundary(
        versioned_prog_id=str(marker),
        timeout_seconds=1.2,
        preexisting_pids=set(),
        dispatch_started_100ns=1_000,
        current_pids=lambda: {200},
        pid_from_hwnd=lambda _hwnd: 200,
        process_identity=lambda _pid: (r"D:\CAD\AutoCAD 2027\acad.exe", 1_001),
        worker_target=_post_startup_rpc_timeout_worker,
        job_factory=lambda: job,
    )

    assert job.disarmed is False
    with pytest.raises(ComCallFailedError) as captured:
        _ = app.Version

    assert captured.value.details == {
        "reason": "isolated_com_call_timeout",
        "helper_terminal": True,
        "job_terminal": True,
    }
    assert job.terminate_calls
    assert job.closed is True
    time.sleep(0.7)
    assert not marker.exists()


def test_graceful_quit_disarms_only_after_helper_and_autocad_exit() -> None:
    job = _FakeStartupJob()
    app, _session, owner = autocad_com_module._run_isolated_startup_boundary(
        versioned_prog_id="AutoCAD.Application.26",
        timeout_seconds=2.0,
        preexisting_pids=set(),
        dispatch_started_100ns=1_000,
        current_pids=lambda: {200},
        pid_from_hwnd=lambda _hwnd: 200,
        process_identity=lambda _pid: (r"D:\CAD\AutoCAD 2027\acad.exe", 1_001),
        worker_target=_graceful_remote_worker,
        job_factory=lambda: job,
    )

    app.Quit()
    was_disarmed_before_exit = job.disarmed
    assert was_disarmed_before_exit is False
    owner.shutdown_after_verified_quit()

    assert job.disarmed is True
    assert job.closed is True
    assert job.terminate_calls == []


def test_unverified_graceful_exit_stays_armed_and_is_force_terminated() -> None:
    job = _FakeStartupJob()
    job.empty = False
    app, _session, owner = autocad_com_module._run_isolated_startup_boundary(
        versioned_prog_id="AutoCAD.Application.26",
        timeout_seconds=2.0,
        preexisting_pids=set(),
        dispatch_started_100ns=1_000,
        current_pids=lambda: {200},
        pid_from_hwnd=lambda _hwnd: 200,
        process_identity=lambda _pid: (r"D:\CAD\AutoCAD 2027\acad.exe", 1_001),
        worker_target=_graceful_remote_worker,
        job_factory=lambda: job,
    )

    app.Quit()
    with pytest.raises(ComCallFailedError) as captured:
        owner.shutdown_after_verified_quit()

    assert captured.value.details["reason"] == "owned_session_graceful_exit_unconfirmed"
    assert job.disarmed is False
    assert job.terminate_calls
    assert job.closed is True


def test_rot_rejects_first_wrong_job_owned_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ManyMonikers:
        def __init__(self) -> None:
            self._values = iter(["wrong", "expected"])

        def Next(self, _count: int) -> list[str]:  # noqa: N802
            try:
                return [next(self._values)]
            except StopIteration:
                return []

    wrong = _IsolatedApp(hwnd=111)
    expected = _IsolatedApp(hwnd=222)

    class _ManyRot:
        def EnumRunning(self) -> _ManyMonikers:  # noqa: N802
            return _ManyMonikers()

        def GetObject(self, moniker: str) -> _IsolatedApp:  # noqa: N802
            return wrong if moniker == "wrong" else expected

    pythoncom, client = _install_fake_com_modules(monkeypatch, expected)
    pythoncom.rot = _ManyRot()
    monkeypatch.setattr(client, "Dispatch", lambda value: value)
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_pid_from_hwnd",
        staticmethod(lambda hwnd: 201 if hwnd == 111 else 200),
    )
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_process_identity",
        staticmethod(lambda _pid: (r"D:\CAD\AutoCAD 2027\acad.exe", 1_001)),
    )
    monkeypatch.setattr(autocad_com_module, "_process_is_in_job", lambda _pid, _job: True)

    with pytest.raises(OSError, match="unexpected AutoCAD ROT process"):
        autocad_com_module._find_job_owned_application(
            123,
            0.2,
            expected_pid=200,
            expected_executable=Path(r"D:\CAD\AutoCAD 2027\acad.exe"),
            versioned_prog_id="AutoCAD.Application.26",
        )


@pytest.mark.parametrize(
    ("image", "version", "message"),
    [
        (r"D:\Other\acad.exe", "R26.0", "image changed"),
        (r"D:\CAD\AutoCAD 2027\acad.exe", "R25.0", "release does not match"),
    ],
)
def test_rot_requires_exact_created_image_and_requested_release(
    monkeypatch: pytest.MonkeyPatch,
    image: str,
    version: str,
    message: str,
) -> None:
    app = _IsolatedApp(hwnd=4321)
    app.Version = version
    pythoncom, _client = _install_fake_com_modules(monkeypatch, app)
    pythoncom.rot = _FakeRot()
    monkeypatch.setattr(ComAutoCADAdapter, "_pid_from_hwnd", staticmethod(lambda _hwnd: 200))
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_process_identity",
        staticmethod(lambda _pid: (image, 1_001)),
    )
    monkeypatch.setattr(autocad_com_module, "_process_is_in_job", lambda _pid, _job: True)

    with pytest.raises(OSError, match=message):
        autocad_com_module._find_job_owned_application(
            123,
            0.2,
            expected_pid=200,
            expected_executable=Path(r"D:\CAD\AutoCAD 2027\acad.exe"),
            versioned_prog_id="AutoCAD.Application.26",
        )


class _RecordingRpcOwner:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request(self, payload: dict[str, Any]) -> Any:
        self.requests.append(payload)
        return None

    def acceptance_netload(self, object_id: int, command: str) -> None:
        if not autocad_com_module._acceptance_netload_request_is_fixed(command):
            raise ComCallFailedError("NETLOAD rejected")
        self.request({"op": "acceptance_netload", "object_id": object_id})


def test_parent_proxy_rejects_unknown_members_before_rpc() -> None:
    owner = _RecordingRpcOwner()
    app = autocad_com_module._ComRemoteObject(
        cast(Any, owner), 1, autocad_com_module._SCOPE_APPLICATION
    )

    with pytest.raises(ComCallFailedError) as captured:
        _ = app.SendCommand

    assert captured.value.details["reason"] == "isolated_com_member_not_allowed"
    assert owner.requests == []


def test_owned_document_save_is_a_zero_argument_rpc() -> None:
    owner = _RecordingRpcOwner()
    document = autocad_com_module._ComRemoteObject(
        cast(Any, owner), 9, autocad_com_module._SCOPE_DOCUMENT
    )

    document.Save()

    assert owner.requests == [
        {
            "op": "invoke",
            "object_id": 9,
            "name": "Save",
            "args": [],
            "kwargs": {},
        }
    ]
    autocad_com_module._validate_worker_invocation(
        autocad_com_module._SCOPE_DOCUMENT,
        "Save",
        [],
        {},
    )
    with pytest.raises(ValueError, match="arity"):
        autocad_com_module._validate_worker_invocation(
            autocad_com_module._SCOPE_DOCUMENT,
            "Save",
            ["unexpected"],
            {},
        )


def test_acceptance_netload_rpc_contains_no_command_or_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _RecordingRpcOwner()
    document = autocad_com_module._ComRemoteObject(
        cast(Any, owner), 9, autocad_com_module._SCOPE_DOCUMENT
    )
    monkeypatch.setattr(
        autocad_com_module,
        "_acceptance_netload_request_is_fixed",
        lambda command: command == "fixed-runner-command",
    )

    document.SendCommand("fixed-runner-command")

    assert owner.requests == [{"op": "acceptance_netload", "object_id": 9}]


class _ScriptedRpcConnection:
    def __init__(self, requests: list[dict[str, Any]]) -> None:
        self.requests = [autocad_com_module._startup_message(item) for item in requests]
        self.responses: list[dict[str, Any]] = []

    def recv_bytes(self, _max_bytes: int) -> bytes:
        if not self.requests:
            raise EOFError
        return self.requests.pop(0)

    def send_bytes(self, value: bytes) -> None:
        self.responses.append(json.loads(value.decode("ascii")))


def test_worker_acceptance_netload_reconstructs_one_fixed_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DocumentWithCommand:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def SendCommand(self, command: str) -> None:  # noqa: N802
            self.commands.append(command)

    document = _DocumentWithCommand()

    class _AppWithDocument:
        ActiveDocument = document

    connection = _ScriptedRpcConnection(
        [
            {"op": "getattr", "object_id": 1, "name": "ActiveDocument"},
            {"op": "acceptance_netload", "object_id": 2},
            {"op": "shutdown"},
        ]
    )
    monkeypatch.setattr(
        autocad_com_module,
        "_expected_acceptance_netload_command",
        lambda: "worker-fixed-netload",
    )
    load_events: list[str] = []
    monkeypatch.setattr(
        autocad_com_module,
        "_assert_acceptance_bridge_absent_before_netload",
        lambda: load_events.append("absent"),
    )

    def prove_owned_pipe(pid: int, timeout: float) -> dict[str, Any]:
        assert document.commands == ["worker-fixed-netload"]
        assert pid == 200
        assert timeout == 2.0
        load_events.append("owned-pipe")
        return {"bridge_loaded": True, "server_pid": pid}

    monkeypatch.setattr(
        autocad_com_module,
        "_wait_for_owned_acceptance_bridge",
        prove_owned_pipe,
    )

    autocad_com_module._serve_isolated_com_apartment(
        cast(Any, connection),
        _AppWithDocument(),
        owned_autocad_pid=200,
        netload_timeout_seconds=2.0,
    )

    assert document.commands == ["worker-fixed-netload"]
    assert load_events == ["absent", "owned-pipe"]
    assert connection.responses[0]["value"] == {
        "$remote_object": 2,
        "scope": "document",
    }
    assert connection.responses[1] == {
        "stage": "result",
        "value": {"bridge_loaded": True, "server_pid": 200},
    }


def test_netload_provenance_rejects_a_pipe_that_existed_before_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(autocad_com_module, "_acceptance_bridge_server_pid", lambda: 200)

    with pytest.raises(OSError, match="existed before NETLOAD"):
        autocad_com_module._assert_acceptance_bridge_absent_before_netload()


def test_netload_completion_rejects_pipe_from_another_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(autocad_com_module, "_acceptance_bridge_server_pid", lambda: 40688)

    with pytest.raises(OSError, match="belongs to another process"):
        autocad_com_module._wait_for_owned_acceptance_bridge(200, 1.0)


def test_worker_rejects_member_outside_scope_without_touching_target() -> None:
    class _FailOnDiscovery:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"worker attempted discovery: {name}")

    connection = _ScriptedRpcConnection(
        [
            {"op": "getattr", "object_id": 1, "name": "Shell"},
            {"op": "shutdown"},
        ]
    )

    autocad_com_module._serve_isolated_com_apartment(cast(Any, connection), _FailOnDiscovery())

    assert connection.responses == [{"stage": "rpc_error"}]


@pytest.mark.parametrize(
    "payload",
    [
        {"value": "x" * (autocad_com_module._RPC_MAX_STRING_BYTES + 1)},
        {"value": [0] * autocad_com_module._RPC_MAX_ITEMS},
    ],
    ids=["oversized-string", "oversized-item-tree"],
)
def test_outbound_rpc_tree_limits_fail_closed(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        autocad_com_module._startup_message(payload)


def test_rpc_depth_limit_is_enforced_both_directions() -> None:
    nested: Any = None
    for _ in range(autocad_com_module._RPC_MAX_DEPTH + 1):
        nested = [nested]

    with pytest.raises(ValueError, match="too deep"):
        autocad_com_module._startup_message({"value": nested})

    raw = json.dumps({"value": nested}, separators=(",", ":")).encode("ascii")
    connection = _ScriptedRpcConnection([])
    connection.requests = [raw]
    with pytest.raises(ComCallFailedError) as captured:
        autocad_com_module._read_startup_message(connection)
    assert captured.value.details["reason"] == "isolated_startup_invalid_response"


def test_inbound_rpc_byte_limit_fails_closed() -> None:
    connection = _ScriptedRpcConnection([])
    connection.requests = [b" " * (autocad_com_module._RPC_MAX_BYTES_VALUE + 1)]

    with pytest.raises(ComCallFailedError) as captured:
        autocad_com_module._read_startup_message(connection)

    assert captured.value.details["reason"] == "isolated_startup_invalid_response"


def test_worker_registry_20k_linear_releases_return_to_root_baseline() -> None:
    registry = autocad_com_module._WorkerObjectRegistry(object(), object_cap=2)

    for index in range(20_000):
        metadata = registry.register(object(), autocad_com_module._SCOPE_ENTITY)
        assert metadata["$remote_object"] == index + 2
        registry.release(index + 2)
        assert registry.size == 1

    registry.clear()
    assert registry.size == 0


def test_worker_registry_cap_is_fail_closed_and_ids_are_not_reused() -> None:
    registry = autocad_com_module._WorkerObjectRegistry(object(), object_cap=2)
    first = registry.register(object(), autocad_com_module._SCOPE_ENTITY)
    with pytest.raises(ValueError, match="cap reached"):
        registry.register(object(), autocad_com_module._SCOPE_ENTITY)
    registry.release(first["$remote_object"])
    second = registry.register(object(), autocad_com_module._SCOPE_ENTITY)

    assert second["$remote_object"] > first["$remote_object"]


def _write_installed_development_bundle(plugins_root: Path) -> Path:
    plugins_root.mkdir(parents=True, exist_ok=True)
    (plugins_root / ".cad-harness-installer.lock").write_bytes(b"")
    bundle = plugins_root / "AutoCADHarness.bundle"
    bridge = bundle / "Contents" / "Windows" / "AutoCADHarness.dll"
    bridge.parent.mkdir(parents=True)
    bridge.write_bytes(b"development-bridge")
    relative = "Contents/Windows/AutoCADHarness.dll"
    bridge_hash = hashlib.sha256(bridge.read_bytes()).hexdigest()
    checksum = bundle / "SHA256SUMS.ps1"
    checksum.write_text(f"# SHA256 {bridge_hash} *{relative}\n", encoding="utf-8")
    receipt = {
        "SchemaVersion": "2.0",
        "Owner": "autocad-mechanical-harness",
        "BundleName": "AutoCADHarness.bundle",
        "ArtifactKind": "DEVELOPMENT-UNSIGNED",
        "AutoCADSeries": "R26.0",
        "AppVersion": "1.0.0",
        "ProductCode": "test",
        "UpgradeCode": "test",
        "ChecksumManifestSha256": hashlib.sha256(checksum.read_bytes()).hexdigest(),
        "SignerId": "",
        "Files": [{"RelativePath": relative, "Sha256": bridge_hash}],
        "Directories": ["Contents", "Contents/Windows"],
    }
    (bundle / "CAD-HARNESS-INSTALL-RECEIPT.json").write_text(
        json.dumps(receipt, separators=(",", ":")), encoding="utf-8"
    )
    return bundle


def test_acceptance_bundle_env_requires_installer_receipt_and_checksums(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugins_root = tmp_path / "data" / "live-r26" / "ApplicationPlugins"
    bundle = _write_installed_development_bundle(plugins_root)
    monkeypatch.setattr(
        autocad_com_module,
        "_workspace_acceptance_plugins_root",
        lambda: plugins_root.resolve(strict=True),
    )
    monkeypatch.setenv("CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT", str(bundle))

    bridge = autocad_com_module._acceptance_bridge_path()
    assert bridge == (bundle / "Contents" / "Windows" / "AutoCADHarness.dll").resolve()

    bridge.write_bytes(b"tampered")
    with pytest.raises(OSError, match="checksum verification failed"):
        autocad_com_module._acceptance_bridge_path()


@pytest.mark.parametrize("extra_kind", ["file", "directory"])
def test_acceptance_bundle_rejects_unlisted_filesystem_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra_kind: str,
) -> None:
    plugins_root = tmp_path / "data" / "live-r26" / "ApplicationPlugins"
    bundle = _write_installed_development_bundle(plugins_root)
    monkeypatch.setattr(
        autocad_com_module,
        "_workspace_acceptance_plugins_root",
        lambda: plugins_root.resolve(strict=True),
    )
    monkeypatch.setenv("CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT", str(bundle))
    extra = bundle / "Contents" / "Windows" / "unlisted"
    if extra_kind == "file":
        extra.with_suffix(".dll").write_bytes(b"not-receipt-bound")
    else:
        extra.mkdir()

    with pytest.raises(OSError, match="filesystem inventory does not match"):
        autocad_com_module._acceptance_bridge_path()


def test_acceptance_bundle_rejects_duplicate_receipt_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugins_root = tmp_path / "data" / "live-r26" / "ApplicationPlugins"
    bundle = _write_installed_development_bundle(plugins_root)
    monkeypatch.setattr(
        autocad_com_module,
        "_workspace_acceptance_plugins_root",
        lambda: plugins_root.resolve(strict=True),
    )
    monkeypatch.setenv("CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT", str(bundle))
    receipt_path = bundle / "CAD-HARNESS-INSTALL-RECEIPT.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["Directories"].append("Contents")
    receipt_path.write_text(json.dumps(receipt, separators=(",", ":")), encoding="utf-8")

    with pytest.raises(OSError, match="directory inventory contains duplicate paths"):
        autocad_com_module._acceptance_bridge_path()


def test_acceptance_bundle_rejects_missing_receipt_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugins_root = tmp_path / "data" / "live-r26" / "ApplicationPlugins"
    bundle = _write_installed_development_bundle(plugins_root)
    monkeypatch.setattr(
        autocad_com_module,
        "_workspace_acceptance_plugins_root",
        lambda: plugins_root.resolve(strict=True),
    )
    monkeypatch.setenv("CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT", str(bundle))
    receipt_path = bundle / "CAD-HARNESS-INSTALL-RECEIPT.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["Directories"].remove("Contents/Windows")
    receipt_path.write_text(json.dumps(receipt, separators=(",", ":")), encoding="utf-8")

    with pytest.raises(OSError, match="filesystem inventory does not match"):
        autocad_com_module._acceptance_bridge_path()


def test_acceptance_bundle_inventory_is_bounded_and_reparse_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _write_installed_development_bundle(tmp_path)
    with pytest.raises(OSError, match="exceeds its entry limit"):
        autocad_com_module._acceptance_bundle_inventory(bundle, maximum_entries=1)

    monkeypatch.setattr(
        autocad_com_module,
        "_is_reparse_path",
        lambda path: path.name == "Contents",
    )
    with pytest.raises(OSError, match="may not contain reparse points"):
        autocad_com_module._acceptance_bundle_inventory(bundle)


def test_acceptance_netload_rejects_legacy_appdata_bundle_without_explicit_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    legacy = (
        tmp_path
        / "Autodesk"
        / "ApplicationPlugins"
        / "AutoCADMechanicalHarness.R26.Acceptance.bundle"
        / "Contents"
        / "Windows"
        / "AutoCADHarness.dll"
    )
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy-unverified")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT", raising=False)

    with pytest.raises(OSError, match="required; legacy APPDATA NETLOAD is disabled"):
        autocad_com_module._acceptance_bridge_path()


class _StoppedHelperProcess:
    pid: int | None = 123
    exitcode: int | None = 0

    def start(self) -> None:  # pragma: no cover - owner tests never start it
        raise AssertionError("unexpected start")

    def join(self, _timeout: float | None = None) -> None:
        return

    def is_alive(self) -> bool:
        return False

    def terminate(self) -> None:  # pragma: no cover - already terminal
        raise AssertionError("unexpected terminate")

    def kill(self) -> None:  # pragma: no cover - already terminal
        raise AssertionError("unexpected kill")


class _NetloadRpcConnection:
    def __init__(
        self,
        response: dict[str, Any],
        *,
        poll_result: bool,
        on_poll: Any,
    ) -> None:
        self.response = autocad_com_module._startup_message(response)
        self.poll_result = poll_result
        self.on_poll = on_poll
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def send_bytes(self, value: bytes) -> None:
        self.sent.append(json.loads(value.decode("ascii")))

    def poll(self, _timeout: float) -> bool:
        self.on_poll()
        return self.poll_result

    def recv_bytes(self, _maximum: int) -> bytes:
        return self.response

    def close(self) -> None:
        self.closed = True


class _InterruptingNetloadRpcConnection(_NetloadRpcConnection):
    def __init__(self, stage: str, interruption_type: type[BaseException]) -> None:
        super().__init__(
            {"stage": "result", "value": {"bridge_loaded": True, "server_pid": 200}},
            poll_result=True,
            on_poll=lambda: None,
        )
        self.stage = stage
        self.interruption_type = interruption_type

    def _interrupt(self, stage: str) -> None:
        if self.stage == stage:
            raise self.interruption_type()

    def send_bytes(self, value: bytes) -> None:
        self._interrupt("send")
        super().send_bytes(value)

    def poll(self, timeout: float) -> bool:
        self._interrupt("poll")
        return super().poll(timeout)

    def recv_bytes(self, maximum: int) -> bytes:
        self._interrupt("read")
        return super().recv_bytes(maximum)


def _configure_test_acceptance_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    plugins_root = tmp_path / "data" / "live-r26" / "ApplicationPlugins"
    bundle = _write_installed_development_bundle(plugins_root)
    monkeypatch.setattr(
        autocad_com_module,
        "_workspace_acceptance_plugins_root",
        lambda: plugins_root.resolve(strict=True),
    )
    monkeypatch.setenv("CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT", str(bundle))
    return bundle, bundle / "Contents" / "Windows" / "AutoCADHarness.dll"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes are Windows-only")
def test_acceptance_bundle_lease_blocks_competing_installer_transaction_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle, _bridge = _configure_test_acceptance_bundle(monkeypatch, tmp_path)
    plugins_root = bundle.parent
    lease = autocad_com_module._acquire_acceptance_bundle()

    with pytest.raises(OSError, match="install root is busy"):
        autocad_com_module._open_exclusive_installer_transaction_lock(plugins_root)

    lease.close()
    competing = autocad_com_module._open_exclusive_installer_transaction_lock(plugins_root)
    competing.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes are Windows-only")
def test_netload_late_unlisted_file_fails_postproof_and_cleans_up_before_unlock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle, bridge = _configure_test_acceptance_bundle(monkeypatch, tmp_path)
    plugins_root = bundle.parent
    command = autocad_com_module._expected_acceptance_netload_command()
    late_file = bundle / "Contents" / "Windows" / "late-unlisted.dll"
    cleanup_events: list[str] = []

    def create_late_file() -> None:
        late_file.write_bytes(b"direct-same-user-drift")

    class _LockObservingJob(_FakeStartupJob):
        def terminate_and_wait(self, timeout_seconds: float) -> bool:
            with pytest.raises(OSError, match="install root is busy"):
                autocad_com_module._open_exclusive_installer_transaction_lock(plugins_root)
            with pytest.raises(OSError):
                bridge.write_bytes(b"write-before-job-terminal")
            cleanup_events.append("job-terminal")
            return super().terminate_and_wait(timeout_seconds)

        def close(self) -> None:
            with pytest.raises(OSError, match="install root is busy"):
                autocad_com_module._open_exclusive_installer_transaction_lock(plugins_root)
            cleanup_events.append("job-close")
            super().close()

    class _ObservedStoppedHelper(_StoppedHelperProcess):
        def join(self, _timeout: float | None = None) -> None:
            with pytest.raises(OSError):
                bridge.write_bytes(b"write-before-helper-terminal")
            cleanup_events.append("helper-terminal")

    connection = _NetloadRpcConnection(
        {"stage": "result", "value": {"bridge_loaded": True, "server_pid": 200}},
        poll_result=True,
        on_poll=create_late_file,
    )
    job = _LockObservingJob()
    owner = autocad_com_module._ComProcessOwner(
        cast(Any, connection),
        cast(Any, _ObservedStoppedHelper()),
        job,
        call_timeout_seconds=1.0,
        expected_autocad_pid=200,
    )

    with pytest.raises(ComCallFailedError) as raised:
        owner.acceptance_netload(9, command)

    assert raised.value.details["reason"] == "acceptance_bundle_changed_during_load"
    assert cleanup_events == ["job-terminal", "helper-terminal", "job-close"]
    assert connection.closed is True
    assert owner._closed is True
    bridge.write_bytes(b"released-after-terminal-cleanup")
    competing = autocad_com_module._open_exclusive_installer_transaction_lock(plugins_root)
    competing.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes are Windows-only")
def test_netload_listed_file_identity_drift_fails_before_acceptance_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _bundle, bridge = _configure_test_acceptance_bundle(monkeypatch, tmp_path)
    command = autocad_com_module._expected_acceptance_netload_command()
    original_identity = autocad_com_module._windows_file_identity_from_stream
    proof_returned = False

    def identity_with_postproof_replacement(stream: Any) -> Any:
        identity = original_identity(stream)
        if not proof_returned:
            return identity
        return autocad_com_module._WindowsFileIdentity(
            identity.volume_serial_number,
            identity.file_index + 1,
            identity.file_size,
        )

    def mark_proof_returned() -> None:
        nonlocal proof_returned
        proof_returned = True

    monkeypatch.setattr(
        autocad_com_module,
        "_windows_file_identity_from_stream",
        identity_with_postproof_replacement,
    )
    connection = _NetloadRpcConnection(
        {"stage": "result", "value": {"bridge_loaded": True, "server_pid": 200}},
        poll_result=True,
        on_poll=mark_proof_returned,
    )
    job = _FakeStartupJob()
    owner = autocad_com_module._ComProcessOwner(
        cast(Any, connection),
        cast(Any, _StoppedHelperProcess()),
        job,
        call_timeout_seconds=1.0,
        expected_autocad_pid=200,
    )

    with pytest.raises(ComCallFailedError) as raised:
        owner.acceptance_netload(9, command)

    assert raised.value.details["reason"] == "acceptance_bundle_changed_during_load"
    assert connection.closed is True
    assert job.closed is True
    assert owner._closed is True
    bridge.write_bytes(b"released-after-listed-identity-drift")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes are Windows-only")
def test_netload_holds_every_verified_artifact_against_write_and_replacement_until_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle, bridge = _configure_test_acceptance_bundle(monkeypatch, tmp_path)
    command = autocad_com_module._expected_acceptance_netload_command()
    metadata = [
        bundle / "CAD-HARNESS-INSTALL-RECEIPT.json",
        bundle / "SHA256SUMS.ps1",
        bridge,
    ]
    original = {path: path.read_bytes() for path in metadata}
    replacement = tmp_path / "AutoCADHarness.replacement.dll"
    replacement.write_bytes(b"replacement")
    lock_checks: list[Path] = []

    def observe_locks() -> None:
        for path in metadata:
            with pytest.raises(OSError):
                path.write_bytes(b"concurrent-write")
            assert path.read_bytes() == original[path]
            lock_checks.append(path)
        with pytest.raises(OSError):
            replacement.replace(bridge)

    connection = _NetloadRpcConnection(
        {
            "stage": "result",
            "value": {"bridge_loaded": True, "server_pid": 200},
        },
        poll_result=True,
        on_poll=observe_locks,
    )
    job = _FakeStartupJob()
    owner = autocad_com_module._ComProcessOwner(
        cast(Any, connection),
        cast(Any, _StoppedHelperProcess()),
        job,
        call_timeout_seconds=1.0,
        expected_autocad_pid=200,
    )

    owner.acceptance_netload(9, command)

    assert connection.sent == [{"object_id": 9, "op": "acceptance_netload"}]
    assert lock_checks == metadata
    bridge.write_bytes(b"released-after-proof")
    owner.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes are Windows-only")
@pytest.mark.parametrize("failure_mode", ["timeout", "rpc_error"])
def test_netload_failure_keeps_bundle_locked_through_terminal_cleanup_then_releases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_mode: str,
) -> None:
    _bundle, bridge = _configure_test_acceptance_bundle(monkeypatch, tmp_path)
    command = autocad_com_module._expected_acceptance_netload_command()
    cleanup_observed_lock: list[bool] = []

    class _LockObservingJob(_FakeStartupJob):
        def terminate_and_wait(self, timeout_seconds: float) -> bool:
            with pytest.raises(OSError):
                bridge.write_bytes(b"write-during-terminal-cleanup")
            cleanup_observed_lock.append(True)
            return super().terminate_and_wait(timeout_seconds)

    connection = _NetloadRpcConnection(
        {"stage": "rpc_error"},
        poll_result=failure_mode != "timeout",
        on_poll=lambda: None,
    )
    job = _LockObservingJob()
    owner = autocad_com_module._ComProcessOwner(
        cast(Any, connection),
        cast(Any, _StoppedHelperProcess()),
        job,
        call_timeout_seconds=0.1,
        expected_autocad_pid=200,
    )

    with pytest.raises(ComCallFailedError):
        owner.acceptance_netload(9, command)

    assert cleanup_observed_lock == [True]
    assert connection.closed is True
    assert job.closed is True
    bridge.write_bytes(b"released-after-terminal-cleanup")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes are Windows-only")
@pytest.mark.parametrize(
    ("stage", "interruption_type"),
    [
        ("send", KeyboardInterrupt),
        ("poll", SystemExit),
        ("read", asyncio.CancelledError),
    ],
)
def test_netload_base_exception_keeps_lease_until_terminal_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    interruption_type: type[BaseException],
) -> None:
    _bundle, bridge = _configure_test_acceptance_bundle(monkeypatch, tmp_path)
    command = autocad_com_module._expected_acceptance_netload_command()
    cleanup_events: list[str] = []

    class _LockObservingJob(_FakeStartupJob):
        def terminate_and_wait(self, timeout_seconds: float) -> bool:
            with pytest.raises(OSError):
                bridge.write_bytes(b"write-during-interrupt-cleanup")
            cleanup_events.append("job-terminal")
            return super().terminate_and_wait(timeout_seconds)

        def close(self) -> None:
            with pytest.raises(OSError):
                bridge.write_bytes(b"write-before-job-handle-close")
            cleanup_events.append("job-close")
            super().close()

    class _ObservedStoppedHelper(_StoppedHelperProcess):
        def join(self, _timeout: float | None = None) -> None:
            with pytest.raises(OSError):
                bridge.write_bytes(b"write-before-helper-join")
            cleanup_events.append("helper-terminal")

    connection = _InterruptingNetloadRpcConnection(stage, interruption_type)
    job = _LockObservingJob()
    owner = autocad_com_module._ComProcessOwner(
        cast(Any, connection),
        cast(Any, _ObservedStoppedHelper()),
        job,
        call_timeout_seconds=0.1,
        expected_autocad_pid=200,
    )

    with pytest.raises(interruption_type):
        owner.acceptance_netload(9, command)

    assert cleanup_events == ["job-terminal", "helper-terminal", "job-close"]
    assert connection.closed is True
    assert job.closed is True
    assert owner._closed is True
    bridge.write_bytes(b"released-after-interrupt-terminal-cleanup")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes are Windows-only")
def test_disconnect_releases_any_retained_acceptance_bundle_only_after_job_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _bundle, bridge = _configure_test_acceptance_bundle(monkeypatch, tmp_path)
    cleanup_observed_lock: list[bool] = []

    class _LockObservingJob(_FakeStartupJob):
        def terminate_and_wait(self, timeout_seconds: float) -> bool:
            with pytest.raises(OSError):
                bridge.write_bytes(b"write-before-disconnect-terminal")
            cleanup_observed_lock.append(True)
            return super().terminate_and_wait(timeout_seconds)

    connection = _NetloadRpcConnection(
        {"stage": "result", "value": None},
        poll_result=True,
        on_poll=lambda: None,
    )
    job = _LockObservingJob()
    owner = autocad_com_module._ComProcessOwner(
        cast(Any, connection),
        cast(Any, _StoppedHelperProcess()),
        job,
        call_timeout_seconds=0.1,
        expected_autocad_pid=200,
    )
    owner._acceptance_bundle_lease = autocad_com_module._acquire_acceptance_bundle()

    owner.shutdown()

    assert cleanup_observed_lock == [True]
    bridge.write_bytes(b"released-after-disconnect")
