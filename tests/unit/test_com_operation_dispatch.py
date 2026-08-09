"""Unit coverage for COM operation dispatch without requiring AutoCAD."""

from __future__ import annotations

import inspect
import math
import sys
from types import ModuleType
from typing import Any

import pytest

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
    def __init__(self) -> None:
        super().__init__("pythoncom")
        self.initialize_calls = 0
        self.uninitialize_calls = 0

    def CoInitialize(self) -> None:  # noqa: N802
        self.initialize_calls += 1

    def CoUninitialize(self) -> None:  # noqa: N802
        self.uninitialize_calls += 1


class _IsolatedApp:
    def __init__(self, hwnd: int) -> None:
        self.HWND = hwnd
        self.quit_calls = 0

    def Quit(self) -> None:  # noqa: N802
        self.quit_calls += 1


class _FakeComClient(ModuleType):
    def __init__(self, app: _IsolatedApp) -> None:
        super().__init__("win32com.client")
        self.app = app
        self.dispatch_ids: list[str] = []

    def DispatchEx(self, prog_id: str) -> _IsolatedApp:  # noqa: N802
        self.dispatch_ids.append(prog_id)
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


def test_isolated_connect_proves_new_exact_acad_process_before_exposing_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _IsolatedApp(hwnd=4321)
    pythoncom, client = _install_fake_com_modules(monkeypatch, app)
    process_snapshots = iter(({100}, {100, 200}))
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_acad_process_ids",
        staticmethod(lambda: set(next(process_snapshots))),
    )
    monkeypatch.setattr(
        ComAutoCADAdapter, "_pid_from_hwnd", staticmethod(lambda hwnd: 200 if hwnd == 4321 else 0)
    )
    monkeypatch.setattr(ComAutoCADAdapter, "_system_filetime_100ns", staticmethod(lambda: 1_000))
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_process_identity",
        staticmethod(lambda pid: (r"D:\CAD\acad.exe", 1_001)),
    )
    adapter = ComAutoCADAdapter()

    session = adapter.connect_isolated(versioned_prog_id="AutoCAD.Application.26")

    assert session == OwnedComSession(
        prog_id="AutoCAD.Application.26",
        hwnd=4321,
        pid=200,
        image_path=r"D:\CAD\acad.exe",
        creation_time_100ns=1_001,
    )
    assert session.owned is True
    assert adapter.owned_session is session
    assert adapter.require_owned_application() is app
    assert adapter._document is None
    assert client.dispatch_ids == ["AutoCAD.Application.26"]
    assert pythoncom.initialize_calls == 1

    adapter.disconnect()
    assert app.quit_calls == 0
    assert pythoncom.uninitialize_calls == 1
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
    process_snapshots = iter((preexisting, current))
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_acad_process_ids",
        staticmethod(lambda: set(next(process_snapshots))),
    )
    monkeypatch.setattr(ComAutoCADAdapter, "_pid_from_hwnd", staticmethod(lambda hwnd: 200))
    monkeypatch.setattr(ComAutoCADAdapter, "_system_filetime_100ns", staticmethod(lambda: 1_000))
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_process_identity",
        staticmethod(lambda pid: (r"D:\CAD\acad.exe", 1_001)),
    )
    adapter = ComAutoCADAdapter()

    with pytest.raises(ComCallFailedError) as captured:
        adapter.connect_isolated(versioned_prog_id="AutoCAD.Application.26")

    assert captured.value.details["reason"] == "isolated_process_ownership_unproven"
    assert adapter.owned_session is None
    assert adapter._app is None
    assert app.quit_calls == 0
    assert pythoncom.uninitialize_calls == 1


def test_isolated_connect_rejects_unversioned_progid_before_com_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _IsolatedApp(hwnd=4321)
    pythoncom, client = _install_fake_com_modules(monkeypatch, app)

    with pytest.raises(ValueError, match="versioned_prog_id"):
        ComAutoCADAdapter().connect_isolated(versioned_prog_id="AutoCAD.Application")

    assert client.dispatch_ids == []
    assert pythoncom.initialize_calls == 0
