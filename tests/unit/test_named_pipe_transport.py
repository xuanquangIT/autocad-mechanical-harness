from __future__ import annotations

import builtins
import struct
import sys
from collections.abc import Callable
from typing import Any

import pytest

from cad_harness.adapters.dotnet_bridge import DEFAULT_PIPE_NAME, decode_frame, encode_frame
from cad_harness.adapters.named_pipe_transport import (
    NamedPipeDeadlineExceededError,
    NamedPipeTransport,
    TerminalCancellationUnconfirmedError,
    resolve_current_user_pipe_name,
)
from cad_harness.application import process_runner
from cad_harness.domain.errors import AdapterCapabilityMissingError, IpcTimeoutError
from cad_harness.domain.models.base import SCHEMA_VERSION


class VirtualClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class EventDriver:
    def __init__(
        self,
        clock: VirtualClock,
        primary_response: bytes,
        cancel_response_factory: Callable[[dict[str, Any]], bytes],
        *,
        primary_delay: float = 0.0,
        terminal_wait_delay: float = 0.0,
    ) -> None:
        self.clock = clock
        self.primary_response = primary_response
        self.cancel_response_factory = cancel_response_factory
        self.primary_delay = primary_delay
        self.terminal_wait_delay = terminal_wait_delay
        self.events: list[str] = []
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.connect_deadlines: list[tuple[str, float]] = []
        self._buffers: dict[str, bytearray] = {}
        self._connect_count = 0

    def connect(
        self,
        pipe_name: str,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> object:
        assert pipe_name == DEFAULT_PIPE_NAME
        self._connect_count += 1
        handle = f"handle-{self._connect_count}"
        self.events.append(f"connect:{handle}")
        self.connect_deadlines.append((handle, deadline))
        if clock() > deadline:
            raise NamedPipeDeadlineExceededError
        return handle

    def write(
        self,
        handle: object,
        data: bytes,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        name = str(handle)
        payload = decode_frame(data)
        self.events.append(f"write:{name}")
        self.writes.append((name, payload))
        if name == "handle-1":
            self._buffers[name] = bytearray(self.primary_response)
        else:
            self._buffers[name] = bytearray(self.cancel_response_factory(payload))
        if clock() > deadline:
            raise NamedPipeDeadlineExceededError

    def read_exact(
        self,
        handle: object,
        size: int,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> bytes:
        name = str(handle)
        self.events.append(f"read:{name}:{size}")
        if name == "handle-1" and self.primary_delay:
            self.clock.advance(self.primary_delay)
            self.primary_delay = 0.0
        if clock() > deadline:
            raise NamedPipeDeadlineExceededError
        buffer = self._buffers[name]
        result = bytes(buffer[:size])
        del buffer[:size]
        if len(result) != size:
            raise EOFError("Named Pipe closed before the requested bytes arrived")
        return result

    def cancel_pending(self, handle: object) -> None:
        self.events.append(f"cancel:{handle}")

    def wait_pending_terminal(
        self,
        handle: object,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        self.events.append(f"terminal:{handle}")
        if self.terminal_wait_delay:
            self.clock.advance(self.terminal_wait_delay)
            self.terminal_wait_delay = 0.0
        if clock() > deadline:
            raise NamedPipeDeadlineExceededError

    def close(self, handle: object) -> None:
        self.events.append(f"close:{handle}")


def _request(request_id: str = "request-1") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "method": "inspect_document",
        "request_id": request_id,
        "job_id": None,
        "idempotency_key": None,
        "params": {"document_id": "doc-1"},
    }


def _response(request_id: str = "request-1", *, schema: str = SCHEMA_VERSION) -> bytes:
    return encode_frame(
        {
            "schema_version": schema,
            "request_id": request_id,
            "status": "ok",
            "data": {"revision": "sha256:test"},
        }
    )


def _cancel_ack(payload: dict[str, Any]) -> bytes:
    return encode_frame(
        {
            "schema_version": SCHEMA_VERSION,
            "request_id": payload["request_id"],
            "status": "ok",
            "data": {
                "terminal": True,
                "cancelled_request_id": payload["params"]["target_request_id"],
            },
        }
    )


@pytest.mark.parametrize(
    ("duration", "times_out"),
    [(0.999, False), (1.0, False), (1.001, True)],
)
def test_strict_monotonic_deadline_boundary(duration: float, times_out: bool) -> None:
    clock = VirtualClock()
    driver = EventDriver(clock, _response(), _cancel_ack, primary_delay=duration)
    transport = NamedPipeTransport(
        DEFAULT_PIPE_NAME,
        driver=driver,
        clock=clock,
        request_id_factory=lambda: "cancel-1",
    )

    if not times_out:
        assert transport.request(_request(), timeout_seconds=1.0)["status"] == "ok"
        assert not any(event.startswith("cancel:") for event in driver.events)
        return

    with pytest.raises(IpcTimeoutError) as captured:
        transport.request(_request(), timeout_seconds=1.0)
    assert captured.value.details["terminal_cancel_confirmed"] is True
    assert captured.value.details["transport_stage"] == "read_response"


def test_timeout_cancels_waits_and_closes_before_exact_control_request() -> None:
    clock = VirtualClock()
    driver = EventDriver(clock, _response(), _cancel_ack, primary_delay=1.01)
    transport = NamedPipeTransport(
        DEFAULT_PIPE_NAME,
        driver=driver,
        clock=clock,
        request_id_factory=lambda: "fresh-cancel-request",
    )

    with pytest.raises(IpcTimeoutError) as captured:
        transport.request(_request("target-request"), timeout_seconds=1.0)

    assert captured.value.code.value == "IPC_TIMEOUT"
    assert captured.value.details["terminal_cancel_confirmed"] is True
    assert driver.events.index("cancel:handle-1") < driver.events.index("terminal:handle-1")
    assert driver.events.index("terminal:handle-1") < driver.events.index("close:handle-1")
    assert driver.events.index("close:handle-1") < driver.events.index("connect:handle-2")
    cancel_payload = driver.writes[-1][1]
    assert cancel_payload == {
        "schema_version": "1.12",
        "method": "cancel",
        "request_id": "fresh-cancel-request",
        "job_id": None,
        "idempotency_key": None,
        "params": {"target_request_id": "target-request"},
    }
    assert driver.events[-1] == "close:handle-2"


def test_prestarted_stdio_broker_routes_response_without_direct_win32_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = VirtualClock()
    driver = EventDriver(clock, _response(), _cancel_ack)
    transport = NamedPipeTransport(DEFAULT_PIPE_NAME, driver=driver, clock=clock)
    broker_calls: list[dict[str, Any]] = []

    def broker(**kwargs: Any) -> dict[str, Any]:
        broker_calls.append(kwargs)
        return {"transport_ok": True, "response": decode_frame(_response())}

    monkeypatch.setattr(process_runner, "run_bridge_request_on_registered_broker", broker)

    assert transport.request(_request(), timeout_seconds=3.0)["status"] == "ok"
    assert driver.events == []
    assert broker_calls[0]["pipe_name"] == DEFAULT_PIPE_NAME
    assert broker_calls[0]["timeout_seconds"] == 3.0


def test_prestarted_stdio_broker_preserves_terminal_timeout_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = NamedPipeTransport(DEFAULT_PIPE_NAME, driver=object())  # type: ignore[arg-type]
    monkeypatch.setattr(
        process_runner,
        "run_bridge_request_on_registered_broker",
        lambda **_kwargs: {
            "transport_ok": False,
            "error_type": "terminal_cancellation_unconfirmed",
            "details": {"terminal_cancel_confirmed": False, "transport_stage": "read_response"},
        },
    )

    with pytest.raises(TerminalCancellationUnconfirmedError) as captured:
        transport.request(_request(), timeout_seconds=3.0)

    assert captured.value.retryable is False
    assert captured.value.details["terminal_cancel_confirmed"] is False


def test_primary_io_terminal_wait_is_bounded_and_control_cancel_still_runs() -> None:
    clock = VirtualClock()
    driver = EventDriver(
        clock,
        _response(),
        _cancel_ack,
        primary_delay=1.01,
        terminal_wait_delay=0.11,
    )
    transport = NamedPipeTransport(
        DEFAULT_PIPE_NAME,
        driver=driver,
        clock=clock,
        cancellation_grace_seconds=0.1,
    )

    with pytest.raises(IpcTimeoutError) as captured:
        transport.request(_request(), timeout_seconds=1.0)

    assert captured.value.details["terminal_cancel_confirmed"] is True
    assert captured.value.details["primary_io_cancel_error"] == ("NamedPipeDeadlineExceededError")
    assert "close:handle-1" in driver.events
    assert "connect:handle-2" in driver.events


def test_cancel_before_registration_retries_then_accepts_only_terminal_ack() -> None:
    responses = 0

    def registration_race_then_terminal(payload: dict[str, Any]) -> bytes:
        nonlocal responses
        responses += 1
        if responses == 1:
            return encode_frame(
                {
                    "schema_version": SCHEMA_VERSION,
                    "request_id": payload["request_id"],
                    "status": "rejected",
                    "error": {
                        "code": "INVALID_FEATURE_PARAMETERS",
                        "message": "The target request is not active.",
                    },
                }
            )
        return _cancel_ack(payload)

    clock = VirtualClock()
    driver = EventDriver(
        clock,
        _response(),
        registration_race_then_terminal,
        primary_delay=1.01,
    )
    request_ids = iter(("cancel-race-1", "cancel-race-2"))
    transport = NamedPipeTransport(
        DEFAULT_PIPE_NAME,
        driver=driver,
        clock=clock,
        request_id_factory=lambda: next(request_ids),
        cancellation_grace_seconds=0.5,
        cancellation_retry_seconds=0.1,
        sleeper=clock.advance,
    )

    with pytest.raises(IpcTimeoutError) as captured:
        transport.request(_request("race-target"), timeout_seconds=1.0)

    assert type(captured.value) is IpcTimeoutError
    assert captured.value.details["terminal_cancel_confirmed"] is True
    assert [payload["request_id"] for _, payload in driver.writes[1:]] == [
        "cancel-race-1",
        "cancel-race-2",
    ]
    assert {deadline for _, deadline in driver.connect_deadlines[1:]} == {101.51}
    assert driver.events[-1] == "close:handle-3"


def test_cancel_grace_exhaustion_is_non_retryable_unconfirmed_exception() -> None:
    def non_terminal_ack(payload: dict[str, Any]) -> bytes:
        return encode_frame(
            {
                "schema_version": SCHEMA_VERSION,
                "request_id": payload["request_id"],
                "status": "ok",
                "data": {
                    "terminal": False,
                    "cancelled_request_id": payload["params"]["target_request_id"],
                },
            }
        )

    clock = VirtualClock()
    driver = EventDriver(clock, _response(), non_terminal_ack, primary_delay=1.01)
    transport = NamedPipeTransport(
        DEFAULT_PIPE_NAME,
        driver=driver,
        clock=clock,
        cancellation_grace_seconds=0.25,
        cancellation_retry_seconds=0.1,
        sleeper=clock.advance,
    )

    with pytest.raises(TerminalCancellationUnconfirmedError) as captured:
        transport.request(_request(), timeout_seconds=1.0)

    assert captured.value.details["terminal_cancel_confirmed"] is False
    assert captured.value.retryable is False
    assert captured.value.details["cancellation_error"] == (
        "terminal acknowledgement was not confirmed"
    )
    assert len(driver.writes[1:]) == 4
    assert clock.value == pytest.approx(101.26)
    assert all(f"close:handle-{handle_number}" in driver.events for handle_number in range(2, 6))


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_response("wrong-request"), "request_id"),
        (_response(schema="9.0"), "schema_version"),
        (b"\x00\x00\x00\x05{}", "closed"),
        (struct.pack(">I", 257), "exceeds maximum"),
    ],
)
def test_malformed_mismatched_and_oversized_responses_are_rejected(
    response: bytes,
    message: str,
) -> None:
    clock = VirtualClock()
    driver = EventDriver(clock, response, _cancel_ack)
    transport = NamedPipeTransport(
        DEFAULT_PIPE_NAME,
        driver=driver,
        clock=clock,
        max_frame_bytes=256,
    )

    with pytest.raises((ValueError, EOFError), match=message):
        transport.request(_request(), timeout_seconds=1.0)
    assert driver.events[-1] == "close:handle-1"


