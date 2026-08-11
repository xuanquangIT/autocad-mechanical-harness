"""Pure, deterministic material and cutting take-off over a normalized DrawingModel."""

from __future__ import annotations

import math
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal

from cad_harness.comprehension.contours import AssembledContour, ContourAnalysis, analyze_contours
from cad_harness.domain.errors import MissingRequiredInputsError
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    CircleGeometry,
    DrawingModel,
    EllipseGeometry,
    EntityRecord,
    LineGeometry,
    PolylineGeometry,
)
from cad_harness.domain.models.takeoff import (
    HoleGroup,
    MaterialEntry,
    MaterialTable,
    PartInput,
    PartTakeoffLine,
    TakeoffReport,
    TakeoffRequest,
)
from cad_harness.domain.models.validation import Finding, Severity
from cad_harness.geometry.areas import CurveContour, contour_perimeter
from cad_harness.geometry.curves import normalize_arc, normalize_ellipse
from cad_harness.geometry.intersections import circles_overlap
from cad_harness.geometry.predicates import point_in_contour
from cad_harness.geometry.primitives import Point2D, Polyline2D
from cad_harness.geometry.tolerance import ToleranceProfile

MM3_PER_M3 = Decimal("1000000000")
MASS_QUANTUM_KG = Decimal("0.001")

TAKEOFF_UNITS = {
    "density_kg_per_m3": "kg/m3",
    "thickness_mm": "mm",
    "quantity": "count",
    "net_area_mm2": "mm2",
    "gross_area_mm2": "mm2",
    "unit_mass_kg": "kg",
    "unit_mass_kg_raw": "kg",
    "unit_mass_kg_raw_text": "kg",
    "total_mass_kg": "kg",
    "total_mass_kg_raw": "kg",
    "total_mass_kg_raw_text": "kg",
    "cut_length_mm": "mm",
    "outer_cut_length_mm": "mm",
    "inner_cut_length_mm": "mm",
    "pierce_count": "count",
    "hole_groups.diameter_mm": "mm",
    "hole_groups.count": "count",
    "hole_groups": "diameter:mm,count:count",
    "weld_length_mm": "mm",
}


def _missing(message: str, *, path: str, **details: object) -> MissingRequiredInputsError:
    return MissingRequiredInputsError(
        message,
        required_action=(
            "Supply a valid explicit take-off input; values are never defaulted or clamped"
        ),
        details={"path": path, **details},
    )


def _validate_request(
    model: DrawingModel, request: TakeoffRequest, materials: MaterialTable
) -> dict[str, MaterialEntry]:
    if not model.geometry_normalized or model.to_mm_factor is None:
        raise _missing(
            "Take-off requires geometry already normalized to millimetres",
            path="drawing_model.geometry_normalized",
        )
    if request.document_id != model.document_id:
        raise _missing(
            "Take-off request targets a different document",
            path="document_id",
            expected=model.document_id,
            actual=request.document_id,
        )
    actual_ref = f"{materials.profile_id}@{materials.version}"
    if request.material_profile_ref != actual_ref:
        raise _missing(
            "Loaded material table does not match the requested version",
            path="material_profile_ref",
            expected=request.material_profile_ref,
            actual=actual_ref,
        )
    by_code = {entry.material_code: entry for entry in materials.entries}
    if len(by_code) != len(materials.entries):
        raise _missing(
            "Material table contains duplicate material codes",
            path="materials.entries",
        )
    for entry_index, entry in enumerate(materials.entries):
        if not math.isfinite(entry.density_kg_per_m3) or entry.density_kg_per_m3 <= 0.0:
            raise _missing(
                "Material density must be a positive finite value",
                path=f"materials.entries[{entry_index}].density_kg_per_m3",
                actual=entry.density_kg_per_m3,
            )
    return by_code


