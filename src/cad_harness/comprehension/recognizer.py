"""Deterministic, AutoCAD-independent recognition of mechanical 2D features."""

from __future__ import annotations

import math
from collections.abc import Iterable
from itertools import pairwise

from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.comprehension.contours import AssembledContour, EdgeRecord, analyze_contours
from cad_harness.domain.models.drawing_model import CircleGeometry, DrawingModel, MeasuredValue
from cad_harness.domain.models.recognition import (
    CandidateExplanation,
    RecognitionReport,
    RecognizedFeature,
    RecognizedFeatureType,
)
from cad_harness.geometry.areas import CurveContour, LineEdge, contour_area
from cad_harness.geometry.curves import CurveKind, CurveParams
from cad_harness.geometry.intersections import angular_spacing_deg
from cad_harness.geometry.primitives import Point2D
from cad_harness.geometry.tolerance import ToleranceProfile


def _refs(records: Iterable[EdgeRecord]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(record.entity_ref for record in records))


def _measured(value: float, unit: str, entity_refs: tuple[str, ...]) -> MeasuredValue:
    return MeasuredValue(
        value=value,
        unit=unit,  # type: ignore[arg-type]
        provenance="measured",
        entity_refs=entity_refs,
    )


def _feature(
    feature_type: RecognizedFeatureType,
    entity_refs: tuple[str, ...],
    values: dict[str, tuple[float, str]],
    *,
    evidence: dict[str, float] | None = None,
) -> RecognizedFeature:
    return RecognizedFeature(
        feature_type=feature_type,
        source_revision="",
        entity_refs=entity_refs,
        parameters={
            name: _measured(value, unit, entity_refs) for name, (value, unit) in values.items()
        },
        evidence=evidence or {},
    )


def _vertices(contour: AssembledContour, tolerance: ToleranceProfile) -> tuple[Point2D, ...]:
    if isinstance(contour.contour, CurveContour):
        return contour.contour.vertices(tolerance.arc_chord_tolerance_mm)
    return contour.contour.vertices


def _point_on_contour(
    point: Point2D, contour: AssembledContour, tolerance: ToleranceProfile
) -> bool:
    vertices = _vertices(contour, tolerance)
    return any(point.distance_to(vertex) <= 1.0e-9 for vertex in vertices)


def _axis_aligned_rectangle(contour: AssembledContour, tolerance: ToleranceProfile) -> bool:
    if len(contour.edges) != 4 or any(
        not isinstance(record.edge, LineEdge) for record in contour.edges
    ):
        return False
    for record in contour.edges:
        edge = record.edge
        assert isinstance(edge, LineEdge)
        vector = edge.start.vector_to(edge.end)
        if not (
            tolerance.is_zero_length(abs(vector.dx)) or tolerance.is_zero_length(abs(vector.dy))
        ):
            return False
    return True


def _outline_feature(contour: AssembledContour, tolerance: ToleranceProfile) -> RecognizedFeature:
    vertices = _vertices(contour, tolerance)
    xs = tuple(point.x for point in vertices)
    ys = tuple(point.y for point in vertices)
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    rectangular = _axis_aligned_rectangle(contour, tolerance)
    return _feature(
        RecognizedFeatureType.PART_OUTLINE,
        contour.entity_refs,
        {
            "width_mm": (width, "mm"),
            "height_mm": (height, "mm"),
            "area_mm2": (contour_area(contour.contour), "mm2"),
            "origin_x_mm": (min(xs), "mm"),
            "origin_y_mm": (min(ys), "mm"),
        },
        evidence={"axis_aligned_rectangle": 1.0 if rectangular else 0.0},
    )


def _circle_data(model: DrawingModel) -> list[tuple[str, Point2D, float]]:
    circles: list[tuple[str, Point2D, float]] = []
    for entity in model.entities:
        geometry = entity.geometry
        if isinstance(geometry, CircleGeometry):
            circles.append(
                (
                    entity.entity_ref,
                    Point2D(*geometry.center_mm),
                    geometry.radius_mm,
                )
            )
    return circles


