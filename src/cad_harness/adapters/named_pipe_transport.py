"""Deadline-safe Windows Named Pipe transport for the AutoCAD bridge.

The high-level transport is deliberately independent from pywin32.  Unit tests use an
in-memory driver and importing/constructing the transport never imports a Windows module.
The production driver opens every handle with ``FILE_FLAG_OVERLAPPED`` and owns pending
I/O until it has reached a terminal state.
"""

from __future__ import annotations

import importlib
import math
import re
import struct
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from cad_harness.adapters.dotnet_bridge import MAX_FRAME_BYTES, decode_frame, encode_frame
from cad_harness.domain.errors import AdapterCapabilityMissingError, IpcTimeoutError
from cad_harness.domain.models.base import SCHEMA_VERSION

_LOCAL_PIPE_PREFIX = "\\\\.\\pipe\\"
_LENGTH_PREFIX = struct.Struct(">I")
_WINDOWS_SID = re.compile(r"^S-\d+(?:-\d+)+$")


def resolve_current_user_pipe_name(
    template: str,
    *,
    sid_resolver: Callable[[], str] | None = None,
) -> str:
    """Resolve a configured pipe leaf to the exact current Windows account SID.

    The resolver is lazy so fake/DXF operation never imports pywin32.  Configuration
    must retain the ``{user_sid}`` placeholder; accepting a shared literal here would
    silently defeat the bridge's per-user ACL boundary.
    """
    if "{user_sid}" not in template:
        raise ValueError("Named Pipe template must contain the '{user_sid}' placeholder")
    if template.lower().startswith(_LOCAL_PIPE_PREFIX.lower()):
        leaf_template = template[len(_LOCAL_PIPE_PREFIX) :]
    else:
        leaf_template = template

    if sid_resolver is None:
        sid_resolver = _current_user_sid
    sid = sid_resolver()
    if not _WINDOWS_SID.fullmatch(sid):
        raise ValueError("Current user SID has an invalid canonical form")
    pipe_name = _LOCAL_PIPE_PREFIX + leaf_template.replace("{user_sid}", sid)
    NamedPipeTransport._validate_local_pipe_name(pipe_name)
    return pipe_name


def _current_user_sid() -> str:
    if sys.platform != "win32":
        raise AdapterCapabilityMissingError(
            "Per-user Named Pipe resolution is only available on Windows",
            required_action="Run the .NET bridge adapter on a supported Windows host",
            details={"platform": sys.platform},
        )
    win32api: Any = importlib.import_module("win32api")
    win32con: Any = importlib.import_module("win32con")
    win32security: Any = importlib.import_module("win32security")
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        return str(win32security.ConvertSidToStringSid(sid))
    finally:
        token.Close()


class NamedPipeDeadlineExceededError(TimeoutError):
    """A low-level pipe operation did not finish by its strict deadline."""


class TerminalCancellationUnconfirmedError(IpcTimeoutError):
    """The original deadline expired and bridge termination was not confirmed.

    This remains an ``IpcTimeoutError`` so callers retain the transport error contract,
    but it is deliberately non-retryable.  In particular, writer callers must reconcile
    an unknown commit outcome instead of assuming that closing the client handle stopped
    execution in AutoCAD.
    """

    retryable = False


