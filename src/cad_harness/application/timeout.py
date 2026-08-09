"""Cooperative monotonic deadlines for bounded harness operations."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Lock, Timer
from time import monotonic

from cad_harness.domain.errors import IpcTimeoutError


class OperationDeadline:
    """Cancellation token plus a strict ``elapsed > timeout`` deadline.

    Python cannot safely kill a running thread. Application operations therefore call
    ``checkpoint`` between bounded stages and before every side effect. Bridge I/O must
    additionally close/cancel its OS transport when this token is cancelled.
    """

    def __init__(
        self,
        timeout_seconds: float,
        operation: str,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if timeout_seconds <= 0.0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self.operation = operation
        self._clock = clock
        self._started_at = clock()
        self._cancelled = Event()
        self._callback_lock = Lock()
        self._cancel_callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self._started_at)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.timeout_seconds - self.elapsed_seconds)

    def cancel(self) -> None:
        with self._callback_lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()
            callbacks = tuple(self._cancel_callbacks)
            self._cancel_callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # Cancellation is best effort across every registered resource. One
                # broken close callback must not prevent the remaining resources from
                # reaching a terminal state.
                continue

    def add_cancel_callback(self, callback: Callable[[], None]) -> None:
        """Register an idempotent resource/transport cancellation callback."""
        call_now = False
        with self._callback_lock:
            if self._cancelled.is_set():
                call_now = True
            else:
                self._cancel_callbacks.append(callback)
        if call_now:
            callback()

    def checkpoint(self) -> None:
        elapsed = self.elapsed_seconds
        if self.cancelled or elapsed > self.timeout_seconds:
            self.cancel()
            raise IpcTimeoutError(
                f"Operation '{self.operation}' exceeded its configured timeout",
                details={
                    "operation": self.operation,
                    "timeout_seconds": self.timeout_seconds,
                    "elapsed_seconds": elapsed,
                    "cancelled": True,
                },
            )


def run_with_deadline[T](
    deadline: OperationDeadline, operation: Callable[[OperationDeadline], T]
) -> T:
    """Run cooperative work and discard any result that arrives after the deadline."""
    deadline.checkpoint()
    result = operation(deadline)
    deadline.checkpoint()
    return result


def run_cancellable[T](
    deadline: OperationDeadline, operation: Callable[[OperationDeadline], T]
) -> T:
    """Run on the caller thread while a timer cancels registered resources.

    No operation worker is abandoned. Long-running ports must cooperate by registering
    their close/cancel callback and checking the token in bounded loops. Consequently,
    returning from this function proves the operation itself is terminal.
    """
    timer = Timer(deadline.timeout_seconds, deadline.cancel)
    timer.name = f"cad-harness-{deadline.operation}-deadline"
    timer.daemon = True
    timer.start()
    try:
        return run_with_deadline(deadline, operation)
    finally:
        timer.cancel()
        timer.join()


__all__ = ["OperationDeadline", "run_cancellable", "run_with_deadline"]
