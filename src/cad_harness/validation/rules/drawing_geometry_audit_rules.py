"""Deterministic geometric integrity rules for extracted drawings."""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from cad_harness.comprehension.contours import AssembledContour, ContourAnalysis, analyze_contours
from cad_harness.domain.errors import HarnessError, InvalidGeometryError
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    CircleGeometry,
    DrawingModel,
    EllipseGeometry,
    EntityRecord,
    LineGeometry,
    PolylineGeometry,
)
from cad_harness.domain.models.validation import Finding, Severity, ValidationStage
from cad_harness.geometry.curves import (
    linearize_curve,
    normalize_arc,
    normalize_bulge,
    normalize_ellipse,
)
from cad_harness.geometry.intersections import (
    circles_overlap,
    contour_intersections,
    point_to_segment_distance,
)
from cad_harness.geometry.predicates import (
    circle_overflow_mm,
    minimum_edge_distance,
    polyline_self_intersects,
)
from cad_harness.geometry.primitives import Circle2D, Point2D, Polyline2D
from cad_harness.validation.engine import RuleContext
from cad_harness.validation.findings import finding

DRAWING_AUDIT_STAGES = (ValidationStage.DRAWING_AUDIT,)


def _model(context: RuleContext) -> DrawingModel:
    model = context.require_drawing_model()
    if not isinstance(model, DrawingModel):
        raise HarnessError(
            "Drawing geometry rules require the complete DrawingModel contract",
            required_action="Read the drawing through DrawingReadService before validation",
        )
    return model


def _analysis(model: DrawingModel, context: RuleContext) -> ContourAnalysis | None:
    """Return no analysis for malformed extracted data; a specific rule reports it."""
    try:
        return analyze_contours(model, context.tolerance)
    except (ArithmeticError, InvalidGeometryError, ValueError):
        return None


def _polyline(contour: AssembledContour, context: RuleContext) -> Polyline2D | None:
    try:
        if isinstance(contour.contour, Polyline2D):
            return contour.contour
        return Polyline2D(
            contour.contour.vertices(context.tolerance.arc_chord_tolerance_mm), closed=True
        )
    except (ArithmeticError, InvalidGeometryError, ValueError):
        return None


def _circles(analysis: ContourAnalysis, model: DrawingModel) -> list[tuple[int, CircleGeometry]]:
    entities = {entity.entity_ref: entity for entity in model.entities}
    return [
        (index, geometry)
        for index, contour in enumerate(analysis.contours)
        if contour.is_circle
        and len(contour.entity_refs) == 1
        and isinstance((geometry := entities[contour.entity_refs[0]].geometry), CircleGeometry)
    ]


def _parent_outline(
    analysis: ContourAnalysis,
    index: int,
    geometry: CircleGeometry,
    context: RuleContext,
    model: DrawingModel,
) -> Polyline2D | None:
    parent = analysis.forest.nodes[index].parent_index
    if parent is not None:
        return _polyline(analysis.contours[parent], context)
    entity_ref = analysis.contours[index].entity_refs[0]
    entity = next(item for item in model.entities if item.entity_ref == entity_ref)
    if entity.feature_id is None or "hole" not in entity.feature_id.casefold():
        return None
    # A root circle among non-circle roots is an outside hole. Pick the smallest
    # candidate overflow deterministically so nested drawings have a stable result.
    candidates: list[Polyline2D] = []
    for contour in analysis.contours:
        if contour.is_circle:
            continue
        candidate = _polyline(contour, context)
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None
    circle = _circle(geometry)
    if circle is None:
        return None
    return min(
        candidates,
        key=lambda candidate: circle_overflow_mm(candidate, circle, context.tolerance),
    )


