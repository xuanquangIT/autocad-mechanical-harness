"""TakeoffService export, persistence and audit boundary tests."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from cad_harness.application.services.takeoff_service import TakeoffService
from cad_harness.company_rules.material_loader import YamlMaterialTableLoader
from cad_harness.config import Settings
from cad_harness.domain.errors import ExportPathNotAllowedError, IpcTimeoutError
from cad_harness.domain.models.drawing_model import (
    DrawingModel,
    EntityRecord,
    LineGeometry,
    PolylineGeometry,
    PolylineVertex,
    ReadScope,
)
from cad_harness.domain.models.takeoff import PartInput, TakeoffReport, TakeoffRequest
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.persistence import (
    SqlTakeoffReportStore,
    build_engine,
    build_session_factory,
    create_all,
)
from cad_harness.persistence.models import AuditEventRow, TakeoffReportRow


@dataclass(slots=True)
class RecordingStore:
    records: list[tuple[str, TakeoffReport, float]] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)

    def save_takeoff_report(
        self, *, report_id: str, report: TakeoffReport, total_mass_kg: float
    ) -> None:
        self.records.append((report_id, report, total_mass_kg))

    def persist_created(
        self,
        *,
        report_id: str,
        report: TakeoffReport,
        total_mass_kg: float,
        actor_id: str,
        deadline: Any,
    ) -> str:
        deadline.checkpoint()
        self.records.append((report_id, report, total_mass_kg))
        self.events.append(
            {
                "document_id": report.document_id,
                "revision": report.revision,
                "total_mass_kg": total_mass_kg,
                "actor_id": actor_id,
            }
        )
        return "audit-test"


def _model() -> DrawingModel:
    outline = EntityRecord(
        entity_ref="outline",
        entity_type="AcDbPolyline",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=PolylineGeometry(
            vertices=tuple(
                PolylineVertex(point_mm=point)
                for point in ((0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0))
            ),
            closed=True,
        ),
        bounding_box_mm=(0.0, 0.0, 100.0, 50.0),
    )
    return DrawingModel(
        document_id="doc-service",
        revision="sha256:service",
        display_name="service.dxf",
        source_unit_code="mm",
        to_mm_factor=1.0,
        geometry_normalized=True,
        scope=ReadScope(),
        entities=(outline,),
        arc_chord_tolerance_mm=0.01,
    )


def _request() -> TakeoffRequest:
    return TakeoffRequest(
        document_id="doc-service",
        parts=(
            PartInput(
                part_code="P-001",
                outline_entity_ref="outline",
                thickness_mm=10.0,
                material_code="SS400",
                quantity=2,
            ),
        ),
        material_profile_ref="demo-materials@1.0",
    )


def test_service_persists_and_audits_only_report_metadata(settings: Settings) -> None:
    store = RecordingStore()
    service = TakeoffService(settings, YamlMaterialTableLoader(), persistence=store)
    report = service.create(
        _model(), _request(), tolerance=ToleranceProfile(id="takeoff", version="1.0")
    )

    assert store.records[0][1] == report
    assert store.records[0][2] == sum(part.total_mass_kg_raw for part in report.parts)
    event = store.events[-1]
    assert set(event) == {"document_id", "revision", "total_mass_kg", "actor_id"}


def test_closed_centerline_loop_is_not_subtracted_as_a_cutting_contour(
    settings: Settings,
) -> None:
    model = _model()
    points = ((20.0, 10.0), (80.0, 10.0), (80.0, 40.0), (20.0, 40.0))
    centerlines = tuple(
        EntityRecord(
            entity_ref=f"center-{index}",
            entity_type="AcDbLine",
            layer="CENTER",
            visible=True,
            space="model",
            geometry=LineGeometry(start_mm=start, end_mm=end),
            bounding_box_mm=(
                min(start[0], end[0]),
                min(start[1], end[1]),
                max(start[0], end[0]),
                max(start[1], end[1]),
            ),
        )
        for index, (start, end) in enumerate(zip(points, points[1:] + points[:1], strict=True))
    )
    service = TakeoffService(settings, YamlMaterialTableLoader())

    report = service.create(
        model.model_copy(update={"entities": model.entities + centerlines}),
        _request(),
        tolerance=ToleranceProfile(id="takeoff", version="1.0"),
    )

    line = report.parts[0]
    assert line.net_area_mm2 == 5_000.0
    assert line.cut_length_mm == 300.0
    assert line.pierce_count == 1
    assert not any("center-" in ref for refs in line.evidence.values() for ref in refs)


def test_property_55_export_allowlist_overwrite_and_formats(
    settings: Settings, tmp_path: Path
) -> None:
    service = TakeoffService(settings, YamlMaterialTableLoader())
    report = service.create(
        _model(), _request(), tolerance=ToleranceProfile(id="takeoff", version="1.0")
    )
    export_root = settings.security.export_path_allowlist[0]
    json_path = service.export(report, export_root / "takeoff.json", format="json")
    csv_path = service.export(report, export_root / "takeoff.csv", format="csv")
    assert TakeoffReport.model_validate(json.loads(json_path.read_text(encoding="utf-8"))) == report
    with csv_path.open(encoding="utf-8", newline="") as stream:
        assert next(iter(csv.DictReader(stream)))["part_code"] == "P-001"

    with pytest.raises(ExportPathNotAllowedError):
        service.export(report, tmp_path / "outside.json", format="json")
    original = json_path.read_bytes()
    with pytest.raises(ExportPathNotAllowedError):
        service.export(report, json_path, format="json")
    assert json_path.read_bytes() == original
    service.export(report, json_path, format="json", overwrite=True)


def test_sql_takeoff_store_writes_existing_schema(tmp_path: Path) -> None:
    engine = build_engine(tmp_path / "takeoff.db")
    create_all(engine)
    sessions = build_session_factory(engine)
    service = TakeoffService(
        Settings(),
        YamlMaterialTableLoader(),
        persistence=SqlTakeoffReportStore(sessions),
    )
    report = service.create(
        _model(), _request(), tolerance=ToleranceProfile(id="takeoff", version="1.0")
    )
    with sessions() as session:
        row = session.scalar(select(TakeoffReportRow))
        assert row is not None
        assert row.report_json == report.model_dump(mode="json")
        assert row.document_id == report.document_id
        assert session.scalar(select(AuditEventRow)) is not None


def test_sql_takeoff_timeout_rolls_back_report_and_audit(tmp_path: Path) -> None:
    engine = build_engine(tmp_path / "takeoff-timeout.db")
    create_all(engine)
    sessions = build_session_factory(engine)
    service = TakeoffService(
        Settings.model_validate({"takeoff": {"timeout_seconds": 0.05}}),
        YamlMaterialTableLoader(),
        persistence=SqlTakeoffReportStore(sessions),
    )

    with engine.connect() as blocker:
        blocker.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            with pytest.raises(IpcTimeoutError):
                service.create(
                    _model(),
                    _request(),
                    tolerance=ToleranceProfile(id="takeoff", version="1.0"),
                )
        finally:
            blocker.rollback()

    with sessions() as session:
        assert session.scalar(select(TakeoffReportRow)) is None
        assert session.scalar(select(AuditEventRow)) is None