def test_only_local_single_segment_pipe_names_are_accepted() -> None:
    for invalid in (
        r"\\server\pipe\cad-harness",
        "\\\\.\\pipe\\",
        r"\\.\pipe\nested\cad-harness",
        "cad-harness",
    ):
        with pytest.raises(ValueError):
            NamedPipeTransport(invalid, driver=object())  # type: ignore[arg-type]


def test_pipe_template_resolves_to_canonical_current_user_sid() -> None:
    assert (
        resolve_current_user_pipe_name(
            "cadharness.{user_sid}", sid_resolver=lambda: "S-1-5-21-123-456-789-1001"
        )
        == r"\\.\pipe\cadharness.S-1-5-21-123-456-789-1001"
    )

    with pytest.raises(ValueError, match="placeholder"):
        resolve_current_user_pipe_name("cadharness.shared", sid_resolver=lambda: "S-1-5-18")
    with pytest.raises(ValueError, match="canonical"):
        resolve_current_user_pipe_name(
            "cadharness.{user_sid}", sid_resolver=lambda: "not-a-windows-sid"
        )


def test_default_driver_fails_cleanly_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    transport = NamedPipeTransport(DEFAULT_PIPE_NAME)

    with pytest.raises(AdapterCapabilityMissingError, match="only available on Windows"):
        transport.request(_request(), timeout_seconds=1.0)


def test_constructing_transport_does_not_import_pywin32(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in {"pywintypes", "win32api", "win32event", "win32file", "win32pipe"}:
            raise AssertionError(f"eager Windows import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    NamedPipeTransport(DEFAULT_PIPE_NAME)
