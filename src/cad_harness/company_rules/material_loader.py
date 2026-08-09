"""Load versioned, read-only material density tables."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import ValidationError

from cad_harness.application.process_runner import ProcessWorkerCommand, run_process_worker
from cad_harness.domain.errors import HarnessError, StandardProfileNotFoundError
from cad_harness.domain.models.takeoff import MaterialTable
from cad_harness.domain.ports.repositories import CancellationTokenPort

MATERIALS_DIR = Path(__file__).parent / "materials"


class _Deadline(Protocol):
    def checkpoint(self) -> None: ...


class YamlMaterialTableLoader:
    """Resolve an exact ``profile_id@version`` from controlled YAML files."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory or MATERIALS_DIR

    def load(self, profile_ref: str) -> MaterialTable:
        return self._load(profile_ref, None)

    def load_cancellable(self, profile_ref: str, deadline: CancellationTokenPort) -> MaterialTable:
        result = run_process_worker(
            deadline,
            ProcessWorkerCommand.LOAD_MATERIAL_TABLE,
            {"profile_ref": profile_ref},
        )
        try:
            return MaterialTable.model_validate(result.get("materials"))
        except ValidationError as exc:
            raise HarnessError(
                "Isolated material worker returned an invalid table",
                details={"command": ProcessWorkerCommand.LOAD_MATERIAL_TABLE.value},
            ) from exc

    def _load(self, profile_ref: str, deadline: _Deadline | None) -> MaterialTable:
        profile_id, separator, version = profile_ref.partition("@")
        if not separator or not profile_id or not version:
            raise StandardProfileNotFoundError(
                "Material profile reference must include an exact version",
                required_action="Use '<profile_id>@<version>' from the controlled material tables",
                details={"profile_ref": profile_ref},
            )
        candidates = sorted(self._directory.glob("*.yaml"))
        for path in candidates:
            if deadline is not None:
                deadline.checkpoint()
            raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
            if deadline is not None:
                deadline.checkpoint()
            table = MaterialTable.model_validate(raw)
            if table.profile_id == profile_id and table.version == version:
                return table
        raise StandardProfileNotFoundError(
            f"Material profile not found: {profile_ref}",
            required_action="Install the requested versioned material table",
            details={"available_profiles": self.available_refs()},
        )

    def available_refs(self) -> list[str]:
        refs: list[str] = []
        for path in sorted(self._directory.glob("*.yaml")):
            raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
            table = MaterialTable.model_validate(raw)
            refs.append(f"{table.profile_id}@{table.version}")
        return refs


def load_material_table(profile_ref: str, directory: Path | None = None) -> MaterialTable:
    return YamlMaterialTableLoader(directory).load(profile_ref)


__all__ = ["MATERIALS_DIR", "YamlMaterialTableLoader", "load_material_table"]
