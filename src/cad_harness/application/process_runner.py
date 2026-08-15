"""Killable, allowlisted subprocess boundary for pure bounded work.

The runner deliberately does not accept callables, import paths, module names, or
pickled exception objects. Requests and responses cross the private multiprocessing
channel as bounded JSON bytes. Every command is implemented in this module so a caller
cannot make a child process import application plugins or touch COM, databases, named
pipes, or networks.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path
from threading import Lock
from time import sleep
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cad_harness.domain.errors import (
    HarnessError,
    InvalidFeatureParametersError,
    IpcTimeoutError,
    UnsupportedFeatureError,
)
from cad_harness.domain.models.drawing_model import DrawingModel
from cad_harness.domain.models.measurement import MeasurementRequest
from cad_harness.domain.models.takeoff import MaterialTable, TakeoffRequest
from cad_harness.domain.ports.drawing_source import DrawingReadRequest
from cad_harness.domain.ports.repositories import CancellationTokenPort
from cad_harness.geometry.tolerance import ToleranceProfile

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None

MAX_ENVELOPE_BYTES = 16_777_216
MAX_REQUEST_BYTES = MAX_ENVELOPE_BYTES - 1_024
MAX_JSON_DEPTH = 32
_TERMINATE_GRACE_SECONDS = 0.25
_BROKER_STARTUP_TIMEOUT_SECONDS = 30.0
_BROKER_REGISTRY_LOCK = Lock()
_ACTIVE_BROKER: _ProcessWorkerBroker | None = None


class ProcessWorkerCommand(StrEnum):
    """Closed command set understood by the isolated worker."""

    ECHO_JSON = "echo_json"
    SLEEP = "sleep"
    WRITE_JSON_MARKER = "write_json_marker"
    DXF_CURRENT_REVISION = "dxf_current_revision"
    DXF_SUMMARY = "dxf_summary"
    DXF_MODEL = "dxf_model"
    LOAD_MATERIAL_TABLE = "load_material_table"
    COMPUTE_TAKEOFF = "compute_takeoff"
    MEASURE = "measure"
    BRIDGE_REQUEST = "bridge_request"


class _ToleranceInput(BaseModel):
    """Strict JSON representation of the dataclass-only tolerance policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    canonical_unit: Literal["mm"] = "mm"
    absolute_length_mm: float = Field(default=1.0e-3, gt=0.0)
    relative_length: float = Field(default=1.0e-9, ge=0.0)
    angular_deg: float = Field(default=1.0e-4, gt=0.0)
    coincidence_mm: float = Field(default=1.0e-3, gt=0.0)
    area_mm2: float = Field(default=1.0e-2, gt=0.0)
    arc_chord_tolerance_mm: float = Field(default=0.01, gt=0.0)

    def to_profile(self) -> ToleranceProfile:
        return ToleranceProfile(**self.model_dump())


def run_process_worker(
    deadline: CancellationTokenPort,
    command: ProcessWorkerCommand,
    payload: Mapping[str, JsonValue],
    *,
    allowed_output_root: Path | None = None,
    allowed_input_root: Path | None = None,
) -> dict[str, JsonValue]:
    """Execute one allowlisted JSON job and return only after its PID is terminal.

    Filesystem commands are closed over explicit roots. ``WRITE_JSON_MARKER`` may only
    create one local JSON file below ``allowed_output_root``. DXF commands may only
    read one existing, non-symlink file below ``allowed_input_root``. Material tables
    resolve only from the packaged controlled directory; all other commands are pure.
    """

    deadline.checkpoint()
    request_json, normalized_payload, normalized_output_root, normalized_input_root = (
        _prepare_request(command, payload, allowed_output_root, allowed_input_root)
    )
    broker = _registered_broker()
    if broker is not None:
        return broker.run(
            deadline,
            command,
            request_json,
            normalized_payload,
            normalized_output_root,
            normalized_input_root,
        )
    return _run_one_shot_worker(
        deadline,
        command,
        request_json,
        normalized_payload,
        normalized_output_root,
        normalized_input_root,
    )


