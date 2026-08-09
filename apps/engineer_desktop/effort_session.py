"""Local click markers for engineer review and post-commit fix-up effort."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from cad_harness.domain.models.metrics import round_minutes
from cad_harness.metrics.collector import EngineerActivityInterval


class EngineerEffortSession:
    """Keep timestamps and a numeric fix-up duration; never keep prompt or geometry."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._active_since: datetime | None = None
        self._intervals: list[EngineerActivityInterval] = []
        self._manual_fixup_minutes = 0.0

    @property
    def active(self) -> bool:
        return self._active_since is not None

    @property
    def intervals(self) -> tuple[EngineerActivityInterval, ...]:
        return tuple(self._intervals)

    @property
    def manual_fixup_minutes(self) -> float:
        return round_minutes(self._manual_fixup_minutes)

    def start_activity(self) -> None:
        if self._active_since is not None:
            raise ValueError("Engineer activity is already being measured")
        self._active_since = self._clock()

    def stop_activity(self) -> EngineerActivityInterval:
        if self._active_since is None:
            raise ValueError("Engineer activity has not been started")
        interval = EngineerActivityInterval(self._active_since, self._clock())
        self._intervals.append(interval)
        self._active_since = None
        return interval

    def add_manual_fixup(self, minutes: float) -> None:
        if self._active_since is not None:
            raise ValueError("Stop active engineer timing before recording manual fix-up")
        if minutes < 0.0:
            raise ValueError("Manual fix-up time cannot be negative")
        self._manual_fixup_minutes += minutes


__all__ = ["EngineerEffortSession"]
