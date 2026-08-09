"""Shared semantic contract for read-only drawing source implementations.

Live COM/Bridge semantic parity belongs to tasks 29.11/30.2, once their real
reader implementations exist.
"""

import json
from multiprocessing import active_children
from pathlib import Path
from typing import Any

import ezdxf
import pytest

import cad_harness.adapters.dxf_drawing_reader as dxf_reader_module
from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
from cad_harness.application.process_runner import ProcessWorkerCommand
from cad_harness.application.timeout import OperationDeadline
from cad_harness.domain.errors import IpcTimeoutError
from cad_harness.domain.models.drawing_model import DrawingModel, ReadScope
from cad_harness.domain.ports.drawing_source import (
    DrawingReadRequest,
    DrawingSourcePort,
    DrawingSourceRef,
)


def _contract_dxf(path: Path) -> None:
    document = ezdxf.new("R2018", setup=True)
    document.header["$INSUNITS"] = 4
    model = document.modelspace()
    model.add_line((0, 0), (5, 0))
    model.add_polyline2d([(0, 1), (5, 1)])
    model.add_lwpolyline([(0, 2), (5, 2)])
    model.add_circle((2, 4), 1)
    model.add_arc((5, 4), 1, 0, 90)
    model.add_ellipse((8, 4), major_axis=(2, 0), ratio=0.5)
    model.add_text("TEXT", dxfattribs={"insert": (0, 6), "height": 1})
    model.add_mtext("MTEXT", dxfattribs={"insert": (3, 6), "char_height": 1})
    model.add_linear_dim(base=(0, 8), p1=(0, 0), p2=(5, 0)).render()
    hatch = model.add_hatch(color=2)
    hatch.paths.add_polyline_path([(0, 10), (2, 10), (2, 12), (0, 12)], is_closed=True)
    block = document.blocks.new("PART")
    block.add_line((0, 0), (1, 1))
    model.add_blockref("PART", (10, 10))
    document.saveas(path)


def _semantic_signature(model: DrawingModel) -> tuple[tuple[str, str, str], ...]:
    return tuple((entity.entity_ref, entity.entity_type, entity.layer) for entity in model.entities)


def test_dxf_reader_satisfies_drawing_source_contract(tmp_path: Path) -> None:
    path = tmp_path / "contract.dxf"
    _contract_dxf(path)
    reader = DxfDrawingReader()
    assert isinstance(reader, DrawingSourcePort)
    request = DrawingReadRequest(
        source=DrawingSourceRef(kind="file", format="dxf", ref=str(path)),
        scope=ReadScope(),
        max_entities=100,
        max_block_nesting_depth=3,
    )

    first = reader.read(request)
    second = reader.read(request)
    assert _semantic_signature(first) == _semantic_signature(second)
    assert len(first.entities) == 11
    assert {entity.entity_type for entity in first.entities} == {
        "AcDbLine",
        "AcDb2dPolyline",
        "AcDbPolyline",
        "AcDbCircle",
        "AcDbArc",
        "AcDbEllipse",
        "AcDbText",
        "AcDbMText",
        "AcDbDimension",
        "AcDbHatch",
        "AcDbBlockReference",
    }


def test_dxf_cancellable_reader_passes_only_json_to_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "isolated-contract.dxf"
    _contract_dxf(path)
    reader = DxfDrawingReader()
    request = DrawingReadRequest(
        source=DrawingSourceRef(kind="file", format="dxf", ref=str(path)),
        scope=ReadScope(),
        max_entities=100,
        max_block_nesting_depth=3,
    )
    expected_revision = reader.current_revision(str(path))
    expected_summary = reader.summarize(request)
    expected_model = reader.read(request)
    calls: list[tuple[ProcessWorkerCommand, dict[str, Any], Path | None]] = []

    def fake_worker(
        deadline: OperationDeadline,
        command: ProcessWorkerCommand,
        payload: dict[str, Any],
        *,
        allowed_output_root: Path | None = None,
        allowed_input_root: Path | None = None,
    ) -> dict[str, Any]:
        del deadline, allowed_output_root
        json.dumps(payload, allow_nan=False)
        calls.append((command, payload, allowed_input_root))
        if command is ProcessWorkerCommand.DXF_CURRENT_REVISION:
            return {"revision": expected_revision}
        if command is ProcessWorkerCommand.DXF_SUMMARY:
            return {"summary": expected_summary.model_dump(mode="json")}
        if command is ProcessWorkerCommand.DXF_MODEL:
            return {"model": expected_model.model_dump(mode="json")}
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(dxf_reader_module, "run_process_worker", fake_worker)

    assert (
        reader.current_revision_cancellable(str(path), OperationDeadline(1.0, "read"))
        == expected_revision
    )
    assert reader.summarize_cancellable(request, OperationDeadline(1.0, "read")) == expected_summary
    assert reader.read_cancellable(request, OperationDeadline(1.0, "read")) == expected_model
    assert [command for command, _, _ in calls] == [
        ProcessWorkerCommand.DXF_CURRENT_REVISION,
        ProcessWorkerCommand.DXF_SUMMARY,
        ProcessWorkerCommand.DXF_MODEL,
    ]
    assert all(root == path.parent for _, _, root in calls)
    for command, payload, _ in calls[1:]:
        assert command in {ProcessWorkerCommand.DXF_SUMMARY, ProcessWorkerCommand.DXF_MODEL}
        assert payload["request"] == request.model_dump(mode="json")
        assert set(payload["tolerance"]) == {
            "id",
            "version",
            "canonical_unit",
            "absolute_length_mm",
            "relative_length",
            "angular_deg",
            "coincidence_mm",
            "area_mm2",
            "arc_chord_tolerance_mm",
        }


def test_dxf_worker_timeout_returns_only_after_child_is_terminal(tmp_path: Path) -> None:
    path = tmp_path / "timeout-contract.dxf"
    _contract_dxf(path)
    reader = DxfDrawingReader()
    request = DrawingReadRequest(
        source=DrawingSourceRef(kind="file", format="dxf", ref=str(path)),
        scope=ReadScope(),
        max_entities=100,
        max_block_nesting_depth=3,
    )

    with pytest.raises(IpcTimeoutError) as captured:
        reader.read_cancellable(request, OperationDeadline(0.02, "read"))

    assert captured.value.details["worker_terminal"] is True
    worker_pid = captured.value.details["worker_pid"]
    assert worker_pid not in {child.pid for child in active_children()}
