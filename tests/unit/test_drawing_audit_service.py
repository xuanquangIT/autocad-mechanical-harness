"""DrawingAuditService persistence and redacted event tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from cad_harness.application.services.drawing_audit_service import DrawingAuditService
from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.document import LayerInfo
from cad_harness.domain.models.drawing_model import (
    CircleGeometry,
    DrawingModel,
    EntityRecord,
    LineGeometry,
    ReadScope,
)
from cad_harness.domain.models.validation import DrawingAuditEvidence, ValidationReport
from cad_harness.observability.audit import AuditEventType, InMemoryAuditSink
from cad_harness.persistence import (
    SqlDrawingAuditStore,
    build_engine,
    build_session_factory,
    create_all,
)
from cad_harness.persistence.models import DrawingAuditRow


@dataclass(slots=True)
class RecordingAuditStore:
    records: list[tuple[str, str, str, ValidationReport]] = field(default_factory=list)

    def save_drawing_audit(
        self,
        *,
        audit_id: str,
        document_id: str,
        revision: str,
        report: ValidationReport,
    ) -> None:
        self.records.append((audit_id, document_id, revision, report))

    def get_drawing_audit(self, audit_id: str) -> DrawingAuditEvidence | None:
        for stored_id, document_id, revision, report in self.records:
            if stored_id == audit_id:
                return DrawingAuditEvidence(
                    audit_id=stored_id,
                    document_id=document_id,
                    revision=revision,
                    report=report,
                )
        return None


def _model() -> DrawingModel:
    profile = load_profile("demo-profile")
    entity = EntityRecord(
        entity_ref="zero",
        entity_type="AcDbLine",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=LineGeometry(start_mm=(1.0, 1.0), end_mm=(1.0, 1.0)),
        bounding_box_mm=(1.0, 1.0, 1.0, 1.0),
    )
    return DrawingModel(
        document_id="doc-audit-service",
        revision="sha256:audit-service",
        display_name="audit-service.dxf",
        source_unit_code="mm",
        to_mm_factor=1.0,
        geometry_normalized=True,
        scope=ReadScope(),
        entities=(entity,),
        layers=tuple(
            LayerInfo(
                name=layer.name,
                color_index=layer.color_index,
                linetype=layer.linetype,
                lineweight=layer.lineweight,
            )
            for layer in profile.layers
        ),
        dimension_styles=profile.dimension_styles,
        text_styles=profile.text_styles,
        arc_chord_tolerance_mm=0.01,
    )


def test_audit_service_persists_counts_and_metadata_only_event() -> None:
    profile = load_profile("demo-profile")
    store = RecordingAuditStore()
    events = InMemoryAuditSink()
    service = DrawingAuditService(store=store, audit=events)
    model = _model()
    report = service.audit(model, profile=profile, tolerance=profile.tolerance())

    assert store.records[0][1:3] == (model.document_id, model.revision)
    assert store.records[0][3] == report
    event = events.events[-1]
    assert event.event_type == AuditEventType.DRAWING_AUDITED
    assert set(event.payload) == {
        "audit_id",
        "document_id",
        "revision",
        "blocking_count",
        "error_count",
        "warning_count",
        "info_count",
        "entities_examined",
    }

    evidence = store.get_drawing_audit(event.payload["audit_id"])
    assert evidence is not None
    assert (evidence.document_id, evidence.revision, evidence.report) == (
        model.document_id,
        model.revision,
        report,
    )


def test_sql_drawing_audit_store_uses_existing_schema(tmp_path: Path) -> None:
    engine = build_engine(tmp_path / "drawing-audit.db")
    create_all(engine)
    sessions = build_session_factory(engine)
    profile = load_profile("demo-profile")
    model = _model()
    report = DrawingAuditService(store=SqlDrawingAuditStore(sessions)).audit(
        model, profile=profile, tolerance=profile.tolerance()
    )
    with sessions() as session:
        row = session.scalar(select(DrawingAuditRow))
        assert row is not None
        assert row.document_id == model.document_id
        assert row.revision == model.revision
        assert row.report_json == report.model_dump(mode="json")
        assert row.error_count == report.error_count
        evidence = SqlDrawingAuditStore(sessions).get_drawing_audit(row.audit_id)
        assert evidence is not None
        assert evidence.report == report


def test_audit_reports_positive_collinear_overlap_but_not_an_endpoint_touch() -> None:
    profile = load_profile("demo-profile")
    first = EntityRecord(
        entity_ref="first",
        entity_type="AcDbLine",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=LineGeometry(start_mm=(0.0, 0.0), end_mm=(10.0, 0.0)),
        bounding_box_mm=(0.0, 0.0, 10.0, 0.0),
    )
    overlapping = first.model_copy(
        update={
            "entity_ref": "overlap",
            "geometry": LineGeometry(start_mm=(5.0, 0.0), end_mm=(15.0, 0.0)),
            "bounding_box_mm": (5.0, 0.0, 15.0, 0.0),
        }
    )
    touching = first.model_copy(
        update={
            "entity_ref": "touch",
            "geometry": LineGeometry(start_mm=(10.0, 0.0), end_mm=(20.0, 0.0)),
            "bounding_box_mm": (10.0, 0.0, 20.0, 0.0),
        }
    )
    service = DrawingAuditService(store=RecordingAuditStore(), audit=InMemoryAuditSink())

    overlap_report = service.audit(
        _model().model_copy(update={"entities": (first, overlapping)}),
        profile=profile,
        tolerance=profile.tolerance(),
    )
    touching_report = service.audit(
        _model().model_copy(update={"entities": (first, touching)}),
        profile=profile,
        tolerance=profile.tolerance(),
    )

    assert any(finding.rule_id == "OVERLAPPING_ENTITY" for finding in overlap_report.findings)
    assert all(finding.rule_id != "OVERLAPPING_ENTITY" for finding in touching_report.findings)


def test_audit_ignores_circle_path_endpoint_touch_but_reports_a_crossing() -> None:
    profile = load_profile("demo-profile")
    circle = EntityRecord(
        entity_ref="circle",
        entity_type="AcDbCircle",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=CircleGeometry(center_mm=(0.0, 0.0), radius_mm=5.0),
        bounding_box_mm=(-5.0, -5.0, 5.0, 5.0),
    )
    endpoint_touch = EntityRecord(
        entity_ref="endpoint-touch",
        entity_type="AcDbLine",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=LineGeometry(start_mm=(5.0, 0.0), end_mm=(10.0, 0.0)),
        bounding_box_mm=(5.0, 0.0, 10.0, 0.0),
    )
    crossing = endpoint_touch.model_copy(
        update={
            "entity_ref": "crossing",
            "geometry": LineGeometry(start_mm=(-10.0, 0.0), end_mm=(10.0, 0.0)),
            "bounding_box_mm": (-10.0, 0.0, 10.0, 0.0),
        }
    )
    fully_inside = endpoint_touch.model_copy(
        update={
            "entity_ref": "fully-inside",
            "geometry": LineGeometry(start_mm=(0.0, 0.0), end_mm=(1.0, 0.0)),
            "bounding_box_mm": (0.0, 0.0, 1.0, 0.0),
        }
    )
    inside_to_boundary = endpoint_touch.model_copy(
        update={
            "entity_ref": "inside-to-boundary",
            "geometry": LineGeometry(start_mm=(0.0, 0.0), end_mm=(5.0, 0.0)),
            "bounding_box_mm": (0.0, 0.0, 5.0, 0.0),
        }
    )
    boundary_to_inside = endpoint_touch.model_copy(
        update={
            "entity_ref": "boundary-to-inside",
            "geometry": LineGeometry(start_mm=(5.0, 0.0), end_mm=(0.0, 0.0)),
            "bounding_box_mm": (0.0, 0.0, 5.0, 0.0),
        }
    )
    service = DrawingAuditService(store=RecordingAuditStore(), audit=InMemoryAuditSink())

    touch_report = service.audit(
        _model().model_copy(update={"entities": (circle, endpoint_touch)}),
        profile=profile,
        tolerance=profile.tolerance(),
    )
    crossing_report = service.audit(
        _model().model_copy(update={"entities": (circle, crossing)}),
        profile=profile,
        tolerance=profile.tolerance(),
    )
    inside_report = service.audit(
        _model().model_copy(update={"entities": (circle, fully_inside)}),
        profile=profile,
        tolerance=profile.tolerance(),
    )
    inside_boundary_report = service.audit(
        _model().model_copy(update={"entities": (circle, inside_to_boundary)}),
        profile=profile,
        tolerance=profile.tolerance(),
    )
    boundary_inside_report = service.audit(
        _model().model_copy(update={"entities": (circle, boundary_to_inside)}),
        profile=profile,
        tolerance=profile.tolerance(),
    )

    assert all(finding.rule_id != "OVERLAPPING_ENTITY" for finding in touch_report.findings)
    assert any(finding.rule_id == "OVERLAPPING_ENTITY" for finding in crossing_report.findings)
    assert all(finding.rule_id != "OVERLAPPING_ENTITY" for finding in inside_report.findings)
    assert all(
        finding.rule_id != "OVERLAPPING_ENTITY" for finding in inside_boundary_report.findings
    )
    assert all(
        finding.rule_id != "OVERLAPPING_ENTITY" for finding in boundary_inside_report.findings
    )


def test_circular_part_outline_is_not_misclassified_as_a_hole_ligament() -> None:
    profile = load_profile("demo-profile")

    def circle(ref: str, center: tuple[float, float], radius: float) -> EntityRecord:
        x, y = center
        return EntityRecord(
            entity_ref=ref,
            entity_type="AcDbCircle",
            layer="OBJECT",
            visible=True,
            space="model",
            geometry=CircleGeometry(center_mm=center, radius_mm=radius),
            bounding_box_mm=(x - radius, y - radius, x + radius, y + radius),
        )

    model = _model().model_copy(
        update={
            "entities": (
                circle("outer", (0.0, 0.0), 100.0),
                circle("hole-a", (-30.0, 0.0), 8.0),
                circle("hole-b", (30.0, 0.0), 8.0),
            )
        }
    )
    report = DrawingAuditService(store=RecordingAuditStore(), audit=InMemoryAuditSink()).audit(
        model, profile=profile, tolerance=profile.tolerance()
    )

    assert all(finding.rule_id != "HOLE_LIGAMENT_MIN" for finding in report.findings)


def test_containing_circle_is_never_reported_as_a_negative_hole_ligament() -> None:
    profile = load_profile("demo-profile")

    def circle(ref: str, center: tuple[float, float], radius: float) -> EntityRecord:
        x, y = center
        return EntityRecord(
            entity_ref=ref,
            entity_type="AcDbCircle",
            layer="OBJECT",
            visible=True,
            space="model",
            geometry=CircleGeometry(center_mm=center, radius_mm=radius),
            bounding_box_mm=(x - radius, y - radius, x + radius, y + radius),
        )

    model = _model().model_copy(
        update={
            "entities": (
                circle("part-boundary", (0.0, 0.0), 100.0),
                circle("contained-hole", (75.0, 0.0), 8.0),
                circle("peer-hole", (-75.0, 0.0), 8.0),
            )
        }
    )

    report = DrawingAuditService(store=RecordingAuditStore(), audit=InMemoryAuditSink()).audit(
        model, profile=profile, tolerance=profile.tolerance()
    )

    assert all(
        not (
            finding.rule_id == "HOLE_LIGAMENT_MIN"
            and float(finding.actual.get("ligament_mm", 0.0)) < 0.0
        )
        for finding in report.findings
    )