def _validate_part(
    part: PartInput, index: int, materials: dict[str, MaterialEntry]
) -> MaterialEntry:
    prefix = f"parts[{index}]"
    if not math.isfinite(part.thickness_mm) or not 0.5 <= part.thickness_mm <= 500.0:
        raise _missing(
            "Part thickness is outside the valid range",
            path=f"{prefix}.thickness_mm",
            valid_range=[0.5, 500.0],
            actual=part.thickness_mm,
        )
    if not 1 <= part.quantity <= 100_000:
        raise _missing(
            "Part quantity is outside the valid range",
            path=f"{prefix}.quantity",
            valid_range=[1, 100_000],
            actual=part.quantity,
        )
    if part.stock_allowance_mm is not None and (
        not math.isfinite(part.stock_allowance_mm) or not 0.0 <= part.stock_allowance_mm <= 500.0
    ):
        raise _missing(
            "Stock allowance is outside the valid range",
            path=f"{prefix}.stock_allowance_mm",
            valid_range=[0.0, 500.0],
            actual=part.stock_allowance_mm,
        )
    material = materials.get(part.material_code)
    if material is None:
        raise _missing(
            "Material code is absent from the selected table",
            path=f"{prefix}.material_code",
            actual=part.material_code,
            available_codes=sorted(materials),
        )
    return material


def _part_boundary_model(
    model: DrawingModel,
    part: PartInput,
    index: int,
    entities: dict[str, EntityRecord],
) -> DrawingModel:
    """Restrict cutting contours to the explicit outline's layer and space.

    Annotation and centre geometry can form geometrically closed loops. Treating those
    loops as material voids produces dangerously low areas and masses. The selected
    outline is therefore also the authoritative cutting-layer/space selector; hidden
    entities never contribute to quotation quantities.
    """
    outline = entities.get(part.outline_entity_ref)
    if outline is None:
        raise _missing(
            "Part outline entity does not exist in the drawing",
            path=f"parts[{index}].outline_entity_ref",
            actual=part.outline_entity_ref,
        )
    if not outline.visible:
        raise _missing(
            "Part outline entity must be visible for take-off",
            path=f"parts[{index}].outline_entity_ref",
            actual=part.outline_entity_ref,
        )
    explicit_inner_refs = set(part.inner_contour_entity_refs)
    if part.outline_entity_ref in explicit_inner_refs:
        raise _missing(
            "Part outline cannot also be an inner contour",
            path=f"parts[{index}].inner_contour_entity_refs",
            actual=part.outline_entity_ref,
        )
    for entity_ref in explicit_inner_refs:
        entity = entities.get(entity_ref)
        if entity is None or not entity.visible or entity.space != outline.space:
            raise _missing(
                "Explicit inner contour must identify visible geometry in the outline space",
                path=f"parts[{index}].inner_contour_entity_refs",
                actual=entity_ref,
            )
    layer = outline.layer.casefold()
    return model.model_copy(
        update={
            "entities": tuple(
                entity
                for entity in model.entities
                if entity.visible
                and entity.space == outline.space
                and (entity.layer.casefold() == layer or entity.entity_ref in explicit_inner_refs)
            )
        }
    )


def _descendants(analysis: ContourAnalysis, root_index: int) -> tuple[int, ...]:
    result: list[int] = []
    for index in range(len(analysis.forest.nodes)):
        cursor: int | None = index
        while cursor is not None and cursor != root_index:
            cursor = analysis.forest.nodes[cursor].parent_index
        if cursor == root_index:
            result.append(index)
    return tuple(result)


def _resolve_root(analysis: ContourAnalysis, part: PartInput, index: int) -> int:
    matches = [
        contour_index
        for contour_index, (contour, node) in enumerate(
            zip(analysis.contours, analysis.forest.nodes, strict=True)
        )
        if node.parent_index is None and part.outline_entity_ref in contour.entity_refs
    ]
    if len(matches) != 1:
        raise _missing(
            "Part outline must identify exactly one root contour",
            path=f"parts[{index}].outline_entity_ref",
            actual=part.outline_entity_ref,
            root_match_count=len(matches),
        )
    return matches[0]


