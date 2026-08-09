"""Deterministic safety examples for the killable process boundary."""

from __future__ import annotations

from dataclasses import asdict
from multiprocessing import active_children
from pathlib import Path
from time import monotonic, sleep

import ezdxf
import pytest

import cad_harness.application.process_runner as process_runner
from cad_harness.application.process_runner import (
    ProcessWorkerCommand,
    run_process_worker,
)
from cad_harness.application.timeout import OperationDeadline
from cad_harness.domain.errors import (
    HarnessError,
    InvalidFeatureParametersError,
    IpcTimeoutError,
    UnsupportedFeatureError,
)
from cad_harness.domain.models.drawing_model import (
    DrawingModel,
    EntityRecord,
    PolylineGeometry,
    PolylineVertex,
    ReadScope,
)
from cad_harness.domain.models.measurement import MeasurementKind, MeasurementRequest
from cad_harness.domain.models.takeoff import PartInput, TakeoffRequest
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef
from cad_harness.geometry.tolerance import ToleranceProfile


@pytest.mark.parametrize("worker_seconds", [0.0, 0.02])
def test_below_deadline_returns_json_only_after_worker_join(worker_seconds: float) -> None:
    result = run_process_worker(
        OperationDeadline(2.0, "pure-worker"),
        ProcessWorkerCommand.SLEEP,
        {"seconds": worker_seconds, "result": {"answer": 42}},
    )

    assert result == {"answer": 42}
    assert not [child for child in active_children() if child.name == "cad-harness-sleep"]


def test_large_json_response_does_not_deadlock_on_pipe_capacity() -> None:
    value = "x" * 262_144

    result = run_process_worker(
        OperationDeadline(2.0, "large-worker"),
        ProcessWorkerCommand.ECHO_JSON,
        {"value": value},
    )

    assert result == {"value": value}


def test_timeout_returns_near_boundary_with_terminal_pid() -> None:
    timeout_seconds = 0.15
    started_at = monotonic()

    with pytest.raises(IpcTimeoutError) as captured:
        run_process_worker(
            OperationDeadline(timeout_seconds, "pure-worker"),
            ProcessWorkerCommand.SLEEP,
            {"seconds": 5.0},
        )

    elapsed = monotonic() - started_at
    worker_pid = captured.value.details["worker_pid"]
    assert elapsed >= timeout_seconds * 0.8
    assert elapsed < 2.0
    assert captured.value.code.value == "IPC_TIMEOUT"
    assert captured.value.details["worker_terminal"] is True
    assert worker_pid not in {child.pid for child in active_children()}


def test_timeout_kills_worker_before_late_marker_side_effect(tmp_path) -> None:
    marker = tmp_path / "late.json"

    with pytest.raises(IpcTimeoutError):
        run_process_worker(
            OperationDeadline(0.15, "artifact-worker"),
            ProcessWorkerCommand.WRITE_JSON_MARKER,
            {
                "delay_seconds": 0.6,
                "filename": marker.name,
                "document": {"late": True},
            },
            allowed_output_root=tmp_path,
        )

    sleep(0.7)
    assert not marker.exists()


def test_completed_marker_is_bounded_to_allowlisted_root(tmp_path) -> None:
    result = run_process_worker(
        OperationDeadline(2.0, "artifact-worker"),
        ProcessWorkerCommand.WRITE_JSON_MARKER,
        {
            "delay_seconds": 0.0,
            "filename": "complete.json",
            "document": {"state": "complete"},
        },
        allowed_output_root=tmp_path,
    )

    assert result == {"filename": "complete.json", "written": True}
    assert (tmp_path / "complete.json").read_text(encoding="utf-8") == '{"state":"complete"}'


def test_invalid_command_fails_closed_without_starting_process() -> None:
    before = {child.pid for child in active_children()}

    with pytest.raises(UnsupportedFeatureError) as captured:
        run_process_worker(
            OperationDeadline(1.0, "invalid-worker"),
            "builtins.eval",  # type: ignore[arg-type]
            {"value": "unsafe"},
        )

    assert "builtins.eval" not in str(captured.value)
    assert {child.pid for child in active_children()} == before


