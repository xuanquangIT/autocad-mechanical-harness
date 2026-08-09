"""Read-only material-table lookup port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cad_harness.domain.models.takeoff import MaterialTable
from cad_harness.domain.ports.repositories import CancellationTokenPort


@runtime_checkable
class MaterialTablePort(Protocol):
    """Resolve a versioned ``<profile_id>@<version>`` material table."""

    def load(self, profile_ref: str) -> MaterialTable: ...

    def load_cancellable(
        self, profile_ref: str, deadline: CancellationTokenPort
    ) -> MaterialTable: ...


__all__ = ["MaterialTablePort"]