def _circle(contour: AssembledContour, entities: dict[str, EntityRecord]) -> CircleGeometry | None:
    if not contour.is_circle or len(contour.entity_refs) != 1:
        return None
    geometry = entities[contour.entity_refs[0]].geometry
    return geometry if isinstance(geometry, CircleGeometry) else None


def _overlapping_holes(
    analysis: ContourAnalysis,
    indices: tuple[int, ...],
    entities: dict[str, EntityRecord],
) -> tuple[set[int], list[Finding]]:
    circles = [
        (index, geometry)
        for index in indices
        if (geometry := _circle(analysis.contours[index], entities)) is not None
    ]
    excluded: set[int] = set()
    findings: list[Finding] = []
    for position, (first_index, first) in enumerate(circles):
        for second_index, second in circles[position + 1 :]:
            if (
                analysis.forest.nodes[first_index].parent_index
                != analysis.forest.nodes[second_index].parent_index
            ):
                continue
            if not circles_overlap(
                Point2D(*first.center_mm),
                2.0 * first.radius_mm,
                Point2D(*second.center_mm),
                2.0 * second.radius_mm,
            ):
                continue
            first_ref = analysis.contours[first_index].entity_refs[0]
            second_ref = analysis.contours[second_index].entity_refs[0]
            overlap = (
                first.radius_mm
                + second.radius_mm
                - Point2D(*first.center_mm).distance_to(Point2D(*second.center_mm))
            )
            excluded.update((first_index, second_index))
            findings.append(
                Finding(
                    rule_id="OVERLAPPING_HOLES",
                    severity=Severity.ERROR,
                    message="Overlapping hole contours were excluded from the complete take-off",
                    entity_ref=first_ref,
                    expected={"overlap_mm": 0.0},
                    actual={"overlap_mm": overlap, "other_entity_ref": second_ref},
                    tolerance=0.0,
                    suggested_fix="Resolve the overlapping contours and run take-off again",
                    measurement={"overlap_mm": overlap, "entity_refs": [first_ref, second_ref]},
                )
            )
    return excluded, findings


def _as_polyline(contour: AssembledContour, tolerance: ToleranceProfile) -> Polyline2D:
    if isinstance(contour.contour, Polyline2D):
        return contour.contour
    return Polyline2D(
        contour.contour.vertices(tolerance.arc_chord_tolerance_mm),
        closed=True,
    )


def _signed_polygon_area(points: list[Point2D]) -> float:
    return (
        sum(
            first.x * second.y - second.x * first.y
            for first, second in zip(points, points[1:] + points[:1], strict=True)
        )
        / 2.0
    )


def _convex_clip(subject: list[Point2D], clip: Polyline2D) -> list[Point2D]:
    """Sutherland-Hodgman intersection used only to measure outside circle area."""

    clip_points = list(clip.vertices)
    orientation = 1.0 if _signed_polygon_area(clip_points) >= 0.0 else -1.0

    def inside(point: Point2D, start: Point2D, end: Point2D) -> bool:
        return orientation * start.vector_to(end).cross(start.vector_to(point)) >= -1.0e-12

    def intersection(
        first: Point2D, second: Point2D, clip_start: Point2D, clip_end: Point2D
    ) -> Point2D:
        direction = first.vector_to(second)
        clip_direction = clip_start.vector_to(clip_end)
        denominator = direction.cross(clip_direction)
        if abs(denominator) <= 1.0e-15:
            return second
        parameter = first.vector_to(clip_start).cross(clip_direction) / denominator
        return Point2D(first.x + parameter * direction.dx, first.y + parameter * direction.dy)

    output = subject
    for clip_start, clip_end in clip.segments:
        input_points = output
        output = []
        if not input_points:
            break
        previous = input_points[-1]
        for current in input_points:
            current_inside = inside(current, clip_start, clip_end)
            previous_inside = inside(previous, clip_start, clip_end)
            if current_inside:
                if not previous_inside:
                    output.append(intersection(previous, current, clip_start, clip_end))
                output.append(current)
            elif previous_inside:
                output.append(intersection(previous, current, clip_start, clip_end))
            previous = current
    return output