def _run_one_shot_worker(
    deadline: CancellationTokenPort,
    command: ProcessWorkerCommand,
    request_json: bytes,
    normalized_payload: dict[str, JsonValue],
    normalized_output_root: str | None,
    normalized_input_root: str | None,
) -> dict[str, JsonValue]:
    """Run the original per-call worker outside an active STDIO broker."""
    context = get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker_main,
        args=(
            sender,
            command.value,
            request_json,
            normalized_output_root,
            normalized_input_root,
        ),
        name=f"cad-harness-{command.value}",
        daemon=False,
    )
    try:
        process.start()
        sender.close()
        worker_pid = process.pid
        raw_response: bytes | None = None
        if receiver.poll(deadline.remaining_seconds):
            try:
                raw_response = receiver.recv_bytes(MAX_ENVELOPE_BYTES + 1)
            except (EOFError, OSError):
                raw_response = None
        process.join(deadline.remaining_seconds)

        if (
            process.is_alive()
            or deadline.cancelled
            or deadline.elapsed_seconds > deadline.timeout_seconds
        ):
            deadline.cancel()
            _terminate_and_join(process)
            raise IpcTimeoutError(
                f"Operation '{deadline.operation}' exceeded its configured timeout",
                details={
                    "operation": deadline.operation,
                    "timeout_seconds": deadline.timeout_seconds,
                    "elapsed_seconds": deadline.elapsed_seconds,
                    "cancelled": True,
                    "worker_pid": worker_pid,
                    "worker_terminal": not process.is_alive(),
                },
            )

        # ``join`` above is intentionally before reading or returning. A successful
        # response therefore cannot outlive its worker or produce later side effects.
        if raw_response is None and receiver.poll(0.0):
            try:
                raw_response = receiver.recv_bytes(MAX_ENVELOPE_BYTES + 1)
            except (EOFError, OSError):
                raw_response = None
        if process.exitcode != 0 or raw_response is None:
            raise HarnessError(
                "Isolated process worker failed",
                details={"command": command.value, "worker_terminal": True},
            )
        envelope = _decode_json_object(raw_response)
        return _unwrap_response(envelope, command, normalized_payload)
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None and process.is_alive():
            _terminate_and_join(process)


class _ProcessWorkerBroker:
    """Prestarted worker used where spawning after transport startup is unsafe.

    FastMCP's Windows STDIO transport keeps blocking stream wrappers active. Starting a
    new spawn process from that runtime can block inside ``multiprocessing.reduction``
    before the caller can poll its deadline. The broker is therefore spawned before the
    transport starts and executes the same closed command set over bounded JSON.
    """

    def __init__(self) -> None:
        context = get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=_broker_main,
            args=(child,),
            name="cad-harness-process-broker",
            daemon=False,
        )
        self._connection = parent
        self._process = process
        self._request_lock = Lock()
        self._terminal = False
        try:
            process.start()
            child.close()
            if not parent.poll(_BROKER_STARTUP_TIMEOUT_SECONDS):
                self._terminate()
                raise HarnessError(
                    "Isolated process broker did not become ready",
                    required_action="Restart the MCP server",
                    details={"worker_terminal": True},
                )
            ready = _decode_json_object(parent.recv_bytes(MAX_ENVELOPE_BYTES + 1))
            if ready != {"ready": True}:
                self._terminate()
                raise HarnessError(
                    "Isolated process broker returned an invalid startup envelope",
                    required_action="Restart the MCP server",
                    details={"worker_terminal": True},
                )
        except Exception:
            child.close()
            parent.close()
            if process.pid is not None and process.is_alive():
                _terminate_and_join(process)
            raise

    def run(
        self,
        deadline: CancellationTokenPort,
        command: ProcessWorkerCommand,
        request_json: bytes,
        normalized_payload: dict[str, JsonValue],
        normalized_output_root: str | None,
        normalized_input_root: str | None,
    ) -> dict[str, JsonValue]:
        deadline.checkpoint()
        if not self._request_lock.acquire(timeout=deadline.remaining_seconds):
            deadline.cancel()
            raise IpcTimeoutError(
                f"Operation '{deadline.operation}' exceeded its configured timeout",
                details={
                    "operation": deadline.operation,
                    "timeout_seconds": deadline.timeout_seconds,
                    "elapsed_seconds": deadline.elapsed_seconds,
                    "cancelled": True,
                    "worker_terminal": self._terminal,
                },
            )
        try:
            if self._terminal or not self._process.is_alive():
                raise HarnessError(
                    "Isolated process broker is unavailable",
                    required_action="Restart the MCP server before retrying the operation",
                    details={"command": command.value, "worker_terminal": True},
                )
            broker_request = _encode_json(
                {
                    "command": command.value,
                    "payload": _decode_json_object(request_json),
                    "allowed_output_root": normalized_output_root,
                    "allowed_input_root": normalized_input_root,
                }
            )
            self._connection.send_bytes(broker_request)
            raw_response: bytes | None = None
            if self._connection.poll(deadline.remaining_seconds):
                try:
                    raw_response = self._connection.recv_bytes(MAX_ENVELOPE_BYTES + 1)
                except (EOFError, OSError):
                    raw_response = None
            if (
                raw_response is None
                or deadline.cancelled
                or deadline.elapsed_seconds > deadline.timeout_seconds
            ):
                deadline.cancel()
                worker_pid = self._process.pid
                self._terminate()
                raise IpcTimeoutError(
                    f"Operation '{deadline.operation}' exceeded its configured timeout",
                    details={
                        "operation": deadline.operation,
                        "timeout_seconds": deadline.timeout_seconds,
                        "elapsed_seconds": deadline.elapsed_seconds,
                        "cancelled": True,
                        "worker_pid": worker_pid,
                        "worker_terminal": True,
                    },
                )
            envelope = _decode_json_object(raw_response)
            return _unwrap_response(envelope, command, normalized_payload)
        finally:
            self._request_lock.release()

    def close(self) -> None:
        with self._request_lock:
            if self._terminal:
                return
            try:
                self._connection.send_bytes(_encode_json({"shutdown": True}))
                self._process.join(_TERMINATE_GRACE_SECONDS)
            except (EOFError, OSError):
                pass
            finally:
                if self._process.is_alive():
                    _terminate_and_join(self._process)
                self._connection.close()
                self._terminal = True

    def _terminate(self) -> None:
        if self._terminal:
            return
        if self._process.pid is not None and self._process.is_alive():
            _terminate_and_join(self._process)
        self._connection.close()
        self._terminal = True