def _circle(geometry: CircleGeometry) -> Circle2D | None:
    try:
        return Circle2D(Point2D(*geometry.center_mm), 2.0 * geometry.radius_mm)
    except (ArithmeticError, InvalidGeometryError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class ZeroLengthEntityRule:
    rule_id: str = "ZERO_LENGTH_ENTITY"
    stages: tuple[ValidationStage, ...] = DRAWING_AUDIT_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        for entity in _model(context).entities:
            geometry = entity.geometry
            zero = False
            if isinstance(geometry, LineGeometry):
                values = (*geometry.start_mm, *geometry.end_mm)
                zero = all(math.isfinite(value) for value in values) and (
                    context.tolerance.is_zero_length(
                        math.hypot(
                            geometry.end_mm[0] - geometry.start_mm[0],
                            geometry.end_mm[1] - geometry.start_mm[1],
                        )
                    )
                )
            if isinstance(geometry, PolylineGeometry):
                points = tuple(vertex.point_mm for vertex in geometry.vertices)
                pairs = list(pairwise(points))
                if geometry.closed and len(points) > 1:
                    pairs.append((points[-1], points[0]))
                zero = any(
                    all(math.isfinite(value) for value in (*first, *second))
                    and context.tolerance.is_zero_length(
                        math.hypot(second[0] - first[0], second[1] - first[1])
                    )
                    for first, second in pairs
                )
            if zero:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Entity contains a zero-length segment",
                        entity_ref=entity.entity_ref,
                        expected="all segments longer than tolerance",
                        actual="zero-length segment",
                        tolerance=context.tolerance.absolute_length_mm,
                        suggested_fix="Delete the degenerate entity or repair its endpoints",
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class OpenContourRule:
    rule_id: str = "OPEN_CONTOUR"
    stages: tuple[ValidationStage, ...] = DRAWING_AUDIT_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        model = _model(context)
        analysis = _analysis(model, context)
        if analysis is None:
            return []
        entities = {entity.entity_ref: entity for entity in model.entities}
        return [
            finding(
                self.rule_id,
                Severity.ERROR,
                "Contour endpoints do not close within tolerance",
                entity_ref=item.endpoint_entity_refs[0],
                expected={"gap_mm": 0.0},
                actual={"gap_mm": item.gap_mm, "other_entity_ref": item.endpoint_entity_refs[1]},
                tolerance=context.tolerance.absolute_length_mm,
                suggested_fix="Join the contour endpoints or remove the incomplete boundary",
            )
            for item in analysis.open_contours
            if item.endpoint_entity_refs[0] != item.endpoint_entity_refs[1]
            or isinstance(
                entities[item.endpoint_entity_refs[0]].geometry,
                PolylineGeometry,
            )
        ]


@dataclass(frozen=True, slots=True)
class SelfIntersectingContourRule:
    rule_id: str = "SELF_INTERSECTING_CONTOUR"
    stages: tuple[ValidationStage, ...] = DRAWING_AUDIT_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        for entity in _model(context).entities:
            geometry = entity.geometry
            if not isinstance(geometry, PolylineGeometry) or not geometry.closed:
                continue
            try:
                polyline = Polyline2D(
                    tuple(Point2D(*vertex.point_mm) for vertex in geometry.vertices), closed=True
                )
            except (InvalidGeometryError, ValueError):
                continue
            if polyline_self_intersects(polyline, context.tolerance):
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Closed contour self-intersects",
                        entity_ref=entity.entity_ref,
                        expected="simple closed contour",
                        actual="self-intersection present",
                        tolerance=context.tolerance.absolute_length_mm,
                        suggested_fix="Repair the vertex order or split crossing geometry",
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class DuplicateEntityRule:
    rule_id: str = "DUPLICATE_ENTITY"
    stages: tuple[ValidationStage, ...] = DRAWING_AUDIT_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        seen: dict[str, EntityRecord] = {}
        findings: list[Finding] = []
        for entity in _model(context).entities:
            payload = entity.model_dump(mode="json", exclude={"entity_ref", "feature_id"})
            key = repr(payload)
            original = seen.get(key)
            if original is None:
                seen[key] = entity
                continue
            findings.append(
                finding(
                    self.rule_id,
                    Severity.WARNING,
                    "Entity duplicates an earlier entity",
                    entity_ref=entity.entity_ref,
                    expected={"unique_geometry": True},
                    actual={"duplicate_of": original.entity_ref},
                    tolerance=context.tolerance.absolute_length_mm,
                    suggested_fix="Remove the duplicate after confirming the intended geometry",
                )
            )
        return findings


@dataclass(frozen=True, slots=True)
class OverlappingEntityRule:
    rule_id: str = "OVERLAPPING_ENTITY"
    stages: tuple[ValidationStage, ...] = DRAWING_AUDIT_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        entities = _model(context).entities
        findings: list[Finding] = []
        for index, first in enumerate(entities):
            for second in entities[index + 1 :]:
                if first.geometry == second.geometry:
                    continue
                if not _entities_overlap(first, second, context):
                    continue
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.WARNING,
                        "Entities overlap geometrically",
                        entity_ref=first.entity_ref,
                        expected="non-overlapping geometry",
                        actual={"other_entity_ref": second.entity_ref},
                        tolerance=context.tolerance.absolute_length_mm,
                        suggested_fix=(
                            "Review the overlapping entities and keep the intended geometry"
                        ),
                    )
                )
        return findings