def _is_convex(polyline: Polyline2D) -> bool:
    signs: set[bool] = set()
    vertices = polyline.vertices
    for index in range(len(vertices)):
        first = vertices[index]
        second = vertices[(index + 1) % len(vertices)]
        third = vertices[(index + 2) % len(vertices)]
        cross = first.vector_to(second).cross(second.vector_to(third))
        if abs(cross) > 1.0e-12:
            signs.add(cross > 0.0)
    return len(signs) <= 1


def _point_in_triangle(point: Point2D, a: Point2D, b: Point2D, c: Point2D) -> bool:
    cross_ab = a.vector_to(b).cross(a.vector_to(point))
    cross_bc = b.vector_to(c).cross(b.vector_to(point))
    cross_ca = c.vector_to(a).cross(c.vector_to(point))
    return (cross_ab >= -1.0e-12 and cross_bc >= -1.0e-12 and cross_ca >= -1.0e-12) or (
        cross_ab <= 1.0e-12 and cross_bc <= 1.0e-12 and cross_ca <= 1.0e-12
    )


def _triangulate(polyline: Polyline2D) -> list[Polyline2D]:
    """Deterministic ear clipping for simple concave part outlines."""

    vertices = list(polyline.vertices)
    if _signed_polygon_area(vertices) < 0.0:
        vertices.reverse()
    remaining = list(range(len(vertices)))
    triangles: list[Polyline2D] = []
    while len(remaining) > 3:
        ear_found = False
        for position, current in enumerate(remaining):
            previous = remaining[position - 1]
            following = remaining[(position + 1) % len(remaining)]
            a, b, c = vertices[previous], vertices[current], vertices[following]
            if a.vector_to(b).cross(b.vector_to(c)) <= 1.0e-12:
                continue
            if any(
                _point_in_triangle(vertices[candidate], a, b, c)
                for candidate in remaining
                if candidate not in {previous, current, following}
            ):
                continue
            triangles.append(Polyline2D((a, b, c), closed=True))
            remaining.pop(position)
            ear_found = True
            break
        if not ear_found:
            return []
    if len(remaining) == 3:
        triangles.append(Polyline2D(tuple(vertices[index] for index in remaining), closed=True))
    return triangles


def _intersection_area(subject: list[Point2D], outer: Polyline2D) -> float:
    clips = [outer] if _is_convex(outer) else _triangulate(outer)
    return sum(
        abs(_signed_polygon_area(clipped))
        for clip in clips
        if len(clipped := _convex_clip(subject, clip)) >= 3
    )


def _circle_polygon(geometry: CircleGeometry, segments: int = 2048) -> list[Point2D]:
    center = Point2D(*geometry.center_mm)
    return [
        Point2D(
            center.x + geometry.radius_mm * math.cos(2.0 * math.pi * index / segments),
            center.y + geometry.radius_mm * math.sin(2.0 * math.pi * index / segments),
        )
        for index in range(segments)
    ]


def _outside_circle_findings(
    analysis: ContourAnalysis,
    root_index: int,
    subtree: tuple[int, ...],
    entities: dict[str, EntityRecord],
    tolerance: ToleranceProfile,
) -> list[Finding]:
    outer = _as_polyline(analysis.contours[root_index], tolerance)
    findings: list[Finding] = []
    for index, contour in enumerate(analysis.contours):
        if index in subtree or index == root_index:
            continue
        circle = _circle(contour, entities)
        if circle is None:
            continue
        polygon = _circle_polygon(circle)
        perimeter_inside = any(point_in_contour(outer, point, tolerance) for point in polygon)
        center_inside = point_in_contour(outer, Point2D(*circle.center_mm), tolerance)
        if not perimeter_inside and not center_inside:
            continue
        intersection_area = _intersection_area(polygon, outer)
        outside_area = max(0.0, math.pi * circle.radius_mm**2 - intersection_area)
        if outside_area <= tolerance.area_mm2:
            continue
        entity_ref = contour.entity_refs[0]
        findings.append(
            Finding(
                rule_id="HOLE_OUTSIDE_PART",
                severity=Severity.ERROR,
                message="Hole contour crosses the part boundary and was excluded completely",
                entity_ref=entity_ref,
                expected={"outside_area_mm2": 0.0},
                actual={"outside_area_mm2": outside_area},
                tolerance=tolerance.area_mm2,
                suggested_fix=(
                    "Move or resize the hole so its complete contour lies inside the part"
                ),
                measurement={"outside_area_mm2": outside_area},
            )
        )
    return findings