def _broker_main(connection: Connection) -> None:
    try:
        _warm_broker_commands()
        connection.send_bytes(_encode_json({"ready": True}))
        while True:
            raw_request = connection.recv_bytes(MAX_ENVELOPE_BYTES + 1)
            request = _decode_json_object(raw_request)
            if request == {"shutdown": True}:
                return
            command_value = request.get("command")
            try:
                if not isinstance(command_value, str):
                    raise ValueError("missing broker command")
                command = ProcessWorkerCommand(command_value)
                payload = request.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("invalid broker payload")
                output_root = request.get("allowed_output_root")
                input_root = request.get("allowed_input_root")
                if output_root is not None and not isinstance(output_root, str):
                    raise ValueError("invalid output root")
                if input_root is not None and not isinstance(input_root, str):
                    raise ValueError("invalid input root")
                result = _execute_command(command, payload, output_root, input_root)
                response: dict[str, JsonValue] = {
                    "ok": True,
                    "command": command.value,
                    "result": result,
                }
            except Exception:
                response = {
                    "ok": False,
                    "command": command_value if isinstance(command_value, str) else "invalid",
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "Isolated process worker failed",
                        "retryable": False,
                        "required_action": None,
                        "details": {},
                    },
                }
            connection.send_bytes(_encode_json(response))
    except (EOFError, OSError):
        return
    finally:
        connection.close()


def _warm_broker_commands() -> None:
    """Pay the controlled command import cost before STDIO starts accepting calls."""
    from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
    from cad_harness.application.services.measurement_service import MeasurementService
    from cad_harness.company_rules.material_loader import load_material_table
    from cad_harness.comprehension.takeoff import compute_takeoff

    # Keeping explicit references makes this a preload, not an accidental unused import.
    del DxfDrawingReader, MeasurementService, compute_takeoff, load_material_table


def _registered_broker() -> _ProcessWorkerBroker | None:
    with _BROKER_REGISTRY_LOCK:
        return _ACTIVE_BROKER


@contextmanager
def prestarted_process_worker_broker() -> Iterator[None]:
    """Install one broker before a Windows STDIO transport begins serving calls."""
    global _ACTIVE_BROKER
    broker = _ProcessWorkerBroker()
    with _BROKER_REGISTRY_LOCK:
        if _ACTIVE_BROKER is not None:
            broker.close()
            raise RuntimeError("A process worker broker is already active")
        _ACTIVE_BROKER = broker
    try:
        yield
    finally:
        with _BROKER_REGISTRY_LOCK:
            if _ACTIVE_BROKER is broker:
                _ACTIVE_BROKER = None
        broker.close()