def _entities_overlap(first: EntityRecord, second: EntityRecord, context: RuleContext) -> bool:
    """Report actual overlap, never merely touching/containing bounding boxes."""
    first_geometry, second_geometry = first.geometry, second.geometry
    if isinstance(first_geometry, CircleGeometry) and isinstance(second_geometry, CircleGeometry):
        try:
            distance = Point2D(*first_geometry.center_mm).distance_to(
                Point2D(*second_geometry.center_mm)
            )
        except (InvalidGeometryError, ValueError):
            return False
        tolerance = context.tolerance.absolute_length_mm
        return (
            abs(first_geometry.radius_mm - second_geometry.radius_mm) + tolerance
            < distance
            < first_geometry.radius_mm + second_geometry.radius_mm - tolerance
        )
    first_path = _entity_path(first, context)
    second_path = _entity_path(second, context)
    if isinstance(first_geometry, CircleGeometry) and second_path is not None:
        return _circle_crosses_path(first_geometry, second_path, context)
    if isinstance(second_geometry, CircleGeometry) and first_path is not None:
        return _circle_crosses_path(second_geometry, first_path, context)
    if first_path is not None and second_path is not None:
        intersections = contour_intersections(first_path, second_path, context.tolerance)
        shared_endpoint_only = bool(intersections) and all(
            any(
                context.tolerance.is_coincident(intersection.distance_to(endpoint))
                for endpoint in (first_path.vertices[0], first_path.vertices[-1])
            )
            and any(
                context.tolerance.is_coincident(intersection.distance_to(endpoint))
                for endpoint in (second_path.vertices[0], second_path.vertices[-1])
            )
            for intersection in intersections
        )
        if intersections and not shared_endpoint_only:
            return True
        return _paths_have_positive_collinear_overlap(first_path, second_path, context)
    else:
        return False


def _paths_have_positive_collinear_overlap(
    first: Polyline2D, second: Polyline2D, context: RuleContext
) -> bool:
    """Detect material shared by any linearized path segments, not endpoint touches."""
    tolerance = context.tolerance.absolute_length_mm
    for first_start, first_end in first.segments:
        vector = first_start.vector_to(first_end)
        length = vector.length
        if length <= tolerance:
            continue
        for second_start, second_end in second.segments:
            if (
                max(
                    abs(vector.cross(first_start.vector_to(second_start))),
                    abs(vector.cross(first_start.vector_to(second_end))),
                )
                > tolerance * length
            ):
                continue

            start_projection = first_start.vector_to(second_start).dot(vector) / length
            end_projection = first_start.vector_to(second_end).dot(vector) / length
            overlap_start = max(0.0, min(start_projection, end_projection))
            overlap_end = min(length, max(start_projection, end_projection))
            if overlap_end - overlap_start > tolerance:
                return True
    return False