def _same_radius_groups(
    circles: list[tuple[str, Point2D, float]], tolerance: ToleranceProfile
) -> list[list[tuple[str, Point2D, float]]]:
    groups: list[list[tuple[str, Point2D, float]]] = []
    for circle in circles:
        for group in groups:
            if tolerance.length_close(circle[2], group[0][2]):
                group.append(circle)
                break
        else:
            groups.append([circle])
    return groups


def _axis_levels(values: tuple[float, ...], tolerance: ToleranceProfile) -> tuple[float, ...]:
    levels: list[list[float]] = []
    for value in sorted(values):
        if levels and tolerance.is_coincident(abs(value - levels[-1][-1])):
            levels[-1].append(value)
        else:
            levels.append([value])
    return tuple(sum(group) / len(group) for group in levels)


def _rectangular_pattern(
    group: list[tuple[str, Point2D, float]], tolerance: ToleranceProfile
) -> RecognizedFeature | None:
    if len(group) < 4:
        return None
    xs = _axis_levels(tuple(item[1].x for item in group), tolerance)
    ys = _axis_levels(tuple(item[1].y for item in group), tolerance)
    if len(xs) < 2 or len(ys) < 2 or len(xs) * len(ys) != len(group):
        return None
    if not all(
        any(tolerance.is_coincident(point.distance_to(Point2D(x, y))) for _, point, _ in group)
        for x in xs
        for y in ys
    ):
        return None
    pitch_x = sum(b - a for a, b in pairwise(xs)) / (len(xs) - 1)
    pitch_y = sum(b - a for a, b in pairwise(ys)) / (len(ys) - 1)
    refs = tuple(item[0] for item in group)
    return _feature(
        RecognizedFeatureType.RECTANGULAR_HOLE_PATTERN,
        refs,
        {
            "hole_diameter_mm": (2.0 * group[0][2], "mm"),
            "count_x": (float(len(xs)), "count"),
            "count_y": (float(len(ys)), "count"),
            "pitch_x_mm": (pitch_x, "mm"),
            "pitch_y_mm": (pitch_y, "mm"),
            "origin_x_mm": (xs[0], "mm"),
            "origin_y_mm": (ys[0], "mm"),
        },
        evidence={"maximum_grid_deviation_mm": 0.0},
    )


def _bolt_pattern(
    group: list[tuple[str, Point2D, float]], tolerance: ToleranceProfile
) -> RecognizedFeature | None:
    if len(group) < 3:
        return None
    center = Point2D(
        sum(item[1].x for item in group) / len(group),
        sum(item[1].y for item in group) / len(group),
    )
    radii = tuple(center.distance_to(item[1]) for item in group)
    mean_radius = sum(radii) / len(radii)
    max_deviation = max(abs(radius - mean_radius) for radius in radii)
    if max_deviation > tolerance.coincidence_mm:
        return None
    gaps = angular_spacing_deg(center, tuple(item[1] for item in group))
    expected_gap = 360.0 / len(group)
    if any(not tolerance.angle_close_deg(gap, expected_gap) for gap in gaps):
        return None
    first = group[0][1]
    start_angle = math.degrees(math.atan2(first.y - center.y, first.x - center.x)) % 360.0
    refs = tuple(item[0] for item in group)
    return _feature(
        RecognizedFeatureType.BOLT_CIRCLE_PATTERN,
        refs,
        {
            "hole_diameter_mm": (2.0 * group[0][2], "mm"),
            "pcd_mm": (2.0 * mean_radius, "mm"),
            "count": (float(len(group)), "count"),
            "center_x_mm": (center.x, "mm"),
            "center_y_mm": (center.y, "mm"),
            "start_angle_deg": (start_angle, "deg"),
            "max_center_deviation_mm": (max_deviation, "mm"),
        },
        evidence={"max_center_deviation_mm": max_deviation},
    )


