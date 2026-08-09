"""Bounded retry policy for transient SQLite lock errors."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy.exc import OperationalError

from cad_harness.domain.errors import HarnessError

T = TypeVar("T")


def is_database_locked(error: OperationalError) -> bool:
    """Return whether SQLite reported its transient writer-lock condition."""
    return "database is locked" in str(error).lower()


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry lock failures without exceeding a deterministic attempt/time bound."""

    max_attempts: int = 5
    budget_seconds: float = 2.0
    initial_delay_seconds: float = 0.05
    backoff_multiplier: float = 2.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def run(self, operation: Callable[[], T]) -> T:
        """Run ``operation`` and retry only SQLite's lock error."""
        started_at = self.clock()
        for attempt in range(1, self.max_attempts + 1):
            try:
                return operation()
            except OperationalError as error:
                if not is_database_locked(error):
                    raise

                elapsed = max(0.0, self.clock() - started_at)
                if attempt >= self.max_attempts or elapsed >= self.budget_seconds:
                    self._raise_exhausted(attempt, elapsed)

                requested_delay = self.initial_delay_seconds * (
                    self.backoff_multiplier ** (attempt - 1)
                )
                delay = min(requested_delay, self.budget_seconds - elapsed)
                if delay <= 0.0:
                    self._raise_exhausted(attempt, elapsed)
                self.sleep(delay)

        raise AssertionError("retry loop exhausted without returning or raising")

    def _raise_exhausted(self, attempts: int, elapsed: float) -> None:
        raise HarnessError(
            "SQLite remained locked after the bounded retry policy was exhausted",
            required_action=(
                "Close other processes holding the harness database and retry the operation"
            ),
            details={
                "attempts": attempts,
                "elapsed_seconds": min(elapsed, self.budget_seconds),
            },
        )


DEFAULT_SQLITE_RETRY = RetryPolicy()