def _entity_path(entity: EntityRecord, context: RuleContext) -> Polyline2D | None:
    geometry = entity.geometry
    try:
        if isinstance(geometry, LineGeometry):
            return Polyline2D(
                (Point2D(*geometry.start_mm), Point2D(*geometry.end_mm)), closed=False
            )
        if isinstance(geometry, ArcGeometry):
            curve = normalize_arc(
                Point2D(*geometry.center_mm),
                geometry.radius_mm,
                geometry.start_angle_deg,
                geometry.end_angle_deg,
            )
            return Polyline2D(
                linearize_curve(curve, context.tolerance.arc_chord_tolerance_mm),
                closed=False,
            )
        if isinstance(geometry, EllipseGeometry):
            curve = normalize_ellipse(
                Point2D(*geometry.center_mm),
                geometry.major_axis_mm,
                geometry.minor_axis_mm,
                geometry.rotation_deg,
            )
            return Polyline2D(
                linearize_curve(curve, context.tolerance.arc_chord_tolerance_mm)[:-1],
                closed=True,
            )
        if isinstance(geometry, PolylineGeometry):
            points: list[Point2D] = []
            count = len(geometry.vertices) if geometry.closed else len(geometry.vertices) - 1
            for index in range(count):
                start = Point2D(*geometry.vertices[index].point_mm)
                end = Point2D(*geometry.vertices[(index + 1) % len(geometry.vertices)].point_mm)
                bulge = geometry.vertices[index].bulge
                segment = (
                    (start, end)
                    if abs(bulge) <= 1.0e-15
                    else linearize_curve(
                        normalize_bulge(start, end, bulge),
                        context.tolerance.arc_chord_tolerance_mm,
                    )
                )
                points.extend(segment if not points else segment[1:])
            if geometry.closed and points and points[-1] == points[0]:
                points.pop()
            return Polyline2D(tuple(points), closed=geometry.closed)
    except (ArithmeticError, InvalidGeometryError, ValueError):
        return None
    return None


def _circle_crosses_path(circle: CircleGeometry, path: Polyline2D, context: RuleContext) -> bool:
    try:
        center = Point2D(*circle.center_mm)
    except InvalidGeometryError:
        return False
    radius = circle.radius_mm
    tolerance = context.tolerance.absolute_length_mm
    for start, end in path.segments:
        start_distance = center.distance_to(start)
        end_distance = center.distance_to(end)
        start_inside = start_distance < radius - tolerance
        end_inside = end_distance < radius - tolerance
        start_boundary = abs(start_distance - radius) <= tolerance
        end_boundary = abs(end_distance - radius) <= tolerance
        if start_boundary or end_boundary:
            # A segment merely entering or leaving at the boundary is a touch,
            # not an overlap.  Only a boundary-to-boundary chord traverses the
            # circle and exits it again.
            if (
                start_boundary
                and end_boundary
                and point_to_segment_distance(center, start, end) < radius - tolerance
            ):
                return True
            continue
        if start_inside != end_inside:
            return True
        if (
            not start_inside
            and not end_inside
            and point_to_segment_distance(center, start, end) < radius - tolerance
        ):
            return True
    return False