def _gross_area(
    contour: AssembledContour, allowance_mm: float | None, tolerance: ToleranceProfile
) -> float | None:
    if allowance_mm is None:
        return None
    box = _as_polyline(contour, tolerance).bounding_box()
    return (box.width + 2.0 * allowance_mm) * (box.height + 2.0 * allowance_mm)


def _hole_groups(
    analysis: ContourAnalysis,
    indices: tuple[int, ...],
    root_index: int,
    entities: dict[str, EntityRecord],
    tolerance: ToleranceProfile,
) -> tuple[HoleGroup, ...]:
    holes = sorted(
        (
            2.0 * geometry.radius_mm,
            analysis.contours[index].entity_refs[0],
        )
        for index in indices
        if index != root_index
        and (geometry := _circle(analysis.contours[index], entities)) is not None
    )
    groups: list[list[tuple[float, str]]] = []
    for diameter, entity_ref in holes:
        if not groups or diameter - groups[-1][0][0] > tolerance.absolute_length_mm:
            groups.append([(diameter, entity_ref)])
        else:
            groups[-1].append((diameter, entity_ref))
    return tuple(
        HoleGroup(
            diameter_mm=group[0][0],
            count=len(group),
            entity_refs=tuple(item[1] for item in group),
        )
        for group in groups
    )


def _entity_length(entity: EntityRecord) -> float | None:
    geometry = entity.geometry
    if isinstance(geometry, LineGeometry):
        return Point2D(*geometry.start_mm).distance_to(Point2D(*geometry.end_mm))
    if isinstance(geometry, ArcGeometry):
        return contour_perimeter(
            CurveContour(
                (
                    normalize_arc(
                        Point2D(*geometry.center_mm),
                        geometry.radius_mm,
                        geometry.start_angle_deg,
                        geometry.end_angle_deg,
                    ),
                )
            )
        )
    if isinstance(geometry, CircleGeometry):
        return 2.0 * math.pi * geometry.radius_mm
    if isinstance(geometry, EllipseGeometry):
        return contour_perimeter(
            CurveContour(
                (
                    normalize_ellipse(
                        Point2D(*geometry.center_mm),
                        geometry.major_axis_mm,
                        geometry.minor_axis_mm,
                        geometry.rotation_deg,
                    ),
                )
            )
        )
    if isinstance(geometry, PolylineGeometry):
        vertices = tuple(Point2D(*vertex.point_mm) for vertex in geometry.vertices)
        return Polyline2D(vertices, closed=geometry.closed).perimeter()
    return None


def _weld_length(
    request: TakeoffRequest,
    root: AssembledContour,
    entities: dict[str, EntityRecord],
    tolerance: ToleranceProfile,
) -> tuple[float, tuple[str, ...]]:
    root_polyline = _as_polyline(root, tolerance)
    owned: list[str] = []
    total = 0.0
    for entity_ref in request.weld_edges:
        entity = entities.get(entity_ref)
        if entity is None:
            raise _missing(
                "Declared weld edge does not exist in the drawing",
                path="weld_edges",
                entity_ref=entity_ref,
            )
        center = Point2D(
            (entity.bounding_box_mm[0] + entity.bounding_box_mm[2]) / 2.0,
            (entity.bounding_box_mm[1] + entity.bounding_box_mm[3]) / 2.0,
        )
        if not point_in_contour(root_polyline, center, tolerance):
            continue
        length = _entity_length(entity)
        if length is None:
            raise _missing(
                "Declared weld entity has no supported length",
                path="weld_edges",
                entity_ref=entity_ref,
                entity_type=entity.entity_type,
            )
        owned.append(entity_ref)
        total += length
    return total, tuple(owned)