def _prepare_request(
    command: ProcessWorkerCommand,
    payload: Mapping[str, JsonValue],
    allowed_output_root: Path | None,
    allowed_input_root: Path | None,
) -> tuple[bytes, dict[str, JsonValue], str | None, str | None]:
    if not isinstance(command, ProcessWorkerCommand):
        raise UnsupportedFeatureError(
            "Process worker command is not allowlisted",
            details={"allowed_commands": [item.value for item in ProcessWorkerCommand]},
        )
    if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
        raise InvalidFeatureParametersError("Process worker payload must be a JSON object")

    normalized_payload = _decode_json_object(_encode_json(dict(payload)))
    normalized_output_root: str | None = None
    normalized_input_root: str | None = None
    if command is ProcessWorkerCommand.ECHO_JSON:
        _require_exact_keys(normalized_payload, required={"value"})
    elif command is ProcessWorkerCommand.SLEEP:
        _require_exact_keys(normalized_payload, required={"seconds"}, optional={"result"})
        _require_duration(normalized_payload["seconds"])
        result = normalized_payload.get("result", {})
        if not isinstance(result, dict):
            raise InvalidFeatureParametersError("Sleep result must be a JSON object")
    elif command is ProcessWorkerCommand.WRITE_JSON_MARKER:
        _require_exact_keys(
            normalized_payload,
            required={"delay_seconds", "filename", "document"},
        )
        _require_duration(normalized_payload["delay_seconds"])
        filename = normalized_payload["filename"]
        if (
            not isinstance(filename, str)
            or not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or not filename.casefold().endswith(".json")
        ):
            raise InvalidFeatureParametersError("Marker filename must be one local .json filename")
        if allowed_output_root is None:
            raise InvalidFeatureParametersError(
                "Marker command requires an allowlisted output root"
            )
        if not isinstance(allowed_output_root, Path):
            raise InvalidFeatureParametersError("Allowlisted output root must be a path")
        if allowed_output_root.is_symlink() or not allowed_output_root.is_dir():
            raise InvalidFeatureParametersError(
                "Allowlisted output root must be an existing non-symlink directory"
            )
        normalized_output_root = str(allowed_output_root.resolve(strict=True))
    elif command is ProcessWorkerCommand.DXF_CURRENT_REVISION:
        _require_exact_keys(normalized_payload, required={"document_id"})
        document_id = normalized_payload["document_id"]
        if not isinstance(document_id, str):
            raise InvalidFeatureParametersError("DXF document identifier must be a string")
        normalized_input_root = _validate_local_input_file(document_id, allowed_input_root)
    elif command in {ProcessWorkerCommand.DXF_SUMMARY, ProcessWorkerCommand.DXF_MODEL}:
        _require_exact_keys(normalized_payload, required={"request", "tolerance"})
        request = _strict_contract(DrawingReadRequest, normalized_payload["request"])
        _strict_tolerance(normalized_payload["tolerance"])
        if (
            request.source.kind != "file"
            or request.source.format.strip().lower().lstrip(".") != "dxf"
        ):
            raise InvalidFeatureParametersError("DXF worker requires one local DXF source")
        normalized_input_root = _validate_local_input_file(request.source.ref, allowed_input_root)
    elif command is ProcessWorkerCommand.LOAD_MATERIAL_TABLE:
        _require_exact_keys(normalized_payload, required={"profile_ref"})
        if not isinstance(normalized_payload["profile_ref"], str):
            raise InvalidFeatureParametersError("Material profile reference must be a string")
    elif command is ProcessWorkerCommand.COMPUTE_TAKEOFF:
        _require_exact_keys(
            normalized_payload,
            required={"model", "request", "materials", "tolerance"},
        )
        _strict_contract(DrawingModel, normalized_payload["model"])
        _strict_contract(TakeoffRequest, normalized_payload["request"])
        _strict_contract(MaterialTable, normalized_payload["materials"])
        _strict_tolerance(normalized_payload["tolerance"])
    elif command is ProcessWorkerCommand.MEASURE:
        _require_exact_keys(
            normalized_payload,
            required={"model", "request", "tolerance"},
        )
        _strict_contract(DrawingModel, normalized_payload["model"])
        _strict_contract(MeasurementRequest, normalized_payload["request"])
        _strict_tolerance(normalized_payload["tolerance"])
    elif command is ProcessWorkerCommand.BRIDGE_REQUEST:
        _require_exact_keys(
            normalized_payload,
            required={"pipe_name", "envelope", "timeout_seconds", "max_frame_bytes"},
        )
        if not isinstance(normalized_payload["pipe_name"], str):
            raise InvalidFeatureParametersError("Bridge pipe name must be a string")
        if not isinstance(normalized_payload["envelope"], dict):
            raise InvalidFeatureParametersError("Bridge envelope must be a JSON object")
        _require_duration(normalized_payload["timeout_seconds"])
        max_frame_bytes = normalized_payload["max_frame_bytes"]
        if (
            isinstance(max_frame_bytes, bool)
            or not isinstance(max_frame_bytes, int)
            or not 4 <= max_frame_bytes <= MAX_ENVELOPE_BYTES
        ):
            raise InvalidFeatureParametersError("Bridge frame limit is out of range")
    request_json = _encode_json(normalized_payload)
    if len(request_json) > MAX_REQUEST_BYTES:
        raise InvalidFeatureParametersError("Process worker JSON payload is too large")
    return (
        request_json,
        normalized_payload,
        normalized_output_root,
        normalized_input_root,
    )