def _slot_feature(
    contour: AssembledContour, tolerance: ToleranceProfile
) -> RecognizedFeature | None:
    if len(contour.edges) != 4:
        return None
    lines = [record for record in contour.edges if isinstance(record.edge, LineEdge)]
    arcs = [
        record
        for record in contour.edges
        if isinstance(record.edge, CurveParams) and record.edge.kind is CurveKind.ARC
    ]
    if len(lines) != 2 or len(arcs) != 2:
        return None
    first_arc = arcs[0].edge
    second_arc = arcs[1].edge
    assert isinstance(first_arc, CurveParams) and isinstance(second_arc, CurveParams)
    if (
        first_arc.radius_mm is None
        or second_arc.radius_mm is None
        or not tolerance.length_close(first_arc.radius_mm, second_arc.radius_mm)
        or not tolerance.angle_close_deg(abs(first_arc.sweep_deg), 180.0)
        or not tolerance.angle_close_deg(abs(second_arc.sweep_deg), 180.0)
    ):
        return None
    for arc_record in arcs:
        arc_index = contour.edges.index(arc_record)
        previous = contour.edges[(arc_index - 1) % len(contour.edges)].edge
        following = contour.edges[(arc_index + 1) % len(contour.edges)].edge
        arc = arc_record.edge
        assert isinstance(arc, CurveParams)
        if (
            not isinstance(previous, LineEdge)
            or not isinstance(following, LineEdge)
            or not _is_tangent(previous, arc, arc.start_point, tolerance)
            or not _is_tangent(following, arc, arc.end_point, tolerance)
        ):
            return None
    center_distance = first_arc.center.distance_to(second_arc.center)
    center = Point2D(
        (first_arc.center.x + second_arc.center.x) / 2.0,
        (first_arc.center.y + second_arc.center.y) / 2.0,
    )
    angle = math.degrees(
        math.atan2(
            second_arc.center.y - first_arc.center.y,
            second_arc.center.x - first_arc.center.x,
        )
    )
    return _feature(
        RecognizedFeatureType.SLOT,
        contour.entity_refs,
        {
            "length_mm": (center_distance + 2.0 * first_arc.radius_mm, "mm"),
            "width_mm": (2.0 * first_arc.radius_mm, "mm"),
            "center_x_mm": (center.x, "mm"),
            "center_y_mm": (center.y, "mm"),
            "angle_deg": (angle, "deg"),
        },
        evidence={"arc_radius_difference_mm": abs(first_arc.radius_mm - second_arc.radius_mm)},
    )


def _is_tangent(
    line: LineEdge, arc: CurveParams, point: Point2D, tolerance: ToleranceProfile
) -> bool:
    line_vector = line.start.vector_to(line.end)
    radius_vector = arc.center.vector_to(point)
    scale = line_vector.length * radius_vector.length
    if scale <= 0.0:
        return False
    normalized_dot = abs(line_vector.dot(radius_vector)) / scale
    angular_bound = math.sin(math.radians(max(tolerance.angular_deg, 1.0e-6)))
    return normalized_dot <= angular_bound


def _fillet_features(
    contour: AssembledContour, tolerance: ToleranceProfile
) -> list[RecognizedFeature]:
    results: list[RecognizedFeature] = []
    edges = contour.edges
    for index, record in enumerate(edges):
        arc = record.edge
        if (
            not isinstance(arc, CurveParams)
            or arc.kind is not CurveKind.ARC
            or arc.radius_mm is None
            or tolerance.angle_close_deg(abs(arc.sweep_deg), 180.0)
        ):
            continue
        previous = edges[(index - 1) % len(edges)].edge
        following = edges[(index + 1) % len(edges)].edge
        if not isinstance(previous, LineEdge) or not isinstance(following, LineEdge):
            continue
        if not _is_tangent(previous, arc, arc.start_point, tolerance) or not _is_tangent(
            following, arc, arc.end_point, tolerance
        ):
            continue
        refs = _refs((edges[(index - 1) % len(edges)], record, edges[(index + 1) % len(edges)]))
        results.append(
            _feature(
                RecognizedFeatureType.FILLET_CORNER,
                refs,
                {
                    "radius_mm": (arc.radius_mm, "mm"),
                    "center_x_mm": (arc.center.x, "mm"),
                    "center_y_mm": (arc.center.y, "mm"),
                    "start_angle_deg": (arc.start_angle_deg, "deg"),
                    "sweep_deg": (arc.sweep_deg, "deg"),
                },
                evidence={"tangent": 1.0, "source_edge_index": float(index)},
            )
        )
    return results


