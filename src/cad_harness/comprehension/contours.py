"""Normalize drawing edges, assemble endpoint chains, and build a contour forest."""

from __future__ import annotations

from dataclasses import dataclass

from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    CircleGeometry,
    DrawingModel,
    LineGeometry,
    PolylineGeometry,
)
from cad_harness.domain.models.recognition import OpenContourFinding
from cad_harness.geometry.areas import (
    Contour,
    ContourEdge,
    ContourForest,
    CurveContour,
    LineEdge,
)
from cad_harness.geometry.curves import (
    CurveParams,
    normalize_arc,
    normalize_bulge,
    normalize_circle,
)
from cad_harness.geometry.primitives import Point2D
from cad_harness.geometry.tolerance import ToleranceProfile


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    edge: ContourEdge
    entity_ref: str

    @property
    def start(self) -> Point2D:
        return self.edge.start if isinstance(self.edge, LineEdge) else self.edge.start_point

    @property
    def end(self) -> Point2D:
        return self.edge.end if isinstance(self.edge, LineEdge) else self.edge.end_point


@dataclass(frozen=True, slots=True)
class AssembledContour:
    contour: Contour
    entity_refs: tuple[str, ...]
    edges: tuple[EdgeRecord, ...]
    is_circle: bool = False


@dataclass(frozen=True, slots=True)
class ContourAnalysis:
    contours: tuple[AssembledContour, ...]
    forest: ContourForest
    open_contours: tuple[OpenContourFinding, ...]


def _point(value: tuple[float, float]) -> Point2D:
    return Point2D(float(value[0]), float(value[1]))


def _reverse_curve(curve: CurveParams) -> CurveParams:
    return CurveParams(
        kind=curve.kind,
        center=curve.center,
        start_angle_deg=curve.end_angle_deg,
        sweep_deg=-curve.sweep_deg,
        radius_mm=curve.radius_mm,
        semi_major_mm=curve.semi_major_mm,
        semi_minor_mm=curve.semi_minor_mm,
        rotation_deg=curve.rotation_deg,
    )


def _reverse(record: EdgeRecord) -> EdgeRecord:
    edge = record.edge
    reversed_edge: ContourEdge
    if isinstance(edge, LineEdge):
        reversed_edge = LineEdge(edge.end, edge.start)
    else:
        reversed_edge = _reverse_curve(edge)
    return EdgeRecord(reversed_edge, record.entity_ref)


def _entity_edges(
    model: DrawingModel,
) -> tuple[tuple[EdgeRecord, ...], tuple[AssembledContour, ...]]:
    edges: list[EdgeRecord] = []
    circles: list[AssembledContour] = []
    for entity in model.entities:
        geometry = entity.geometry
        if isinstance(geometry, LineGeometry):
            edges.append(
                EdgeRecord(
                    LineEdge(_point(geometry.start_mm), _point(geometry.end_mm)),
                    entity.entity_ref,
                )
            )
        elif isinstance(geometry, ArcGeometry):
            edges.append(
                EdgeRecord(
                    normalize_arc(
                        _point(geometry.center_mm),
                        geometry.radius_mm,
                        geometry.start_angle_deg,
                        geometry.end_angle_deg,
                    ),
                    entity.entity_ref,
                )
            )
        elif isinstance(geometry, CircleGeometry):
            curve = normalize_circle(_point(geometry.center_mm), geometry.radius_mm)
            record = EdgeRecord(curve, entity.entity_ref)
            circles.append(
                AssembledContour(
                    CurveContour((curve,)),
                    (entity.entity_ref,),
                    (record,),
                    is_circle=True,
                )
            )
        elif isinstance(geometry, PolylineGeometry):
            vertices = geometry.vertices
            segment_count = len(vertices) if geometry.closed else max(0, len(vertices) - 1)
            for index in range(segment_count):
                start = _point(vertices[index].point_mm)
                end = _point(vertices[(index + 1) % len(vertices)].point_mm)
                bulge = vertices[index].bulge
                edge: ContourEdge = (
                    LineEdge(start, end)
                    if abs(bulge) <= 1.0e-15
                    else normalize_bulge(start, end, bulge)
                )
                edges.append(EdgeRecord(edge, entity.entity_ref))
    return tuple(edges), tuple(circles)


def _find_match(
    unused: list[EdgeRecord], endpoint: Point2D, tolerance: ToleranceProfile
) -> tuple[int, EdgeRecord] | None:
    for index, candidate in enumerate(unused):
        if tolerance.is_coincident(endpoint.distance_to(candidate.start)):
            return index, candidate
        if tolerance.is_coincident(endpoint.distance_to(candidate.end)):
            return index, _reverse(candidate)
    return None


def _unique_refs(records: list[EdgeRecord]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(record.entity_ref for record in records))


def analyze_contours(model: DrawingModel, tolerance: ToleranceProfile) -> ContourAnalysis:
    """Assemble every supported boundary in stable source order."""

    normalized, circle_contours = _entity_edges(model)
    unused = list(normalized)
    closed: list[AssembledContour] = list(circle_contours)
    open_findings: list[OpenContourFinding] = []
    while unused:
        chain = [unused.pop(0)]
        while unused:
            match = _find_match(unused, chain[-1].end, tolerance)
            if match is None:
                break
            index, record = match
            unused.pop(index)
            chain.append(record)
            if tolerance.is_coincident(chain[-1].end.distance_to(chain[0].start)):
                break

        while unused and not tolerance.is_coincident(chain[-1].end.distance_to(chain[0].start)):
            match = _find_match(unused, chain[0].start, tolerance)
            if match is None:
                break
            index, record = match
            unused.pop(index)
            chain.insert(0, _reverse(record))

        gap = chain[-1].end.distance_to(chain[0].start)
        if tolerance.is_coincident(gap):
            closed.append(
                AssembledContour(
                    CurveContour(tuple(record.edge for record in chain)),
                    _unique_refs(chain),
                    tuple(chain),
                )
            )
        else:
            open_findings.append(
                OpenContourFinding(
                    gap_mm=gap,
                    endpoint_entity_refs=(chain[0].entity_ref, chain[-1].entity_ref),
                )
            )

    forest = ContourForest.build(tuple(item.contour for item in closed), tolerance)
    return ContourAnalysis(tuple(closed), forest, tuple(open_findings))