def _raw_mass_text(value: Decimal) -> str:
    rendered = format(value, "f")
    whole, separator, fractional = rendered.partition(".")
    return f"{whole}.{fractional.ljust(6, '0')}" if separator else f"{whole}.000000"


def _mass(
    area_mm2: float, thickness_mm: float, density: float, quantity: int
) -> tuple[float, float, str, float, float, str]:
    raw = Decimal(str(area_mm2)) * Decimal(str(thickness_mm)) * Decimal(str(density)) / MM3_PER_M3
    total_raw = raw * Decimal(quantity)
    return (
        float(raw.quantize(MASS_QUANTUM_KG, rounding=ROUND_HALF_UP)),
        float(raw),
        _raw_mass_text(raw),
        float(total_raw.quantize(MASS_QUANTUM_KG, rounding=ROUND_HALF_UP)),
        float(total_raw),
        _raw_mass_text(total_raw),
    )


def _open_findings(analysis: ContourAnalysis, tolerance: ToleranceProfile) -> tuple[Finding, ...]:
    return tuple(
        Finding(
            rule_id="OPEN_CONTOUR",
            severity=Severity.ERROR,
            message="Open contour was excluded from all take-off quantities",
            entity_ref=item.endpoint_entity_refs[0],
            expected={"gap_mm": 0.0},
            actual={"gap_mm": item.gap_mm, "endpoint_entity_refs": item.endpoint_entity_refs},
            tolerance=tolerance.coincidence_mm,
            suggested_fix="Close the contour within the company tolerance and rerun take-off",
            measurement={"gap_mm": item.gap_mm},
        )
        for item in analysis.open_contours
    )