def _infinite_intersection(first: LineEdge, second: LineEdge) -> Point2D | None:
    first_vector = first.start.vector_to(first.end)
    second_vector = second.start.vector_to(second.end)
    denominator = first_vector.cross(second_vector)
    if abs(denominator) <= 1.0e-12:
        return None
    offset = first.start.vector_to(second.start)
    factor = offset.cross(second_vector) / denominator
    return Point2D(
        first.start.x + factor * first_vector.dx,
        first.start.y + factor * first_vector.dy,
    )


def _chamfer_features(
    contour: AssembledContour, tolerance: ToleranceProfile
) -> list[RecognizedFeature]:
    # A real chamfer replaces a corner with an edge that is materially shorter
    # than both adjacent legs.  Merely requiring it to be infinitesimally shorter
    # misclassifies tessellated arcs/circles, whose nearly equal chord lengths
    # differ only by floating-point noise, as dozens of tiny chamfers.
    maximum_adjacent_length_ratio = 0.75
    results: list[RecognizedFeature] = []
    edges = contour.edges
    for index, record in enumerate(edges):
        chamfer = record.edge
        previous_record = edges[(index - 1) % len(edges)]
        following_record = edges[(index + 1) % len(edges)]
        previous = previous_record.edge
        following = following_record.edge
        if not all(isinstance(edge, LineEdge) for edge in (previous, chamfer, following)):
            continue
        assert isinstance(previous, LineEdge)
        assert isinstance(chamfer, LineEdge)
        assert isinstance(following, LineEdge)
        chamfer_length = chamfer.start.distance_to(chamfer.end)
        adjacent_length = min(
            previous.start.distance_to(previous.end),
            following.start.distance_to(following.end),
        )
        if chamfer_length >= maximum_adjacent_length_ratio * adjacent_length:
            continue
        vertex = _infinite_intersection(previous, following)
        if vertex is None or _point_on_contour(vertex, contour, tolerance):
            continue
        distance_first = vertex.distance_to(chamfer.start)
        distance_second = vertex.distance_to(chamfer.end)
        if tolerance.is_zero_length(distance_first) or tolerance.is_zero_length(distance_second):
            continue
        toward_vertex = chamfer.start.vector_to(vertex).normalized()
        along_chamfer = chamfer.start.vector_to(chamfer.end).normalized()
        angle = math.degrees(math.acos(max(-1.0, min(1.0, toward_vertex.dot(along_chamfer)))))
        refs = _refs((previous_record, record, following_record))
        results.append(
            _feature(
                RecognizedFeatureType.CHAMFER_CORNER,
                refs,
                {
                    "distance_1_mm": (distance_first, "mm"),
                    "distance_2_mm": (distance_second, "mm"),
                    "angle_deg": (angle, "deg"),
                },
                evidence={
                    "inferred_vertex_x_mm": vertex.x,
                    "inferred_vertex_y_mm": vertex.y,
                    "source_edge_index": float(index),
                },
            )
        )
    return results


def _candidate(
    feature: RecognizedFeature, group_index: int, candidate_index: int, rationale: str
) -> CandidateExplanation:
    return CandidateExplanation(
        candidate_id=f"candidate:{group_index}:{candidate_index}:{feature.feature_type.value}",
        feature=feature,
        rationale=rationale,
    )