def _worker_main(
    sender: Connection,
    command_value: str,
    request_json: bytes,
    allowed_output_root: str | None,
    allowed_input_root: str | None,
) -> None:
    try:
        command = ProcessWorkerCommand(command_value)
        payload = _decode_json_object(request_json)
        result = _execute_command(command, payload, allowed_output_root, allowed_input_root)
        response: dict[str, JsonValue] = {
            "ok": True,
            "command": command.value,
            "result": result,
        }
    except Exception:
        # Never serialize exception objects, messages, paths, tracebacks, or reprs.
        response = {
            "ok": False,
            "command": command_value,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Isolated process worker failed",
                "retryable": False,
                "required_action": None,
                "details": {},
            },
        }
    try:
        sender.send_bytes(_encode_json(response))
    except Exception:
        # The parent treats a missing envelope as a sanitized INTERNAL_ERROR.
        pass
    finally:
        sender.close()


def _execute_command(
    command: ProcessWorkerCommand,
    payload: dict[str, JsonValue],
    allowed_output_root: str | None,
    allowed_input_root: str | None,
) -> dict[str, JsonValue]:
    if command is ProcessWorkerCommand.ECHO_JSON:
        return {"value": payload["value"]}
    if command is ProcessWorkerCommand.SLEEP:
        sleep(cast(float, payload["seconds"]))
        return cast(dict[str, JsonValue], payload.get("result", {}))
    if command is ProcessWorkerCommand.WRITE_JSON_MARKER:
        if allowed_output_root is None:
            raise ValueError("missing allowlisted root")
        sleep(cast(float, payload["delay_seconds"]))
        root = Path(allowed_output_root).resolve(strict=True)
        filename = cast(str, payload["filename"])
        target = root / filename
        # Re-check in the child immediately before the only filesystem side effect.
        if target.parent.resolve(strict=True) != root or target.exists():
            raise ValueError("invalid marker target")
        target.write_bytes(_encode_json(payload["document"]))
        return {"filename": filename, "written": True}
    if command is ProcessWorkerCommand.DXF_CURRENT_REVISION:
        from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader

        document_id = cast(str, payload["document_id"])
        _recheck_local_input_file(document_id, allowed_input_root)
        return {"revision": DxfDrawingReader().current_revision(document_id)}
    if command in {ProcessWorkerCommand.DXF_SUMMARY, ProcessWorkerCommand.DXF_MODEL}:
        from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader

        request = _strict_contract(DrawingReadRequest, payload["request"])
        _recheck_local_input_file(request.source.ref, allowed_input_root)
        reader = DxfDrawingReader(_strict_tolerance(payload["tolerance"]))
        if command is ProcessWorkerCommand.DXF_SUMMARY:
            summary = reader.summarize(request)
            return {"summary": cast(JsonValue, summary.model_dump(mode="json"))}
        model = reader.read(request)
        return {"model": cast(JsonValue, model.model_dump(mode="json"))}
    if command is ProcessWorkerCommand.LOAD_MATERIAL_TABLE:
        from cad_harness.company_rules.material_loader import load_material_table

        materials = load_material_table(cast(str, payload["profile_ref"]))
        return {"materials": cast(JsonValue, materials.model_dump(mode="json"))}
    if command is ProcessWorkerCommand.COMPUTE_TAKEOFF:
        from cad_harness.comprehension.takeoff import compute_takeoff

        report = compute_takeoff(
            _strict_contract(DrawingModel, payload["model"]),
            _strict_contract(TakeoffRequest, payload["request"]),
            materials=_strict_contract(MaterialTable, payload["materials"]),
            tolerance=_strict_tolerance(payload["tolerance"]),
        )
        return {"report": cast(JsonValue, report.model_dump(mode="json"))}
    if command is ProcessWorkerCommand.MEASURE:
        from cad_harness.application.services.measurement_service import MeasurementService

        result = MeasurementService().measure(
            _strict_contract(DrawingModel, payload["model"]),
            _strict_contract(MeasurementRequest, payload["request"]),
            tolerance=_strict_tolerance(payload["tolerance"]),
        )
        return {"measurement": cast(JsonValue, result.model_dump(mode="json"))}
    if command is ProcessWorkerCommand.BRIDGE_REQUEST:
        from cad_harness.adapters.named_pipe_transport import (
            NamedPipeTransport,
            TerminalCancellationUnconfirmedError,
        )
        from cad_harness.domain.errors import IpcTimeoutError

        try:
            transport = NamedPipeTransport(
                cast(str, payload["pipe_name"]),
                max_frame_bytes=cast(int, payload["max_frame_bytes"]),
            )
            response = transport.request(
                cast(dict[str, Any], payload["envelope"]),
                timeout_seconds=float(cast(int | float, payload["timeout_seconds"])),
            )
            return {
                "transport_ok": True,
                "response": cast(JsonValue, response),
                "server_process_id": transport.last_server_process_id,
            }
        except TerminalCancellationUnconfirmedError as error:
            return {
                "transport_ok": False,
                "error_type": "terminal_cancellation_unconfirmed",
                "details": cast(JsonValue, error.details),
            }
        except IpcTimeoutError as error:
            return {
                "transport_ok": False,
                "error_type": "ipc_timeout",
                "details": cast(JsonValue, error.details),
            }
        except Exception:
            return {"transport_ok": False, "error_type": "transport_error", "details": {}}
    raise ValueError("unreachable command")


