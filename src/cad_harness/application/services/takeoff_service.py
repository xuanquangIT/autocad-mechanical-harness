"""Take-off orchestration, persistence, audit and allowlisted report export."""

from __future__ import annotations

import csv
import json
import os
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import ValidationError

from cad_harness.application.process_runner import (
    JsonValue,
    ProcessWorkerCommand,
    run_process_worker,
)
from cad_harness.application.timeout import OperationDeadline, run_cancellable
from cad_harness.config import Settings
from cad_harness.domain.errors import HarnessError
from cad_harness.domain.models.drawing_model import DrawingModel
from cad_harness.domain.models.takeoff import TakeoffReport, TakeoffRequest
from cad_harness.domain.ports.material_table import MaterialTablePort
from cad_harness.domain.ports.repositories import TakeoffPersistencePort
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.metrics.recorder import OperationMeasurement, OperationMetricsRecorder
from cad_harness.security.paths import ensure_path_allowed


class TakeoffService:
    def __init__(
        self,
        settings: Settings,
        materials: MaterialTablePort,
        *,
        persistence: TakeoffPersistencePort | None = None,
        operation_metrics: OperationMetricsRecorder | None = None,
    ) -> None:
        self._settings = settings
        self._materials = materials
        self._persistence = persistence
        self._operation_metrics = operation_metrics

    def create(
        self,
        model: DrawingModel,
        request: TakeoffRequest,
        *,
        tolerance: ToleranceProfile,
        actor_id: str = "local-user",
    ) -> TakeoffReport:
        measurement = (
            self._operation_metrics.measure("takeoff")
            if self._operation_metrics is not None
            else nullcontext(OperationMeasurement())
        )
        with measurement as metric:
            deadline = OperationDeadline(self._settings.takeoff.timeout_seconds, "takeoff")
            report = run_cancellable(
                deadline,
                lambda token: self._create_terminal(
                    model,
                    request,
                    tolerance=tolerance,
                    actor_id=actor_id,
                    deadline=token,
                ),
            )
            metric.entity_count = len(model.entities)
            return report

    def _create_terminal(
        self,
        model: DrawingModel,
        request: TakeoffRequest,
        *,
        tolerance: ToleranceProfile,
        actor_id: str,
        deadline: OperationDeadline,
    ) -> TakeoffReport:
        report = self._compute(model, request, tolerance=tolerance, deadline=deadline)
        self._persist(report, actor_id=actor_id, deadline=deadline)
        return report

    def _compute(
        self,
        model: DrawingModel,
        request: TakeoffRequest,
        *,
        tolerance: ToleranceProfile,
        deadline: OperationDeadline,
    ) -> TakeoffReport:
        deadline.checkpoint()
        materials = self._materials.load_cancellable(
            request.material_profile_ref,
            deadline,
        )
        deadline.checkpoint()
        result = run_process_worker(
            deadline,
            ProcessWorkerCommand.COMPUTE_TAKEOFF,
            {
                "model": cast(JsonValue, model.model_dump(mode="json")),
                "request": cast(JsonValue, request.model_dump(mode="json")),
                "materials": cast(JsonValue, materials.model_dump(mode="json")),
                "tolerance": cast(JsonValue, asdict(tolerance)),
            },
        )
        deadline.checkpoint()
        try:
            return TakeoffReport.model_validate(result.get("report"))
        except ValidationError as exc:
            raise HarnessError(
                "Isolated take-off worker returned an invalid report",
                details={"command": ProcessWorkerCommand.COMPUTE_TAKEOFF.value},
            ) from exc

    def _persist(
        self,
        report: TakeoffReport,
        *,
        actor_id: str,
        deadline: OperationDeadline,
    ) -> None:
        total_mass = sum(part.total_mass_kg_raw for part in report.parts)
        if self._persistence is not None:
            self._persistence.persist_created(
                report_id=new_id(IdPrefix.TAKEOFF_REPORT),
                report=report,
                total_mass_kg=total_mass,
                actor_id=actor_id,
                deadline=deadline,
            )

    def export(
        self,
        report: TakeoffReport,
        target: Path,
        *,
        format: str,
        overwrite: bool = False,
    ) -> Path:
        resolved = ensure_path_allowed(
            target,
            self._settings.security.export_path_allowlist,
            allow_arbitrary=self._settings.security.allow_arbitrary_export_path,
            overwrite=overwrite,
        )
        normalized_format = format.lower()
        if normalized_format not in {"json", "csv"}:
            from cad_harness.domain.errors import UnsupportedInputFormatError

            raise UnsupportedInputFormatError(
                f"Unsupported take-off export format: {format}",
                details={"supported_formats": ["csv", "json"]},
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        temporary = resolved.with_name(f".{resolved.name}.{uuid4().hex}.tmp")
        try:
            if normalized_format == "json":
                with temporary.open("x", encoding="utf-8", newline="") as stream:
                    json.dump(report.model_dump(mode="json"), stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                self._write_csv(report, temporary, "x")
            # Re-resolve immediately before publication. If the target was replaced
            # by a symlink/reparse point during generation, this check fails closed.
            ensure_path_allowed(
                resolved,
                self._settings.security.export_path_allowlist,
                allow_arbitrary=self._settings.security.allow_arbitrary_export_path,
                overwrite=overwrite,
            )
            if overwrite:
                # Replaces the directory entry itself; it never follows a target symlink.
                os.replace(temporary, resolved)
            else:
                # Hard-link publication is atomic and fails if a racer created target.
                try:
                    os.link(temporary, resolved)
                except FileExistsError as error:
                    from cad_harness.domain.errors import ExportPathNotAllowedError

                    raise ExportPathNotAllowedError(
                        "Target file appeared while the report was being exported",
                        required_action="Choose another filename or pass overwrite=true",
                        details={"filename": resolved.name},
                    ) from error
                temporary.unlink()
        finally:
            temporary.unlink(missing_ok=True)
        return resolved

    @staticmethod
    def _write_csv(report: TakeoffReport, target: Path, mode: str) -> None:
        fields = [
            "part_code",
            "material_code",
            "density_kg_per_m3",
            "thickness_mm",
            "quantity",
            "net_area_mm2",
            "gross_area_mm2",
            "unit_mass_kg",
            "unit_mass_kg_raw",
            "unit_mass_kg_raw_text",
            "total_mass_kg",
            "total_mass_kg_raw",
            "total_mass_kg_raw_text",
            "cut_length_mm",
            "outer_cut_length_mm",
            "inner_cut_length_mm",
            "pierce_count",
            "hole_groups",
            "weld_length_mm",
            "evidence",
        ]
        with target.open(mode, encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for part in report.parts:
                row = part.model_dump(mode="json")
                row["hole_groups"] = json.dumps(row["hole_groups"], sort_keys=True)
                row["evidence"] = json.dumps(row["evidence"], sort_keys=True)
                writer.writerow(row)
            stream.flush()
            os.fsync(stream.fileno())


__all__ = ["TakeoffService"]