def recognize(
    model: DrawingModel,
    *,
    tolerance: ToleranceProfile,
    profile: CompanyProfile,
) -> RecognitionReport:
    """Recognize all supported interpretations without choosing among ambiguities."""

    # Loading a concrete profile is intentional even though recognition thresholds are
    # currently all computational tolerances: it prevents a future unprofiled call path.
    _ = profile.profile_id
    analysis = analyze_contours(model, tolerance)
    features: list[RecognizedFeature] = []
    ambiguous: list[tuple[CandidateExplanation, ...]] = []

    slot_by_contour: dict[int, RecognizedFeature] = {}
    for index, contour in enumerate(analysis.contours):
        if not contour.is_circle:
            slot = _slot_feature(contour, tolerance)
            if slot is not None:
                slot_by_contour[index] = slot

    for index, node in enumerate(analysis.forest.nodes):
        contour = analysis.contours[index]
        if node.depth != 0 or contour.is_circle:
            continue
        outline = _outline_feature(contour, tolerance)
        slot = slot_by_contour.get(index)
        if slot is None:
            features.append(outline)
        else:
            group_index = len(ambiguous)
            ambiguous.append(
                (
                    _candidate(
                        outline, group_index, 0, "Closed root contour may be a part outline"
                    ),
                    _candidate(
                        slot, group_index, 1, "Two tangent semicircles and two flanks form a slot"
                    ),
                )
            )

    circle_by_ref = {item[0]: item for item in _circle_data(model)}
    circles_by_root: dict[int, list[tuple[str, Point2D, float]]] = {}
    for index, node in enumerate(analysis.forest.nodes):
        contour = analysis.contours[index]
        if not contour.is_circle or node.depth % 2 != 1:
            continue
        root_index = index
        parent = node.parent_index
        while parent is not None:
            root_index = parent
            parent = analysis.forest.nodes[parent].parent_index
        circle = circle_by_ref.get(contour.entity_refs[0])
        if circle is not None:
            circles_by_root.setdefault(root_index, []).append(circle)
    circles = [circle for root_group in circles_by_root.values() for circle in root_group]
    for entity_ref, _, radius in circles:
        features.append(
            _feature(
                RecognizedFeatureType.CIRCULAR_HOLE,
                (entity_ref,),
                {
                    "diameter_mm": (2.0 * radius, "mm"),
                    "center_x_mm": (circle_by_ref[entity_ref][1].x, "mm"),
                    "center_y_mm": (circle_by_ref[entity_ref][1].y, "mm"),
                },
            )
        )

    for root_circles in circles_by_root.values():
        for group in _same_radius_groups(root_circles, tolerance):
            rectangular = _rectangular_pattern(group, tolerance)
            bolt = _bolt_pattern(group, tolerance)
            candidates = tuple(item for item in (rectangular, bolt) if item is not None)
            if len(candidates) == 1:
                features.append(candidates[0])
            elif len(candidates) > 1:
                group_index = len(ambiguous)
                ambiguous.append(
                    tuple(
                        _candidate(
                            item,
                            group_index,
                            candidate_index,
                            "The same hole centers satisfy this pattern within tolerance",
                        )
                        for candidate_index, item in enumerate(candidates)
                    )
                )

    for contour in analysis.contours:
        if contour.is_circle:
            continue
        features.extend(_fillet_features(contour, tolerance))
        features.extend(_chamfer_features(contour, tolerance))

    bound_features = tuple(
        feature.model_copy(update={"source_revision": model.revision}) for feature in features
    )
    bound_ambiguous = tuple(
        tuple(
            candidate.model_copy(
                update={
                    "feature": candidate.feature.model_copy(
                        update={"source_revision": model.revision}
                    )
                }
            )
            for candidate in group
        )
        for group in ambiguous
    )
    return RecognitionReport(
        document_id=model.document_id,
        revision=model.revision,
        features=bound_features,
        ambiguous_groups=bound_ambiguous,
        open_contours=analysis.open_contours,
    )