@pytest.mark.parametrize(
    "payload",
    [
        {"seconds": 0.0, "callable": "os.system"},
        {"seconds": float("nan")},
        {"seconds": "soon"},
    ],
)
def test_invalid_payload_fails_closed(payload: object) -> None:
    with pytest.raises(InvalidFeatureParametersError):
        run_process_worker(
            OperationDeadline(1.0, "invalid-worker"),
            ProcessWorkerCommand.SLEEP,
            payload,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("filename", ["../escape.json", "folder/escape.json", "escape.txt"])
def test_marker_rejects_unbounded_or_non_json_paths(tmp_path, filename: str) -> None:
    with pytest.raises(InvalidFeatureParametersError):
        run_process_worker(
            OperationDeadline(1.0, "invalid-marker"),
            ProcessWorkerCommand.WRITE_JSON_MARKER,
            {"delay_seconds": 0.0, "filename": filename, "document": {}},
            allowed_output_root=tmp_path,
        )


def test_child_failure_is_sanitized_and_terminal(tmp_path) -> None:
    marker = tmp_path / "existing.json"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(HarnessError) as captured:
        run_process_worker(
            OperationDeadline(2.0, "failed-marker"),
            ProcessWorkerCommand.WRITE_JSON_MARKER,
            {
                "delay_seconds": 0.0,
                "filename": marker.name,
                "document": {"replace": False},
            },
            allowed_output_root=tmp_path,
        )

    assert captured.value.code.value == "INTERNAL_ERROR"
    assert captured.value.details == {
        "command": ProcessWorkerCommand.WRITE_JSON_MARKER.value,
        "worker_terminal": True,
    }
    assert str(tmp_path) not in captured.value.message
    assert marker.read_text(encoding="utf-8") == "preserve"


def _tolerance_json() -> dict[str, object]:
    return asdict(ToleranceProfile(id="worker-test", version="1.0"))


def _contract_dxf(path: Path) -> None:
    document = ezdxf.new("R2010", setup=True)
    document.units = 4
    document.modelspace().add_line((0.0, 0.0), (25.0, 0.0), dxfattribs={"layer": "0"})
    document.saveas(path)


def _takeoff_model() -> DrawingModel:
    outline = EntityRecord(
        entity_ref="outline",
        entity_type="AcDbPolyline",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=PolylineGeometry(
            vertices=tuple(
                PolylineVertex(point_mm=point)
                for point in ((0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0))
            ),
            closed=True,
        ),
        bounding_box_mm=(0.0, 0.0, 100.0, 50.0),
    )
    return DrawingModel(
        document_id="doc-worker",
        revision="sha256:worker",
        display_name="worker.dxf",
        source_unit_code="mm",
        to_mm_factor=1.0,
        geometry_normalized=True,
        scope=ReadScope(),
        entities=(outline,),
        arc_chord_tolerance_mm=0.01,
    )


def test_dxf_commands_round_trip_strict_contracts(tmp_path: Path) -> None:
    source = tmp_path / "worker.dxf"
    _contract_dxf(source)
    request = DrawingReadRequest(
        source=DrawingSourceRef(kind="file", format="dxf", ref=str(source)),
        scope=ReadScope(),
        max_entities=10,
        max_block_nesting_depth=2,
    )
    common = {"request": request.model_dump(mode="json"), "tolerance": _tolerance_json()}

    revision = run_process_worker(
        OperationDeadline(5.0, "dxf-revision"),
        ProcessWorkerCommand.DXF_CURRENT_REVISION,
        {"document_id": str(source)},
        allowed_input_root=tmp_path,
    )
    summary = run_process_worker(
        OperationDeadline(5.0, "dxf-summary"),
        ProcessWorkerCommand.DXF_SUMMARY,
        common,  # type: ignore[arg-type]
        allowed_input_root=tmp_path,
    )
    model = run_process_worker(
        OperationDeadline(5.0, "dxf-model"),
        ProcessWorkerCommand.DXF_MODEL,
        common,  # type: ignore[arg-type]
        allowed_input_root=tmp_path,
    )

    assert str(revision["revision"]).startswith("sha256:")
    assert summary["summary"]["counts_by_entity_type"] == {"AcDbLine": 1}  # type: ignore[index]
    assert model["model"]["entities"][0]["entity_type"] == "AcDbLine"  # type: ignore[index]


def test_material_takeoff_and_measure_commands_round_trip() -> None:
    materials = run_process_worker(
        OperationDeadline(5.0, "materials"),
        ProcessWorkerCommand.LOAD_MATERIAL_TABLE,
        {"profile_ref": "demo-materials@1.0"},
    )["materials"]
    model = _takeoff_model()
    request = TakeoffRequest(
        document_id=model.document_id,
        parts=(
            PartInput(
                part_code="P-001",
                outline_entity_ref="outline",
                thickness_mm=10.0,
                material_code="SS400",
                quantity=2,
            ),
        ),
        material_profile_ref="demo-materials@1.0",
    )
    report = run_process_worker(
        OperationDeadline(5.0, "takeoff"),
        ProcessWorkerCommand.COMPUTE_TAKEOFF,
        {
            "model": model.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
            "materials": materials,
            "tolerance": _tolerance_json(),
        },  # type: ignore[arg-type]
    )["report"]
    measurement = run_process_worker(
        OperationDeadline(5.0, "measure"),
        ProcessWorkerCommand.MEASURE,
        {
            "model": model.model_dump(mode="json"),
            "request": MeasurementRequest(
                kind=MeasurementKind.POINT_TO_POINT,
                first_point_mm=(0.0, 0.0),
                second_point_mm=(3.0, 4.0),
            ).model_dump(mode="json"),
            "tolerance": _tolerance_json(),
        },  # type: ignore[arg-type]
    )["measurement"]

    assert report["parts"][0]["part_code"] == "P-001"  # type: ignore[index]
    assert measurement["value"] == 5.0  # type: ignore[index]


def test_dxf_command_rejects_missing_allowlist_and_outside_file(tmp_path: Path) -> None:
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path / "outside.dxf"
    _contract_dxf(outside)

    for root in (None, inside):
        with pytest.raises(InvalidFeatureParametersError) as captured:
            run_process_worker(
                OperationDeadline(1.0, "invalid-dxf"),
                ProcessWorkerCommand.DXF_CURRENT_REVISION,
                {"document_id": str(outside)},
                allowed_input_root=root,
            )
        assert str(tmp_path) not in captured.value.message


def test_production_commands_reject_unknown_fields_and_oversized_json() -> None:
    with pytest.raises(InvalidFeatureParametersError):
        run_process_worker(
            OperationDeadline(1.0, "invalid-materials"),
            ProcessWorkerCommand.LOAD_MATERIAL_TABLE,
            {"profile_ref": "demo-materials@1.0", "directory": "unsafe"},
        )

    with pytest.raises(InvalidFeatureParametersError):
        run_process_worker(
            OperationDeadline(1.0, "oversized"),
            ProcessWorkerCommand.ECHO_JSON,
            {"value": "x" * (process_runner.MAX_REQUEST_BYTES + 1)},
        )