def compute_takeoff(
    model: DrawingModel,
    request: TakeoffRequest,
    *,
    materials: MaterialTable,
    tolerance: ToleranceProfile,
    checkpoint: Callable[[], None] | None = None,
) -> TakeoffReport:
    """Compute quantities without mutating the drawing or touching an adapter."""

    if checkpoint is not None:
        checkpoint()
    material_by_code = _validate_request(model, request, materials)
    entities = {entity.entity_ref: entity for entity in model.entities}
    lines: list[PartTakeoffLine] = []
    excluded_findings: list[Finding] = []
    used_roots: set[frozenset[str]] = set()
    for part_index, part in enumerate(request.parts):
        if checkpoint is not None:
            checkpoint()
        material = _validate_part(part, part_index, material_by_code)
        boundary_model = _part_boundary_model(model, part, part_index, entities)
        analysis = analyze_contours(boundary_model, tolerance)
        excluded_findings.extend(_open_findings(analysis, tolerance))
        if checkpoint is not None:
            checkpoint()
        root_index = _resolve_root(analysis, part, part_index)
        root_identity = frozenset(analysis.contours[root_index].entity_refs)
        if root_identity in used_roots:
            raise _missing(
                "A root contour cannot be assigned to more than one part",
                path=f"parts[{part_index}].outline_entity_ref",
                actual=part.outline_entity_ref,
            )
        used_roots.add(root_identity)
        subtree = _descendants(analysis, root_index)
        subtree_refs = {
            entity_ref
            for contour_index in subtree
            if contour_index != root_index
            for entity_ref in analysis.contours[contour_index].entity_refs
        }
        unresolved_inner_refs = set(part.inner_contour_entity_refs) - subtree_refs
        if unresolved_inner_refs:
            raise _missing(
                "Explicit inner contours must form closed boundaries inside the part outline",
                path=f"parts[{part_index}].inner_contour_entity_refs",
                unresolved_count=len(unresolved_inner_refs),
            )
        excluded_findings.extend(
            _outside_circle_findings(analysis, root_index, subtree, entities, tolerance)
        )
        overlap_exclusions, overlap_findings = _overlapping_holes(analysis, subtree, entities)
        excluded_findings.extend(overlap_findings)
        excluded = set(overlap_exclusions)
        for contour_index in tuple(excluded):
            excluded.update(_descendants(analysis, contour_index))
        valid_indices = tuple(index for index in subtree if index not in excluded)
        root_depth = analysis.forest.nodes[root_index].depth
        net_area = sum(
            analysis.forest.nodes[index].area_mm2
            * (1.0 if (analysis.forest.nodes[index].depth - root_depth) % 2 == 0 else -1.0)
            for index in valid_indices
        )
        outer_length = contour_perimeter(analysis.contours[root_index].contour)
        inner_length = sum(
            contour_perimeter(analysis.contours[index].contour)
            for index in valid_indices
            if index != root_index
        )
        (
            unit_mass,
            unit_mass_raw,
            unit_mass_raw_text,
            total_mass,
            total_mass_raw,
            total_mass_raw_text,
        ) = _mass(net_area, part.thickness_mm, material.density_kg_per_m3, part.quantity)
        weld_length, weld_refs = _weld_length(
            request, analysis.contours[root_index], entities, tolerance
        )
        all_refs = tuple(
            dict.fromkeys(
                entity_ref
                for index in valid_indices
                for entity_ref in analysis.contours[index].entity_refs
            )
        )
        inner_refs = tuple(
            dict.fromkeys(
                entity_ref
                for index in valid_indices
                if index != root_index
                for entity_ref in analysis.contours[index].entity_refs
            )
        )
        root_refs = analysis.contours[root_index].entity_refs
        mass_refs = all_refs or root_refs
        evidence = {
            "density_kg_per_m3": root_refs,
            "thickness_mm": root_refs,
            "quantity": root_refs,
            "net_area_mm2": mass_refs,
            "gross_area_mm2": root_refs,
            "unit_mass_kg": mass_refs,
            "unit_mass_kg_raw": mass_refs,
            "unit_mass_kg_raw_text": mass_refs,
            "total_mass_kg": mass_refs,
            "total_mass_kg_raw": mass_refs,
            "total_mass_kg_raw_text": mass_refs,
            "cut_length_mm": mass_refs,
            "outer_cut_length_mm": root_refs,
            "inner_cut_length_mm": inner_refs or root_refs,
            "pierce_count": mass_refs,
            "hole_groups": inner_refs or root_refs,
            "weld_length_mm": weld_refs or root_refs,
        }
        lines.append(
            PartTakeoffLine(
                part_code=part.part_code,
                material_code=material.material_code,
                density_kg_per_m3=material.density_kg_per_m3,
                thickness_mm=part.thickness_mm,
                quantity=part.quantity,
                net_area_mm2=net_area,
                gross_area_mm2=_gross_area(
                    analysis.contours[root_index], part.stock_allowance_mm, tolerance
                ),
                unit_mass_kg=unit_mass,
                unit_mass_kg_raw=unit_mass_raw,
                unit_mass_kg_raw_text=unit_mass_raw_text,
                total_mass_kg=total_mass,
                total_mass_kg_raw=total_mass_raw,
                total_mass_kg_raw_text=total_mass_raw_text,
                cut_length_mm=outer_length + inner_length,
                outer_cut_length_mm=outer_length,
                inner_cut_length_mm=inner_length,
                pierce_count=len(valid_indices),
                hole_groups=_hole_groups(analysis, valid_indices, root_index, entities, tolerance),
                weld_length_mm=weld_length,
                evidence=evidence,
            )
        )
    if checkpoint is not None:
        checkpoint()
    return TakeoffReport(
        document_id=model.document_id,
        revision=model.revision,
        profile_id=materials.profile_id,
        material_profile_id=materials.profile_id,
        material_profile_version=materials.version,
        company_approved=materials.company_approved,
        parts=tuple(lines),
        excluded_contours=tuple(excluded_findings),
        units=TAKEOFF_UNITS,
    )


__all__ = ["MM3_PER_M3", "TAKEOFF_UNITS", "compute_takeoff"]