@dataclass(frozen=True, slots=True)
class HoleOutsidePartRule:
    rule_id: str = "HOLE_OUTSIDE_PART"
    stages: tuple[ValidationStage, ...] = DRAWING_AUDIT_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        model = _model(context)
        analysis = _analysis(model, context)
        if analysis is None:
            return []
        findings: list[Finding] = []
        for index, geometry in _circles(analysis, model):
            hole = _circle(geometry)
            outline = _parent_outline(analysis, index, geometry, context, model)
            if hole is None or outline is None:
                continue
            overflow = circle_overflow_mm(outline, hole, context.tolerance)
            if overflow > context.tolerance.absolute_length_mm:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Circular hole extends outside its containing part",
                        entity_ref=analysis.contours[index].entity_refs[0],
                        expected={"overflow_mm": 0.0},
                        actual={"overflow_mm": overflow},
                        tolerance=context.tolerance.absolute_length_mm,
                        suggested_fix=(
                            "Move or resize the hole so it remains inside the part boundary"
                        ),
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class HoleEdgeDistanceRule:
    rule_id: str = "HOLE_EDGE_DISTANCE_MIN"
    stages: tuple[ValidationStage, ...] = DRAWING_AUDIT_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        minimum = context.profile.minimum_hole_edge_distance_mm
        if minimum is None:
            return []
        model = _model(context)
        analysis = _analysis(model, context)
        if analysis is None:
            return []
        findings: list[Finding] = []
        for index, geometry in _circles(analysis, model):
            hole = _circle(geometry)
            outline = _parent_outline(analysis, index, geometry, context, model)
            if hole is None or outline is None:
                continue
            clearance = minimum_edge_distance(hole, outline)
            if clearance < minimum:
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Hole is closer to the edge than the profile minimum",
                        entity_ref=analysis.contours[index].entity_refs[0],
                        expected={"minimum_edge_distance_mm": minimum},
                        actual={"edge_distance_mm": clearance},
                        tolerance=context.tolerance.absolute_length_mm,
                        suggested_fix="Move the hole inward or obtain an engineering exception",
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class HoleLigamentRule:
    rule_id: str = "HOLE_LIGAMENT_MIN"
    stages: tuple[ValidationStage, ...] = DRAWING_AUDIT_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        minimum = context.profile.minimum_hole_ligament_mm
        if minimum is None:
            return []
        model = _model(context)
        analysis = _analysis(model, context)
        if analysis is None:
            return []
        circles = _circles(analysis, model)
        findings: list[Finding] = []
        for position, (first_index, first) in enumerate(circles):
            for second_index, second in circles[position + 1 :]:
                if _root_index(analysis, first_index) != _root_index(analysis, second_index):
                    continue
                first_circle, second_circle = _circle(first), _circle(second)
                if first_circle is None or second_circle is None:
                    continue
                if not circles_overlap(
                    first_circle.center,
                    first_circle.diameter_mm,
                    second_circle.center,
                    second_circle.diameter_mm,
                    minimum_ligament_mm=minimum,
                ):
                    continue
                ligament = first_circle.center.distance_to(second_circle.center) - (
                    first_circle.radius_mm + second_circle.radius_mm
                )
                findings.append(
                    finding(
                        self.rule_id,
                        Severity.ERROR,
                        "Hole-to-hole ligament is below the profile minimum",
                        entity_ref=analysis.contours[first_index].entity_refs[0],
                        expected={"minimum_ligament_mm": minimum},
                        actual={
                            "ligament_mm": ligament,
                            "other_entity_ref": analysis.contours[second_index].entity_refs[0],
                        },
                        tolerance=context.tolerance.absolute_length_mm,
                        suggested_fix="Increase the centre distance or reduce a hole diameter",
                    )
                )
        return findings


def _root_index(analysis: ContourAnalysis, index: int) -> int:
    parent = analysis.forest.nodes[index].parent_index
    while parent is not None:
        index = parent
        parent = analysis.forest.nodes[index].parent_index
    return index


@dataclass(frozen=True, slots=True)
class InvalidArcRadiusRule:
    rule_id: str = "INVALID_ARC_RADIUS"
    stages: tuple[ValidationStage, ...] = DRAWING_AUDIT_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        return [
            finding(
                self.rule_id,
                Severity.ERROR,
                "Arc or circle radius must be positive and finite",
                entity_ref=entity.entity_ref,
                expected="finite radius > 0",
                actual=geometry.radius_mm,
                tolerance=context.tolerance.absolute_length_mm,
                suggested_fix="Repair the arc radius and re-read the drawing",
            )
            for entity in _model(context).entities
            if isinstance((geometry := entity.geometry), ArcGeometry | CircleGeometry)
            and (not math.isfinite(geometry.radius_mm) or geometry.radius_mm <= 0.0)
        ]


