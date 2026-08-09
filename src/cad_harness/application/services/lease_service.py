"""Application lifecycle for acquiring and heartbeating writer leases."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread

from cad_harness.domain.models.lease import WriterLease
from cad_harness.domain.ports.lease_store import LeaseStore

Clock = Callable[[], datetime]


@dataclass(slots=True)
class LeaseHandle:
    """Thread-safe lease state shared by the commit and heartbeat paths."""

    lease: WriterLease
    renewal_failed: bool = False
    renewal_error: Exception | None = None
    released: bool = False
    _lock: Lock = field(default_factory=Lock, repr=False)

    def update(self, lease: WriterLease) -> None:
        with self._lock:
            self.lease = lease

    def fail(self, error: Exception) -> None:
        with self._lock:
            self.renewal_failed = True
            self.renewal_error = error

    def is_valid_at(self, now: datetime) -> bool:
        with self._lock:
            return not self.renewal_failed and self.lease.expires_at > now

    def mark_released(self) -> None:
        with self._lock:
            self.released = True


class LeaseRenewer:
    """Five-second heartbeat worker; failures are reported, never cancellation signals."""

    def __init__(
        self,
        store: LeaseStore,
        handle: LeaseHandle,
        *,
        ttl: timedelta,
        interval_seconds: float,
        clock: Clock,
    ) -> None:
        self._store = store
        self.handle = handle
        self._ttl = ttl
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._stop = Event()
        self._thread: Thread | None = None

    def renew_once(self, *, now: datetime | None = None) -> WriterLease:
        """Perform one deterministic heartbeat; useful to fault-inject without sleeping."""
        current = self.handle.lease
        try:
            renewed = self._store.renew(
                current.lease_id,
                ttl=self._ttl,
                now=now or self._clock(),
            )
        except Exception as exc:
            self.handle.fail(exc)
            raise
        self.handle.update(renewed)
        return renewed

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="writer-lease-renewer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_seconds + 1.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self.renew_once()
            except Exception as exc:  # the commit thread decides the final state
                self.handle.fail(exc)
                return


class LeaseService:
    def __init__(
        self,
        store: LeaseStore,
        *,
        ttl_seconds: int = 30,
        heartbeat_interval_seconds: int = 5,
        minimum_remaining_seconds: int = 15,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        if ttl_seconds - heartbeat_interval_seconds < minimum_remaining_seconds:
            raise ValueError("TTL minus heartbeat interval must meet the minimum remaining lease")
        self.store = store
        self.ttl = timedelta(seconds=ttl_seconds)
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.minimum_remaining = timedelta(seconds=minimum_remaining_seconds)
        self.clock = clock

    @contextmanager
    def hold(self, document_id: str, owner_id: str) -> Iterator[LeaseHandle]:
        lease = self.store.try_acquire(
            document_id=document_id,
            owner_id=owner_id,
            ttl=self.ttl,
            now=self.clock(),
        )
        handle = LeaseHandle(lease)
        renewer = LeaseRenewer(
            self.store,
            handle,
            ttl=self.ttl,
            interval_seconds=self.heartbeat_interval_seconds,
            clock=self.clock,
        )
        renewer.start()
        try:
            yield handle
        finally:
            renewer.stop()
            if not handle.released:
                self.store.release(handle.lease.lease_id, now=self.clock())