def run_bridge_request_on_registered_broker(
    *,
    pipe_name: str,
    envelope: dict[str, Any],
    timeout_seconds: float,
    max_frame_bytes: int,
) -> dict[str, JsonValue] | None:
    """Route bridge I/O through the process prestarted before Windows STDIO.

    FastMCP's active Windows STDIO wrappers can prevent a new Named Pipe client handle
    from becoming usable even though the endpoint is healthy. The existing terminal
    process broker avoids that runtime interaction and is absent in CLI/test callers.
    """
    if _registered_broker() is None:
        return None
    from cad_harness.application.timeout import OperationDeadline

    deadline = OperationDeadline(timeout_seconds + 1.0, "bridge_ipc_broker")
    return run_process_worker(
        deadline,
        ProcessWorkerCommand.BRIDGE_REQUEST,
        {
            "pipe_name": pipe_name,
            "envelope": cast(JsonValue, envelope),
            "timeout_seconds": timeout_seconds,
            "max_frame_bytes": max_frame_bytes,
        },
    )


def _terminate_and_join(process: Any) -> None:
    if process.is_alive():
        process.terminate()
        process.join(_TERMINATE_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join()
    else:
        process.join()


def _strict_contract[ModelT: BaseModel](model_type: type[ModelT], value: JsonValue) -> ModelT:
    """Reconstruct a wire contract without Python-side coercion or unknown fields."""

    if not isinstance(value, dict):
        raise InvalidFeatureParametersError("Process worker contract must be a JSON object")
    try:
        return model_type.model_validate_json(_encode_json(value), strict=True)
    except (ValidationError, ValueError, TypeError) as exc:
        raise InvalidFeatureParametersError("Process worker contract is invalid") from exc


def _strict_tolerance(value: JsonValue) -> ToleranceProfile:
    if not isinstance(value, dict):
        raise InvalidFeatureParametersError("Process worker tolerance must be a JSON object")
    try:
        model = _ToleranceInput.model_validate_json(_encode_json(value), strict=True)
    except (ValidationError, ValueError, TypeError) as exc:
        raise InvalidFeatureParametersError("Process worker tolerance is invalid") from exc
    return model.to_profile()


def _validate_local_input_file(document_id: str, allowed_input_root: Path | None) -> str:
    """Validate one read-only file and return only its normalized allowlisted root."""

    if allowed_input_root is None:
        raise InvalidFeatureParametersError("DXF command requires an allowlisted input root")
    if not isinstance(allowed_input_root, Path):
        raise InvalidFeatureParametersError("Allowlisted input root must be a path")
    try:
        root = allowed_input_root.resolve(strict=True)
        source = Path(document_id)
        if (
            not root.is_dir()
            or _is_reparse_point(allowed_input_root)
            or not source.is_file()
            or _is_reparse_point(source)
        ):
            raise ValueError("invalid local input")
        resolved_source = source.resolve(strict=True)
        if resolved_source == root or not resolved_source.is_relative_to(root):
            raise ValueError("input is outside allowlist")
    except (OSError, RuntimeError, ValueError) as exc:
        raise InvalidFeatureParametersError("DXF input is not an allowlisted local file") from exc
    return str(root)


def _recheck_local_input_file(document_id: str, allowed_input_root: str | None) -> None:
    """Close parent/child TOCTOU gaps immediately before the child opens a DXF."""

    if allowed_input_root is None:
        raise ValueError("missing allowlisted input root")
    root = Path(allowed_input_root)
    source = Path(document_id)
    if (
        not root.is_dir()
        or _is_reparse_point(root)
        or not source.is_file()
        or _is_reparse_point(source)
    ):
        raise ValueError("invalid local input")
    resolved_root = root.resolve(strict=True)
    resolved_source = source.resolve(strict=True)
    if resolved_source == resolved_root or not resolved_source.is_relative_to(resolved_root):
        raise ValueError("input is outside allowlist")


def _is_reparse_point(path: Path) -> bool:
    try:
        stat = path.lstat()
    except OSError:
        return True
    reparse_flag = getattr(stat, "st_file_attributes", 0) & 0x400
    return path.is_symlink() or bool(reparse_flag)


def _encode_json(value: JsonValue | dict[str, Any]) -> bytes:
    try:
        _validate_json_value(value, depth=0)
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidFeatureParametersError(
            "Process worker payload must contain bounded JSON values"
        ) from exc
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise InvalidFeatureParametersError("Process worker JSON envelope is too large")
    return encoded


def _decode_json_object(encoded: bytes) -> dict[str, JsonValue]:
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise InvalidFeatureParametersError("Process worker JSON envelope is too large")
    try:
        decoded = json.loads(encoded)
        _validate_json_value(decoded, depth=0)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise InvalidFeatureParametersError("Process worker returned invalid JSON") from exc
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise InvalidFeatureParametersError("Process worker JSON envelope must be an object")
    return cast(dict[str, JsonValue], decoded)


def _validate_json_value(value: Any, *, depth: int) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("JSON nesting is too deep")
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    raise TypeError("not a JSON value")


def _require_exact_keys(
    payload: Mapping[str, JsonValue],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    if set(payload) - allowed or not required.issubset(payload):
        raise InvalidFeatureParametersError(
            "Process worker payload has missing or unknown fields",
            details={
                "required_fields": sorted(required),
                "optional_fields": sorted(optional or ()),
            },
        )


def _require_duration(value: JsonValue) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFeatureParametersError("Process worker duration must be a number")
    if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 86_400.0:
        raise InvalidFeatureParametersError("Process worker duration is out of range")


def _unwrap_response(
    envelope: dict[str, JsonValue],
    command: ProcessWorkerCommand,
    request_payload: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    del request_payload  # reserved for future request/result correlation fields
    if envelope.get("command") != command.value or not isinstance(envelope.get("ok"), bool):
        raise HarnessError(
            "Isolated process worker returned an invalid envelope",
            details={"command": command.value},
        )
    if envelope["ok"] is True:
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise HarnessError(
                "Isolated process worker returned an invalid result",
                details={"command": command.value},
            )
        return result
    raise HarnessError(
        "Isolated process worker failed",
        details={"command": command.value, "worker_terminal": True},
    )


__all__ = [
    "JsonValue",
    "ProcessWorkerCommand",
    "prestarted_process_worker_broker",
    "run_bridge_request_on_registered_broker",
    "run_process_worker",
]