@dataclass(frozen=True, slots=True)
class FilletNotTangentRule:
    rule_id: str = "FILLET_NOT_TANGENT"
    stages: tuple[ValidationStage, ...] = DRAWING_AUDIT_STAGES

    def evaluate(self, context: RuleContext) -> list[Finding]:
        findings: list[Finding] = []
        entities = _model(context).entities
        for index in range(1, len(entities) - 1):
            previous, arc, following = entities[index - 1 : index + 2]
            if not (
                isinstance(previous.geometry, LineGeometry)
                and isinstance(arc.geometry, ArcGeometry)
                and isinstance(following.geometry, LineGeometry)
            ):
                continue
            error = _fillet_tangency_error(
                previous.geometry, arc.geometry, following.geometry, context
            )
            angular_bound = math.sin(math.radians(context.tolerance.angular_deg))
            if error is None or error <= angular_bound:
                continue
            findings.append(
                finding(
                    self.rule_id,
                    Severity.ERROR,
                    "Arc joining adjacent line edges is not tangent",
                    entity_ref=arc.entity_ref,
                    expected="tangent arc-to-line joins",
                    actual={"normalized_tangency_error": error},
                    tolerance=angular_bound,
                    suggested_fix="Rebuild the fillet from the adjacent line geometry",
                )
            )
        return findings


def _fillet_tangency_error(
    previous: LineGeometry,
    arc: ArcGeometry,
    following: LineGeometry,
    context: RuleContext,
) -> float | None:
    """Return a scale-free error for source-order line/arc/line fillet candidates."""
    if not math.isfinite(arc.radius_mm) or arc.radius_mm <= 0.0:
        return None
    try:
        center = Point2D(*arc.center_mm)
        start_angle, end_angle = math.radians(arc.start_angle_deg), math.radians(arc.end_angle_deg)
        arc_start = Point2D(
            center.x + arc.radius_mm * math.cos(start_angle),
            center.y + arc.radius_mm * math.sin(start_angle),
        )
        arc_end = Point2D(
            center.x + arc.radius_mm * math.cos(end_angle),
            center.y + arc.radius_mm * math.sin(end_angle),
        )
        previous_points = (Point2D(*previous.start_mm), Point2D(*previous.end_mm))
        following_points = (Point2D(*following.start_mm), Point2D(*following.end_mm))
        candidates: list[tuple[float, Point2D, Point2D, Point2D, Point2D]] = []
        for first_arc_point, second_arc_point in (
            (arc_start, arc_end),
            (arc_end, arc_start),
        ):
            previous_near = min(
                previous_points, key=lambda point: point.distance_to(first_arc_point)
            )
            following_near = min(
                following_points, key=lambda point: point.distance_to(second_arc_point)
            )
            candidates.append(
                (
                    previous_near.distance_to(first_arc_point)
                    + following_near.distance_to(second_arc_point),
                    first_arc_point,
                    second_arc_point,
                    previous_near,
                    following_near,
                )
            )
        _, first_arc_point, second_arc_point, previous_near, following_near = min(
            candidates, key=lambda item: item[0]
        )
        previous_far = next(point for point in previous_points if point != previous_near)
        following_far = next(point for point in following_points if point != following_near)
        endpoint_gap = max(
            previous_near.distance_to(first_arc_point),
            following_near.distance_to(second_arc_point),
        )
        if not context.tolerance.is_coincident(endpoint_gap):
            return None
        incoming = previous_far.vector_to(previous_near).normalized()
        outgoing = following_near.vector_to(following_far).normalized()
        start_radius = center.vector_to(first_arc_point)
        end_radius = center.vector_to(second_arc_point)
        return max(
            abs(start_radius.dot(incoming)) / arc.radius_mm,
            abs(end_radius.dot(outgoing)) / arc.radius_mm,
        )
    except (InvalidGeometryError, ValueError):
        return None