class NamedPipeDriver(Protocol):
    """Injectable low-level pipe API.

    ``cancel_pending`` followed by ``wait_pending_terminal`` is mandatory before a timed
    out handle is closed.  This mirrors the CancelIoEx completion contract on Windows.
    """

    def connect(
        self,
        pipe_name: str,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> object: ...

    def write(
        self,
        handle: object,
        data: bytes,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> None: ...

    def read_exact(
        self,
        handle: object,
        size: int,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> bytes: ...

    def cancel_pending(self, handle: object) -> None: ...

    def wait_pending_terminal(
        self,
        handle: object,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> None: ...

    def close(self, handle: object) -> None: ...


@dataclass(slots=True)
class _PendingIo:
    overlapped: Any
    event: Any
    buffer: Any = None


class Win32NamedPipeDriver:
    """pywin32 implementation; all platform imports are lazy and instance-local."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise AdapterCapabilityMissingError(
                "Windows Named Pipe transport is only available on Windows",
                required_action="Run the .NET bridge adapter on a supported Windows host",
                details={"platform": sys.platform},
            )

        # Lazy imports are a hard boundary: fake/DXF adapters never load pywin32 and never
        # attempt to connect to AutoCAD merely because this module was imported.
        self._pywintypes: Any = importlib.import_module("pywintypes")
        self._win32api: Any = importlib.import_module("win32api")
        self._win32event: Any = importlib.import_module("win32event")
        self._win32file: Any = importlib.import_module("win32file")
        self._win32pipe: Any = importlib.import_module("win32pipe")
        ctypes: Any = importlib.import_module("ctypes")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._cancel_io_ex: Any = kernel32.CancelIoEx
        self._cancel_io_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._cancel_io_ex.restype = ctypes.c_int
        self._ctypes = ctypes
        self._pending: dict[int, list[_PendingIo]] = {}

    @staticmethod
    def _handle_key(handle: object) -> int:
        return id(handle)

    @staticmethod
    def _remaining_milliseconds(deadline: float, clock: Callable[[], float]) -> int:
        now = clock()
        if now > deadline:
            raise NamedPipeDeadlineExceededError
        return max(0, math.ceil((deadline - now) * 1000.0))

    def connect(
        self,
        pipe_name: str,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> object:
        wait_ms = self._remaining_milliseconds(deadline, clock)
        try:
            self._win32pipe.WaitNamedPipe(pipe_name, wait_ms)
            handle = self._win32file.CreateFile(
                pipe_name,
                self._win32file.GENERIC_READ | self._win32file.GENERIC_WRITE,
                0,
                None,
                self._win32file.OPEN_EXISTING,
                self._win32file.FILE_FLAG_OVERLAPPED,
                None,
            )
        except Exception as error:
            if clock() > deadline or getattr(error, "winerror", None) in {121, 258}:
                raise NamedPipeDeadlineExceededError from error
            raise
        if clock() > deadline:
            self.close(handle)
            raise NamedPipeDeadlineExceededError
        return handle

    def _new_overlapped(self) -> tuple[Any, Any]:
        event = self._win32event.CreateEvent(None, True, False, None)
        overlapped = self._pywintypes.OVERLAPPED()
        overlapped.hEvent = event
        return overlapped, event

    def _wait_io(
        self,
        handle: object,
        pending: _PendingIo,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> int:
        wait_ms = self._remaining_milliseconds(deadline, clock)
        result = self._win32event.WaitForSingleObject(pending.event, wait_ms)
        if result == self._win32event.WAIT_TIMEOUT:
            self._pending.setdefault(self._handle_key(handle), []).append(pending)
            raise NamedPipeDeadlineExceededError
        if result != self._win32event.WAIT_OBJECT_0:
            self._win32api.CloseHandle(pending.event)
            raise OSError(f"Unexpected Named Pipe wait result: {result}")
        transferred = int(self._win32file.GetOverlappedResult(handle, pending.overlapped, False))
        self._win32api.CloseHandle(pending.event)
        if clock() > deadline:
            raise NamedPipeDeadlineExceededError
        return transferred

    def write(
        self,
        handle: object,
        data: bytes,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        offset = 0
        while offset < len(data):
            overlapped, event = self._new_overlapped()
            error_code, written = self._win32file.WriteFile(handle, data[offset:], overlapped)
            pending = _PendingIo(overlapped=overlapped, event=event)
            if error_code == 997:  # ERROR_IO_PENDING
                count = self._wait_io(handle, pending, deadline=deadline, clock=clock)
            elif error_code == 0:
                count = int(written)
                self._win32api.CloseHandle(event)
                if clock() > deadline:
                    raise NamedPipeDeadlineExceededError
            else:
                self._win32api.CloseHandle(event)
                raise OSError(f"WriteFile failed with Windows error {error_code}")
            if count <= 0:
                raise OSError("Named Pipe write completed without progress")
            offset += count

    def read_exact(
        self,
        handle: object,
        size: int,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            overlapped, event = self._new_overlapped()
            error_code, buffer = self._win32file.ReadFile(handle, remaining, overlapped)
            pending = _PendingIo(overlapped=overlapped, event=event, buffer=buffer)
            if error_code == 997:  # ERROR_IO_PENDING
                count = self._wait_io(handle, pending, deadline=deadline, clock=clock)
            elif error_code == 0:
                count = len(buffer)
                self._win32api.CloseHandle(event)
                if clock() > deadline:
                    raise NamedPipeDeadlineExceededError
            else:
                self._win32api.CloseHandle(event)
                raise OSError(f"ReadFile failed with Windows error {error_code}")
            if count <= 0:
                raise EOFError("Named Pipe closed before the complete frame was received")
            chunk = bytes(buffer)[:count]
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def cancel_pending(self, handle: object) -> None:
        # Each connection carries exactly one request, so NULL intentionally cancels all
        # overlapped operations for this handle without affecting the control connection.
        untyped_handle: Any = handle
        succeeded = self._cancel_io_ex(self._ctypes.c_void_p(int(untyped_handle)), None)
        if not succeeded:
            error_code = int(self._ctypes.get_last_error())
            # ERROR_NOT_FOUND means the I/O became terminal before cancellation won the
            # race.  Its event is still observed by wait_pending_terminal below.
            if error_code != 1168:
                raise OSError(error_code, "CancelIoEx failed")

    def wait_pending_terminal(
        self,
        handle: object,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        pending_items = self._pending.pop(self._handle_key(handle), [])
        for index, pending in enumerate(pending_items):
            wait_ms = self._remaining_milliseconds(deadline, clock)
            result = self._win32event.WaitForSingleObject(pending.event, wait_ms)
            if result == self._win32event.WAIT_TIMEOUT:
                # Retain the OVERLAPPED and event objects so a pathological driver that
                # ignores CancelIoEx cannot cause use-after-free.  The transport still
                # closes the pipe handle and returns fail-closed instead of hanging.
                self._pending[self._handle_key(handle)] = pending_items[index:]
                raise NamedPipeDeadlineExceededError
            if result != self._win32event.WAIT_OBJECT_0:
                self._win32api.CloseHandle(pending.event)
                raise OSError(f"Unexpected Named Pipe cancellation wait result: {result}")
            try:
                self._win32file.GetOverlappedResult(handle, pending.overlapped, False)
            except Exception as error:
                if getattr(error, "winerror", None) not in {995, 996}:
                    self._win32api.CloseHandle(pending.event)
                    raise
            self._win32api.CloseHandle(pending.event)

    def close(self, handle: object) -> None:
        self._win32file.CloseHandle(handle)


class NamedPipeTransport:
    """One-request-per-connection bridge transport with terminal cancellation."""

    def __init__(
        self,
        pipe_name: str,
        *,
        driver: NamedPipeDriver | None = None,
        clock: Callable[[], float] = time.monotonic,
        request_id_factory: Callable[[], str] | None = None,
        cancellation_grace_seconds: float = 2.0,
        cancellation_retry_seconds: float = 0.05,
        sleeper: Callable[[float], None] = time.sleep,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        self._validate_local_pipe_name(pipe_name)
        if cancellation_grace_seconds <= 0:
            raise ValueError("cancellation_grace_seconds must be positive")
        if cancellation_retry_seconds <= 0:
            raise ValueError("cancellation_retry_seconds must be positive")
        if not 0 < max_frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError(f"max_frame_bytes must be in [1, {MAX_FRAME_BYTES}]")
        self.pipe_name = pipe_name
        self._driver = driver
        self._clock = clock
        self._request_id_factory = request_id_factory or (lambda: str(uuid.uuid4()))
        self._cancellation_grace_seconds = cancellation_grace_seconds
        self._cancellation_retry_seconds = cancellation_retry_seconds
        self._sleeper = sleeper
        self._max_frame_bytes = max_frame_bytes

    @staticmethod
    def _validate_local_pipe_name(pipe_name: str) -> None:
        if not pipe_name.lower().startswith(_LOCAL_PIPE_PREFIX.lower()):
            raise ValueError(r"Named Pipe must use the local \\.\pipe\ namespace")
        leaf = pipe_name[len(_LOCAL_PIPE_PREFIX) :]
        if not leaf or "\\" in leaf or "/" in leaf or leaf in {".", ".."}:
            raise ValueError("Named Pipe name must be one non-empty local pipe segment")

    def _get_driver(self) -> NamedPipeDriver:
        if self._driver is None:
            self._driver = Win32NamedPipeDriver()
        return self._driver

    def request(self, envelope: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        """Send one envelope and return its matching schema-1.10 response.

        Timeout is strict: an operation completing exactly at the deadline succeeds;
        completion even one monotonic tick after it triggers terminal cancellation.
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        request_id = envelope.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("IPC request requires a non-empty request_id")
        if envelope.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"IPC request schema_version must be {SCHEMA_VERSION}")

        request_frame = self._encode_checked(envelope)
        driver = self._get_driver()
        deadline = self._clock() + timeout_seconds
        handle: object | None = None
        try:
            handle = driver.connect(self.pipe_name, deadline=deadline, clock=self._clock)
            self._check_deadline(deadline)
            driver.write(handle, request_frame, deadline=deadline, clock=self._clock)
            self._check_deadline(deadline)
            response = self._read_response(driver, handle, deadline)
            self._check_deadline(deadline)
            self._validate_response(response, request_id=request_id)
        except NamedPipeDeadlineExceededError:
            primary_cancel_error: str | None = None
            if handle is not None:
                try:
                    self._cancel_wait_close(
                        driver,
                        handle,
                        deadline=self._clock() + self._cancellation_grace_seconds,
                    )
                except Exception as error:
                    primary_cancel_error = type(error).__name__
                handle = None
            confirmed, cancel_error = self._request_terminal_cancel(driver, request_id)
            details: dict[str, Any] = {
                "operation": envelope.get("method", "ipc_request"),
                "request_id": request_id,
                "timeout_seconds": timeout_seconds,
                "terminal_cancel_confirmed": confirmed,
            }
            if cancel_error is not None:
                details["cancellation_error"] = cancel_error
            if primary_cancel_error is not None:
                details["primary_io_cancel_error"] = primary_cancel_error
            error_type = IpcTimeoutError if confirmed else TerminalCancellationUnconfirmedError
            message = (
                "Bridge request exceeded its configured deadline"
                if confirmed
                else "Bridge request timed out and terminal cancellation was not confirmed"
            )
            raise error_type(message, details=details) from None
        finally:
            if handle is not None:
                driver.close(handle)
        return response

    def _check_deadline(self, deadline: float) -> None:
        if self._clock() > deadline:
            raise NamedPipeDeadlineExceededError

    def _encode_checked(self, envelope: dict[str, Any]) -> bytes:
        frame = encode_frame(envelope)
        if len(frame) - _LENGTH_PREFIX.size > self._max_frame_bytes:
            raise ValueError(f"Request frame exceeds maximum {self._max_frame_bytes}")
        return frame

    def _read_response(
        self,
        driver: NamedPipeDriver,
        handle: object,
        deadline: float,
    ) -> dict[str, Any]:
        prefix = driver.read_exact(
            handle,
            _LENGTH_PREFIX.size,
            deadline=deadline,
            clock=self._clock,
        )
        (body_length,) = _LENGTH_PREFIX.unpack(prefix)
        if body_length > self._max_frame_bytes:
            raise ValueError(
                f"Declared frame length {body_length} exceeds maximum {self._max_frame_bytes}"
            )
        body = driver.read_exact(handle, body_length, deadline=deadline, clock=self._clock)
        return decode_frame(prefix + body)

    @staticmethod
    def _validate_response(response: dict[str, Any], *, request_id: str) -> None:
        if response.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Bridge response schema_version does not match the client contract")
        if response.get("request_id") != request_id:
            raise ValueError("Bridge response request_id does not match the request")

    def _cancel_wait_close(
        self,
        driver: NamedPipeDriver,
        handle: object,
        *,
        deadline: float,
    ) -> None:
        try:
            driver.cancel_pending(handle)
            driver.wait_pending_terminal(handle, deadline=deadline, clock=self._clock)
        finally:
            driver.close(handle)

    def _request_terminal_cancel(
        self,
        driver: NamedPipeDriver,
        target_request_id: str,
    ) -> tuple[bool, str | None]:
        # A cancel control request can beat registration of the primary request in the
        # bridge's active-request registry.  Every attempt shares this one monotonic
        # grace deadline; a target-not-active or terminal=false response is therefore
        # only an unconfirmed observation, never cancellation success.
        deadline = self._clock() + self._cancellation_grace_seconds
        maximum_attempts = max(
            1,
            math.ceil(self._cancellation_grace_seconds / self._cancellation_retry_seconds) + 1,
        )
        last_error = "terminal acknowledgement was not confirmed"
        for _attempt in range(maximum_attempts):
            if self._clock() > deadline:
                break
            cancel_request_id = self._request_id_factory()
            if not cancel_request_id or cancel_request_id == target_request_id:
                cancel_request_id = str(uuid.uuid4())
            cancel_envelope = {
                "schema_version": SCHEMA_VERSION,
                "method": "cancel",
                "request_id": cancel_request_id,
                "job_id": None,
                "idempotency_key": None,
                "params": {"target_request_id": target_request_id},
            }
            handle: object | None = None
            try:
                handle = driver.connect(self.pipe_name, deadline=deadline, clock=self._clock)
                driver.write(
                    handle,
                    self._encode_checked(cancel_envelope),
                    deadline=deadline,
                    clock=self._clock,
                )
                response = self._read_response(driver, handle, deadline)
                self._check_deadline(deadline)
                self._validate_response(response, request_id=cancel_request_id)
                data = response.get("data")
                confirmed = (
                    response.get("status") == "ok"
                    and isinstance(data, dict)
                    and data.get("terminal") is True
                    and data.get("cancelled_request_id") == target_request_id
                )
                if confirmed:
                    return True, None
                last_error = "terminal acknowledgement was not confirmed"
            except NamedPipeDeadlineExceededError:
                last_error = NamedPipeDeadlineExceededError.__name__
                if handle is not None:
                    timed_out_handle = handle
                    handle = None
                    try:
                        self._cancel_wait_close(
                            driver,
                            timed_out_handle,
                            deadline=deadline,
                        )
                    except Exception as cancellation_error:
                        return False, type(cancellation_error).__name__
                break
            except Exception as error:
                # Protocol-invalid responses are not a registration race and cannot be
                # trusted.  Fail closed immediately; transient pipe failures may use the
                # remaining grace window for another bounded attempt.
                last_error = type(error).__name__
                if isinstance(error, (ValueError, TypeError)):
                    break
            finally:
                if handle is not None:
                    driver.close(handle)

            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            self._sleeper(min(self._cancellation_retry_seconds, remaining))

        return False, last_error


__all__ = [
    "NamedPipeDeadlineExceededError",
    "NamedPipeDriver",
    "NamedPipeTransport",
    "TerminalCancellationUnconfirmedError",
    "Win32NamedPipeDriver",
    "resolve_current_user_pipe_name",
]
