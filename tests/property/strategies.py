"""Shared Hypothesis generators for the property suite (design.md, Testing Strategy).

Every property test draws from here instead of hand-rolling its own generator, so the
edge cases the design calls out are covered once and stay covered:

- ``outlines()``      closed, simple polygons: rectangle, L-shape, convex, concave,
                      plus deliberately pathological variants with a very short edge or
                      a very narrow interior angle (Requirement 12.4).
- ``rigid_motions()`` ``(dx, dy, theta)`` translations and rotations, with theta hitting
                      0 / 90 / 180 / 270 exactly as well as arbitrary values
                      (Requirement 12.4).
- ``paths()``         export targets covering ``..`` traversal, UNC shares and non-ASCII
                      filenames (Requirement 18.6).
- ``curve_params()``  raw curved-edge inputs: arc, circle, ellipse and polyline bulge,
                      including negative bulge, sweeps close to 360 degrees and
                      near-circular ellipses (Requirement 13.12).
- ``nested_contour_forests()`` concentric forests of depth 0-3 with a closed-form
                      even/odd net area (Requirement 16.1).
- ``takeoff_requests()`` user part inputs at material, thickness, quantity, and stock
                      allowance boundaries (Requirement 16.4, 16.6, 16.11).

Two construction invariants make the polygon generators usable without a filter:

1. Polygons built in polar space are simple when the vertex angles increase strictly and
   every gap - including the one that closes the loop - stays below 180 degrees: each
   ray from the centre then crosses the boundary exactly once. ``_ring_angles`` places
   one jittered vertex per equal sector, which guarantees both conditions.
2. Inserting a vertex on an existing edge, or replacing one ring vertex with a radial
   spike, preserves that monotonicity. So the pathological variants stay simple too.

Angles are in degrees, measured from the positive X axis, counter-clockwise positive -
the same convention as ``CurveParams`` in the design. Lengths are millimetres.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from hypothesis import strategies as st

from cad_harness.domain.models.document import LayerInfo
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    BlockReferenceGeometry,
    CircleGeometry,
    DrawingModel,
    EllipseGeometry,
    EntityRecord,
    LineGeometry,
    PolylineGeometry,
    PolylineVertex,
    ReadScope,
    UnsupportedEntityCount,
)
from cad_harness.domain.models.metrics import BaselineCase, EffortRecord, FailureReason
from cad_harness.domain.models.recognition import RecognizedFeatureType
from cad_harness.domain.models.takeoff import PartInput, TakeoffRequest
from cad_harness.geometry.areas import ContourForest
from cad_harness.geometry.fillet_chamfer import fillet_vertex
from cad_harness.geometry.patterns import bolt_circle, rectangular_grid, slot_end_arcs, slot_outline
from cad_harness.geometry.primitives import Point2D, Polyline2D
from cad_harness.geometry.tolerance import DEMO_TOLERANCE
from cad_harness.metrics.collector import EngineerActivityInterval
from cad_harness.observability.audit import AuditEvent, AuditEventType

# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #

#: Coordinates stay modest so shoelace areas of slivers keep their significant digits.
MAX_COORDINATE_MM = 1.0e3
MIN_RADIUS_MM = 1.0
MAX_RADIUS_MM = 500.0
MIN_EXTENT_MM = 1.0
MAX_EXTENT_MM = 1.0e3

#: A "very short" edge sits at or below ``ToleranceProfile.absolute_length_mm`` (1e-3),
#: which is exactly where zero-length detection has to make a decision.
MIN_SHORT_EDGE_MM = 1.0e-6
MAX_SHORT_EDGE_MM = 1.0e-3

#: Half-width of a spike, in degrees. Kept far below the smallest angular gap a ring can
#: have (0.75 * 360/12 = 22.5 degrees) so the spike cannot overtake its neighbours.
MAX_SPIKE_HALF_WIDTH_DEG = 0.05

coordinates = st.floats(
    min_value=-MAX_COORDINATE_MM,
    max_value=MAX_COORDINATE_MM,
    allow_nan=False,
    allow_infinity=False,
)
radii_mm = st.floats(
    min_value=MIN_RADIUS_MM, max_value=MAX_RADIUS_MM, allow_nan=False, allow_infinity=False
)
extents_mm = st.floats(
    min_value=MIN_EXTENT_MM, max_value=MAX_EXTENT_MM, allow_nan=False, allow_infinity=False
)
angles_deg = st.floats(min_value=-360.0, max_value=360.0, allow_nan=False, allow_infinity=False)
centers = st.builds(Point2D, coordinates, coordinates)


# --------------------------------------------------------------------------- #
# Material take-off requests
# --------------------------------------------------------------------------- #

_DEMO_MATERIAL_CODES = ("SS400", "S355", "SUS304", "AL6061")
_TAKEOFF_THICKNESSES_MM = st.one_of(
    st.sampled_from((0.5, 500.0)),
    st.floats(min_value=0.500_001, max_value=499.999_999, allow_nan=False),
)
_TAKEOFF_QUANTITIES = st.one_of(
    st.sampled_from((1, 100_000)),
    st.integers(min_value=2, max_value=99_999),
)
_STOCK_ALLOWANCES_MM = st.one_of(
    st.none(),
    st.sampled_from((0.0, 500.0)),
    st.floats(min_value=0.000_001, max_value=499.999_999, allow_nan=False),
)


@st.composite
def takeoff_requests(draw: st.DrawFn) -> TakeoffRequest:
    """Valid requests covering exact engine-validation boundaries (Requirement 18.20)."""

    part_index = draw(st.integers(min_value=1, max_value=9_999))
    return TakeoffRequest(
        document_id=f"doc_takeoff_{part_index}",
        parts=(
            PartInput(
                part_code=f"P-{part_index:04d}",
                outline_entity_ref=f"outline-{part_index}",
                thickness_mm=draw(_TAKEOFF_THICKNESSES_MM),
                material_code=draw(st.sampled_from(_DEMO_MATERIAL_CODES)),
                quantity=draw(_TAKEOFF_QUANTITIES),
                stock_allowance_mm=draw(_STOCK_ALLOWANCES_MM),
            ),
        ),
        material_profile_ref="demo-materials@1.0",
    )


# --------------------------------------------------------------------------- #
# Outlines
# --------------------------------------------------------------------------- #


def _polar_vertices(
    center: Point2D, angles: list[float], radii: list[float]
) -> tuple[Point2D, ...]:
    return tuple(
        Point2D(
            center.x + radius * math.cos(math.radians(angle)),
            center.y + radius * math.sin(math.radians(angle)),
        )
        for angle, radius in zip(angles, radii, strict=True)
    )


#: Jitter is capped at a quarter of a sector, which bounds every angular gap to
#: ``[0.75, 1.25] * sector``. That keeps each gap strictly positive and strictly below
#: 180 degrees even for a triangle - both are needed for the star-shape argument.
_MAX_JITTER_FRACTION = 0.25


@st.composite
def _ring_angles(draw: st.DrawFn, *, min_vertices: int = 3, max_vertices: int = 12) -> list[float]:
    """Strictly increasing angles that wrap monotonically around the full circle.

    One vertex per equal sector, jittered inside its sector. Sector order plus a bounded
    jitter means no gap can reach 180 degrees, so the polygon is star-shaped about the
    centre and therefore simple.
    """
    count = draw(st.integers(min_value=min_vertices, max_value=max_vertices))
    sector = 360.0 / count
    start = draw(st.floats(min_value=0.0, max_value=360.0, allow_nan=False))
    jitters = draw(
        st.lists(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
            min_size=count,
            max_size=count,
        )
    )
    return [
        start + index * sector + jitter * _MAX_JITTER_FRACTION * sector
        for index, jitter in enumerate(jitters)
    ]


@st.composite
def rectangles(draw: st.DrawFn) -> Polyline2D:
    """Axis-aligned rectangle, counter-clockwise from the lower-left corner."""
    origin = draw(centers)
    width = draw(extents_mm)
    height = draw(extents_mm)
    return Polyline2D(
        (
            origin,
            Point2D(origin.x + width, origin.y),
            Point2D(origin.x + width, origin.y + height),
            Point2D(origin.x, origin.y + height),
        ),
        closed=True,
    )


@st.composite
def l_shapes(draw: st.DrawFn) -> Polyline2D:
    """Six-vertex L: a rectangle with the upper-right corner notched out."""
    origin = draw(centers)
    width = draw(extents_mm)
    height = draw(extents_mm)
    # Fractions keep the notch strictly inside the rectangle, so the outline stays simple.
    notch_x = width * draw(st.floats(min_value=0.2, max_value=0.8))
    notch_y = height * draw(st.floats(min_value=0.2, max_value=0.8))
    return Polyline2D(
        (
            origin,
            Point2D(origin.x + width, origin.y),
            Point2D(origin.x + width, origin.y + notch_y),
            Point2D(origin.x + notch_x, origin.y + notch_y),
            Point2D(origin.x + notch_x, origin.y + height),
            Point2D(origin.x, origin.y + height),
        ),
        closed=True,
    )


@st.composite
def convex_polygons(draw: st.DrawFn) -> Polyline2D:
    """Vertices on a common circle in angular order, which is convex by construction."""
    center = draw(centers)
    angles = draw(_ring_angles())
    radius = draw(radii_mm)
    return Polyline2D(_polar_vertices(center, angles, [radius] * len(angles)), closed=True)


@st.composite
def concave_polygons(draw: st.DrawFn) -> Polyline2D:
    """Star-shaped polygon with varying radii, so most samples have reflex vertices."""
    center = draw(centers)
    angles = draw(_ring_angles(min_vertices=4))
    radii = draw(
        st.lists(radii_mm, min_size=len(angles), max_size=len(angles)).filter(
            # At least one notch: otherwise this is just convex_polygons() again.
            lambda values: max(values) > 2.0 * min(values)
        )
    )
    return Polyline2D(_polar_vertices(center, angles, radii), closed=True)


@st.composite
def outlines_with_short_edge(draw: st.DrawFn) -> Polyline2D:
    """A valid outline with one edge at or below the zero-length tolerance.

    The extra vertex is placed *on* an existing edge, so the polygon stays simple and
    its area is unchanged - only the segment length becomes pathological.
    """
    base = draw(st.one_of(rectangles(), l_shapes(), convex_polygons(), concave_polygons()))
    vertices = base.vertices
    index = draw(st.integers(min_value=0, max_value=len(vertices) - 1))
    epsilon = draw(
        st.floats(
            min_value=MIN_SHORT_EDGE_MM,
            max_value=MAX_SHORT_EDGE_MM,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    start = vertices[index]
    direction = start.vector_to(vertices[(index + 1) % len(vertices)]).normalized().scaled(epsilon)
    inserted = Point2D(start.x + direction.dx, start.y + direction.dy)
    return Polyline2D((*vertices[: index + 1], inserted, *vertices[index + 1 :]), closed=True)


@st.composite
def outlines_with_narrow_angle(draw: st.DrawFn) -> Polyline2D:
    """A valid outline with one very narrow interior angle.

    One ring vertex is replaced by a radial spike: two vertices a hair either side of it
    at the ring radius, and a tip far outside. Both flanks of the spike are nearly
    radial, so the interior angle at the tip is well under a degree.
    """
    center = draw(centers)
    angles = draw(_ring_angles(max_vertices=8))
    radius = draw(radii_mm)
    tip_factor = draw(st.floats(min_value=2.0, max_value=20.0))
    half_width = draw(
        st.floats(min_value=1.0e-4, max_value=MAX_SPIKE_HALF_WIDTH_DEG, allow_nan=False)
    )
    index = draw(st.integers(min_value=0, max_value=len(angles) - 1))

    spike_angle = angles[index]
    spiked_angles: list[float] = []
    spiked_radii: list[float] = []
    for position, angle in enumerate(angles):
        if position == index:
            spiked_angles.extend([spike_angle - half_width, spike_angle, spike_angle + half_width])
            spiked_radii.extend([radius, radius * tip_factor, radius])
        else:
            spiked_angles.append(angle)
            spiked_radii.append(radius)
    return Polyline2D(_polar_vertices(center, spiked_angles, spiked_radii), closed=True)


def outlines(*, include_pathological: bool = True) -> st.SearchStrategy[Polyline2D]:
    """Closed, simple outlines: rectangle, L-shape, convex and concave polygons.

    Args:
        include_pathological: also draw outlines with a very short edge or a very narrow
            interior angle. Turn this off only for a property that is explicitly about
            well-conditioned geometry, and say why in the test.
    """
    well_formed = [rectangles(), l_shapes(), convex_polygons(), concave_polygons()]
    if not include_pathological:
        return st.one_of(*well_formed)
    return st.one_of(*well_formed, outlines_with_short_edge(), outlines_with_narrow_angle())


# --------------------------------------------------------------------------- #
# Rigid motions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RigidMotion:
    """Rotation about the origin followed by a translation.

    Order matters and is fixed: rotate, then translate. A property that needs the other
    order composes two motions instead of reinterpreting this one.
    """

    dx_mm: float
    dy_mm: float
    rotation_deg: float

    def apply(self, point: Point2D) -> Point2D:
        return point.rotated(self.rotation_deg).translated(self.dx_mm, self.dy_mm)

    def apply_to_outline(self, outline: Polyline2D) -> Polyline2D:
        return Polyline2D(
            tuple(self.apply(vertex) for vertex in outline.vertices), closed=outline.closed
        )

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.dx_mm, self.dy_mm, self.rotation_deg)


#: Right angles and full turns are where a naive implementation loses a sign, and where
#: sin/cos land on values that are exact in binary. Sampling them explicitly stops
#: Hypothesis from having to guess them.
CARDINAL_ROTATIONS_DEG = (0.0, 90.0, 180.0, 270.0, 360.0, -90.0, -180.0, -270.0, -360.0)


def rotations_deg() -> st.SearchStrategy[float]:
    return st.one_of(st.sampled_from(CARDINAL_ROTATIONS_DEG), angles_deg)


def rigid_motions() -> st.SearchStrategy[RigidMotion]:
    """``(dx, dy, theta)`` with the cardinal rotations over-sampled."""
    offsets = st.one_of(st.just(0.0), coordinates)
    return st.builds(RigidMotion, offsets, offsets, rotations_deg())


# --------------------------------------------------------------------------- #
# Export paths
# --------------------------------------------------------------------------- #

_EXTENSIONS = (".json", ".csv", ".dxf", ".svg")

#: Non-ASCII stems drawn from the scripts this project actually sees: Vietnamese part
#: names, Japanese drawing titles, Cyrillic. Spaces included on purpose.
_UNICODE_STEMS = (
    "takeoff",
    "báo-giá",
    "tấm đế 160x100",
    "図面-01",
    "чертёж",
    "bản vẽ (rev B)",
)

_RESERVED_WINDOWS_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)

_ILLEGAL_WINDOWS_CHARS = '<>:"/\\|?*'


def _is_usable_stem(stem: str) -> bool:
    """Reject names Windows itself would refuse, so tests fail on policy, not on I/O."""
    if not stem or stem != stem.strip(" ."):
        return False
    return stem.upper() not in _RESERVED_WINDOWS_NAMES


def filename_stems() -> st.SearchStrategy[str]:
    generated = st.text(
        alphabet=st.characters(
            min_codepoint=32,
            exclude_categories=("Cc", "Cs", "Co", "Cn", "Zl", "Zp"),
            exclude_characters=_ILLEGAL_WINDOWS_CHARS,
        ),
        min_size=1,
        max_size=16,
    )
    return st.one_of(st.sampled_from(_UNICODE_STEMS), generated).filter(_is_usable_stem)


def filenames() -> st.SearchStrategy[str]:
    return st.builds(lambda stem, ext: stem + ext, filename_stems(), st.sampled_from(_EXTENSIONS))


def _directory_segments() -> st.SearchStrategy[list[str]]:
    return st.lists(filename_stems(), min_size=0, max_size=3)


@st.composite
def contained_paths(draw: st.DrawFn) -> Path:
    """Relative export path that stays inside whatever root it is joined to.

    No ``..``, not absolute, so ``(root / result).resolve()`` is always under ``root``.
    """
    segments = draw(_directory_segments())
    return Path(*segments, draw(filenames()))


@st.composite
def traversal_paths(draw: st.DrawFn) -> Path:
    """Relative path that escapes any root it is joined to via ``..`` segments.

    More ``..`` than preceding real segments, so the escape does not depend on how deep
    the root happens to be.
    """
    segments = draw(_directory_segments())
    extra = draw(st.integers(min_value=1, max_value=3))
    upwards = [".."] * (len(segments) + extra)
    return Path(*segments, *upwards, draw(filenames()))


@st.composite
def unc_paths(draw: st.DrawFn) -> Path:
    """UNC share target: absolute, and never inside a local allowlist."""
    server = draw(st.sampled_from(("fileserver", "nas01", "máy-chủ")))
    share = draw(st.sampled_from(("drawings", "public$", "công-trình")))
    segments = draw(_directory_segments())
    return Path(f"//{server}/{share}", *segments, draw(filenames()))


@st.composite
def absolute_outside_paths(draw: st.DrawFn) -> Path:
    """Absolute local path in a location no allowlist should ever contain."""
    root = draw(st.sampled_from(("C:/Windows/System32", "C:/Program Files", "D:/")))
    return Path(root, *draw(_directory_segments()), draw(filenames()))


def escaping_paths() -> st.SearchStrategy[Path]:
    """Paths that must be refused by ``ensure_path_allowed`` for any local root."""
    return st.one_of(traversal_paths(), unc_paths(), absolute_outside_paths())


def paths() -> st.SearchStrategy[Path]:
    """Export path candidates, safe and hostile mixed.

    Results are relative (join them to an allowlisted root) or absolute (joining is a
    no-op under ``pathlib`` semantics), so a test can uniformly write ``root / drawn``.
    """
    return st.one_of(contained_paths(), escaping_paths())


# --------------------------------------------------------------------------- #
# Curved edges
# --------------------------------------------------------------------------- #


class CurveKind(StrEnum):
    ARC = "arc"
    CIRCLE = "circle"
    ELLIPSE = "ellipse"
    BULGE = "bulge"


@dataclass(frozen=True, slots=True)
class CircleCase:
    """A full circle. ``kind`` lets a test branch without ``isinstance`` chains."""

    center: Point2D
    radius_mm: float
    kind: CurveKind = CurveKind.CIRCLE


@dataclass(frozen=True, slots=True)
class ArcCase:
    center: Point2D
    radius_mm: float
    start_angle_deg: float
    #: Counter-clockwise sweep from ``start_angle_deg``, in (0, 360].
    sweep_deg: float
    kind: CurveKind = CurveKind.ARC

    @property
    def end_angle_deg(self) -> float:
        return self.start_angle_deg + self.sweep_deg


@dataclass(frozen=True, slots=True)
class EllipseCase:
    center: Point2D
    semi_major_mm: float
    semi_minor_mm: float
    #: Rotation of the major axis from the positive X axis.
    rotation_deg: float
    start_angle_deg: float
    sweep_deg: float
    kind: CurveKind = CurveKind.ELLIPSE

    @property
    def axis_ratio(self) -> float:
        return self.semi_minor_mm / self.semi_major_mm


@dataclass(frozen=True, slots=True)
class BulgeCase:
    """A polyline bulge: two vertices plus the DXF bulge factor.

    ``bulge`` is ``tan(sweep / 4)``: negative means clockwise, and the magnitude grows
    without bound as the arc approaches a full circle.
    """

    start: Point2D
    end: Point2D
    bulge: float
    kind: CurveKind = CurveKind.BULGE


CurveCase = CircleCase | ArcCase | EllipseCase | BulgeCase


def circle_cases() -> st.SearchStrategy[CircleCase]:
    return st.builds(CircleCase, centers, radii_mm)


def arc_cases() -> st.SearchStrategy[ArcCase]:
    #: Sweeps near 360 are where a normaliser confuses "full circle" with "almost".
    sweeps = st.one_of(
        st.sampled_from((1.0e-3, 0.5, 90.0, 180.0, 359.0, 359.999, 360.0)),
        st.floats(min_value=1.0e-3, max_value=360.0, allow_nan=False, allow_infinity=False),
    )
    return st.builds(ArcCase, centers, radii_mm, angles_deg, sweeps)


def ellipse_cases() -> st.SearchStrategy[EllipseCase]:
    #: Ratios near 1 must still be reported as an ellipse, not rounded to a circle.
    ratios = st.one_of(
        st.sampled_from((1.0, 0.999999, 0.99, 0.5, 1.0e-3)),
        st.floats(min_value=1.0e-3, max_value=1.0, allow_nan=False, allow_infinity=False),
    )
    return st.builds(
        lambda center, semi_major, ratio, rotation, start, sweep: EllipseCase(
            center=center,
            semi_major_mm=semi_major,
            semi_minor_mm=semi_major * ratio,
            rotation_deg=rotation,
            start_angle_deg=start,
            sweep_deg=sweep,
        ),
        centers,
        radii_mm,
        ratios,
        angles_deg,
        angles_deg,
        st.floats(min_value=1.0e-3, max_value=360.0, allow_nan=False, allow_infinity=False),
    )


def bulge_cases() -> st.SearchStrategy[BulgeCase]:
    magnitudes = st.one_of(
        # 1.0 is the exact semicircle; 1e-4 is all but straight; 1e3 is a near-full turn.
        st.sampled_from((1.0e-4, 0.5, 1.0, 5.0, 1.0e3)),
        st.floats(min_value=1.0e-4, max_value=1.0e3, allow_nan=False, allow_infinity=False),
    )
    signed = st.builds(
        lambda magnitude, negative: -magnitude if negative else magnitude, magnitudes, st.booleans()
    )
    chords = st.tuples(centers, centers).filter(
        # A bulge needs two distinct vertices to define a chord.
        lambda pair: pair[0].distance_to(pair[1]) > MIN_EXTENT_MM
    )
    return st.builds(
        lambda chord, bulge: BulgeCase(start=chord[0], end=chord[1], bulge=bulge), chords, signed
    )


def curve_params() -> st.SearchStrategy[CurveCase]:
    """Raw curved edges as a reader receives them, before normalisation.

    Covers all four inputs of Requirement 13.12 plus the edge cases the design names:
    negative bulge, sweeps close to 360 degrees, near-circular ellipses.
    """
    return st.one_of(circle_cases(), arc_cases(), ellipse_cases(), bulge_cases())


# --------------------------------------------------------------------------- #
# Nested contour forests
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class NestedContourForestCase:
    forest: ContourForest
    expected_net_area_mm2: float
    requested_depth: int


@st.composite
def nested_contour_forests(draw: st.DrawFn) -> NestedContourForestCase:
    """Concentric square nesting of depth 0-3 with analytic even/odd area.

    Depth two is the important hole-in-island topology: material, void, material.
    Concentric geometry makes parentage unambiguous while translations and arbitrary
    shrink ratios still exercise the containment implementation.
    """
    center = draw(centers)
    outer_side = draw(st.floats(min_value=10.0, max_value=500.0, allow_nan=False))
    depth = draw(st.integers(min_value=0, max_value=3))
    shrink = draw(st.floats(min_value=0.25, max_value=0.75, allow_nan=False))
    contours: list[Polyline2D] = []
    expected = 0.0
    side = outer_side
    for level in range(depth + 1):
        half = side / 2.0
        contours.append(
            Polyline2D(
                (
                    Point2D(center.x - half, center.y - half),
                    Point2D(center.x + half, center.y - half),
                    Point2D(center.x + half, center.y + half),
                    Point2D(center.x - half, center.y + half),
                ),
                closed=True,
            )
        )
        expected += side * side * (1.0 if level % 2 == 0 else -1.0)
        side *= shrink
    return NestedContourForestCase(
        ContourForest.build(tuple(contours), DEMO_TOLERANCE), expected, depth
    )


# --------------------------------------------------------------------------- #
# Drawing models and deliberate audit defects
# --------------------------------------------------------------------------- #


class DrawingScenario(StrEnum):
    EMPTY = "empty"
    FROZEN_LAYER = "frozen_layer"
    OFF_LAYER = "off_layer"
    PAPER_SPACE = "paper_space"
    NESTED_BLOCK = "nested_block"
    NON_UNIFORM_SCALE = "non_uniform_scale"
    UNKNOWN_UNITS = "unknown_units"
    UNSUPPORTED = "unsupported"


class DrawingDefect(StrEnum):
    ZERO_LENGTH = "zero_length"
    NON_FINITE = "non_finite"
    OPEN_CONTOUR = "open_contour"
    SELF_INTERSECTION = "self_intersection"
    DUPLICATE = "duplicate"
    OVERLAP = "overlap"
    HOLE_OUTSIDE = "hole_outside"
    HOLE_EDGE_DISTANCE = "hole_edge_distance"
    HOLE_HOLE_DISTANCE = "hole_hole_distance"
    INVALID_RADIUS = "invalid_radius"
    NON_TANGENT_FILLET = "non_tangent_fillet"


@dataclass(frozen=True, slots=True)
class DefectiveDrawingModelCase:
    defect: DrawingDefect
    model: DrawingModel


def _line_record(
    ref: str,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    visible: bool = True,
    space: str = "model",
) -> EntityRecord:
    return EntityRecord(
        entity_ref=ref,
        entity_type="AcDbLine",
        layer="OBJECT",
        visible=visible,
        space=space,
        geometry=LineGeometry(start_mm=start, end_mm=end),
        bounding_box_mm=(
            min(start[0], end[0]),
            min(start[1], end[1]),
            max(start[0], end[0]),
            max(start[1], end[1]),
        ),
    )


def _circle_record(ref: str, center: tuple[float, float], radius: float) -> EntityRecord:
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


def _polyline_record(
    ref: str, points: tuple[tuple[float, float], ...], *, closed: bool
) -> EntityRecord:
    xs = tuple(point[0] for point in points)
    ys = tuple(point[1] for point in points)
    return EntityRecord(
        entity_ref=ref,
        entity_type="AcDbPolyline",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=PolylineGeometry(
            vertices=tuple(PolylineVertex(point_mm=point) for point in points),
            closed=closed,
        ),
        bounding_box_mm=(min(xs), min(ys), max(xs), max(ys)),
    )


def _drawing_model(
    entities: tuple[EntityRecord, ...],
    *,
    unit: str = "mm",
    factor: float | None = 1.0,
    layers: tuple[LayerInfo, ...] = (LayerInfo(name="OBJECT"),),
    unsupported: tuple[UnsupportedEntityCount, ...] = (),
    scope: ReadScope | None = None,
) -> DrawingModel:
    return DrawingModel(
        document_id="doc:property",
        revision="sha256:property",
        display_name="property.dxf",
        source_unit_code=unit,
        to_mm_factor=factor,
        geometry_normalized=factor is not None,
        scope=scope or ReadScope(),
        entities=entities,
        layers=layers,
        unsupported=unsupported,
        coverage_complete=not unsupported,
        arc_chord_tolerance_mm=0.01,
    )


def _nested_block(non_uniform: bool = False) -> EntityRecord:
    if non_uniform:
        child_geometry = EllipseGeometry(
            center_mm=(2.0, 2.0),
            major_axis_mm=2.0,
            minor_axis_mm=1.0,
            rotation_deg=0.0,
        )
        circle = EntityRecord(
            entity_ref="child-circle",
            entity_type="AcDbEllipse",
            layer="OBJECT",
            visible=True,
            space="model",
            geometry=child_geometry,
            bounding_box_mm=(0.0, 1.0, 4.0, 3.0),
            non_uniform_scale=True,
        )
    else:
        circle = _circle_record("child-circle", (2.0, 2.0), 1.0)
    inner_geometry = BlockReferenceGeometry(
        block_name="INNER",
        insertion_mm=(1.0, 1.0),
        scale=(1.0, 1.0),
        rotation_deg=0.0,
        nested_depth_read=2,
        child_entities=(circle,),
    )
    inner = EntityRecord(
        entity_ref="inner-insert",
        entity_type="AcDbBlockReference",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=inner_geometry,
        bounding_box_mm=(1.0, 1.0, 3.0, 3.0),
    )
    outer_geometry = BlockReferenceGeometry(
        block_name="OUTER",
        insertion_mm=(0.0, 0.0),
        scale=(2.0, 1.0) if non_uniform else (1.0, 1.0),
        rotation_deg=0.0,
        non_uniform_scale=non_uniform,
        nested_depth_read=2,
        child_entities=(inner,),
    )
    return EntityRecord(
        entity_ref="outer-insert",
        entity_type="AcDbBlockReference",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=outer_geometry,
        bounding_box_mm=(0.0, 0.0, 3.0, 3.0),
    )


@st.composite
def drawing_models(draw: st.DrawFn) -> DrawingModel:
    """Semantic models spanning the read-path edge cases in Requirements 13.1/13.9."""
    scenario = draw(st.sampled_from(tuple(DrawingScenario)))
    value = draw(st.floats(min_value=-100.0, max_value=100.0, allow_nan=False))
    line = _line_record("line-1", (value, 0.0), (value + 10.0, 5.0))
    if scenario is DrawingScenario.EMPTY:
        return _drawing_model(())
    if scenario is DrawingScenario.FROZEN_LAYER:
        return _drawing_model(
            (line.model_copy(update={"visible": False}),),
            layers=(LayerInfo(name="OBJECT", frozen=True),),
        )
    if scenario is DrawingScenario.OFF_LAYER:
        return _drawing_model(
            (line.model_copy(update={"visible": False}),),
            layers=(LayerInfo(name="OBJECT", off=True),),
        )
    if scenario is DrawingScenario.PAPER_SPACE:
        paper = line.model_copy(update={"space": "paper:Sheet1"})
        return _drawing_model((paper,), scope=ReadScope(kind="layout", layout_name="Sheet1"))
    if scenario is DrawingScenario.NESTED_BLOCK:
        return _drawing_model((_nested_block(),))
    if scenario is DrawingScenario.NON_UNIFORM_SCALE:
        return _drawing_model((_nested_block(non_uniform=True),))
    if scenario is DrawingScenario.UNKNOWN_UNITS:
        return _drawing_model((line,), unit="unknown", factor=None)
    unsupported = (UnsupportedEntityCount(entity_type="spline", count=1),)
    return _drawing_model((line,), unsupported=unsupported)


def _defect_entities(defect: DrawingDefect) -> tuple[EntityRecord, ...]:
    outline = _polyline_record(
        "outline", ((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)), closed=True
    )
    if defect is DrawingDefect.ZERO_LENGTH:
        return (_line_record("zero", (1.0, 1.0), (1.0, 1.0)),)
    if defect is DrawingDefect.NON_FINITE:
        return (_line_record("nan", (0.0, 0.0), (math.nan, 1.0)),)
    if defect is DrawingDefect.OPEN_CONTOUR:
        return (_polyline_record("open", ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)), closed=False),)
    if defect is DrawingDefect.SELF_INTERSECTION:
        return (
            _polyline_record(
                "bow", ((0.0, 0.0), (10.0, 10.0), (0.0, 10.0), (10.0, 0.0)), closed=True
            ),
        )
    if defect in {DrawingDefect.DUPLICATE, DrawingDefect.OVERLAP}:
        first = _line_record("a", (0.0, 0.0), (10.0, 0.0))
        second_end = (10.0, 0.0) if defect is DrawingDefect.DUPLICATE else (15.0, 0.0)
        return (
            first,
            _line_record("b", (5.0 if defect is DrawingDefect.OVERLAP else 0.0, 0.0), second_end),
        )
    if defect is DrawingDefect.HOLE_OUTSIDE:
        outside = _circle_record("hole", (25.0, 10.0), 2.0).model_copy(
            update={"feature_id": "hole-outside"}
        )
        return (outline, outside)
    if defect is DrawingDefect.HOLE_EDGE_DISTANCE:
        return (outline, _circle_record("hole", (1.5, 10.0), 1.0))
    if defect is DrawingDefect.HOLE_HOLE_DISTANCE:
        return (
            outline,
            _circle_record("h1", (8.0, 10.0), 2.0),
            _circle_record("h2", (13.0, 10.0), 2.0),
        )
    if defect is DrawingDefect.INVALID_RADIUS:
        return (_circle_record("bad-radius", (5.0, 5.0), -1.0),)

    arc = EntityRecord(
        entity_ref="fillet",
        entity_type="AcDbArc",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=ArcGeometry(
            center_mm=(10.0, 10.0),
            radius_mm=2.0,
            start_angle_deg=10.0,
            end_angle_deg=80.0,
        ),
        bounding_box_mm=(8.0, 8.0, 12.0, 12.0),
    )
    return (
        _line_record("leg-a", (0.0, 8.0), (8.0, 10.0)),
        arc.model_copy(
            update={
                "geometry": ArcGeometry(
                    center_mm=(10.0, 10.0),
                    radius_mm=2.0,
                    start_angle_deg=90.0,
                    end_angle_deg=180.0,
                )
            }
        ),
        _line_record("leg-b", (10.0, 12.0), (12.0, 20.0)),
        _line_record("fillet-top", (12.0, 20.0), (0.0, 20.0)),
        _line_record("fillet-close", (0.0, 20.0), (0.0, 8.0)),
    )


@st.composite
def defective_models(draw: st.DrawFn) -> DefectiveDrawingModelCase:
    """Exactly the eleven defect classes enumerated by Requirement 21.2."""
    defect = draw(st.sampled_from(tuple(DrawingDefect)))
    return DefectiveDrawingModelCase(defect=defect, model=_drawing_model(_defect_entities(defect)))


# --------------------------------------------------------------------------- #
# Recognition synthesis
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RecognitionCase:
    model: DrawingModel
    expected_type: RecognizedFeatureType
    expected_parameters: dict[str, float]


def _arc_record(ref: str, arc: object) -> EntityRecord:
    center = arc.center
    radius = arc.radius_mm
    assert radius is not None
    return EntityRecord(
        entity_ref=ref,
        entity_type="AcDbArc",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=ArcGeometry(
            center_mm=center.as_tuple(),
            radius_mm=radius,
            start_angle_deg=arc.start_angle_deg,
            end_angle_deg=arc.end_angle_deg,
        ),
        bounding_box_mm=(
            center.x - radius,
            center.y - radius,
            center.x + radius,
            center.y + radius,
        ),
    )


@st.composite
def recognition_cases(draw: st.DrawFn) -> RecognitionCase:
    """Synthetic geometry for all seven recognition feature types."""

    kind = draw(st.sampled_from(tuple(RecognizedFeatureType)))
    origin_x = draw(st.floats(min_value=-50.0, max_value=50.0, allow_nan=False))
    origin_y = draw(st.floats(min_value=-50.0, max_value=50.0, allow_nan=False))
    width = draw(st.floats(min_value=40.0, max_value=120.0, allow_nan=False))
    height = draw(st.floats(min_value=35.0, max_value=100.0, allow_nan=False))
    outline = _polyline_record(
        "outline",
        (
            (origin_x, origin_y),
            (origin_x + width, origin_y),
            (origin_x + width, origin_y + height),
            (origin_x, origin_y + height),
        ),
        closed=True,
    )
    if kind is RecognizedFeatureType.PART_OUTLINE:
        return RecognitionCase(
            _drawing_model((outline,)),
            kind,
            {"width_mm": width, "height_mm": height, "area_mm2": width * height},
        )

    hole_radius = draw(st.floats(min_value=1.0, max_value=4.0, allow_nan=False))
    if kind is RecognizedFeatureType.CIRCULAR_HOLE:
        center = (origin_x + width / 2.0, origin_y + height / 2.0)
        return RecognitionCase(
            _drawing_model((outline, _circle_record("hole", center, hole_radius))),
            kind,
            {"diameter_mm": 2.0 * hole_radius},
        )

    if kind is RecognizedFeatureType.RECTANGULAR_HOLE_PATTERN:
        count_x = draw(st.integers(min_value=3, max_value=4))
        count_y = draw(st.integers(min_value=2, max_value=3))
        pitch_x = (width - 20.0) / (count_x - 1)
        pitch_y = (height - 20.0) / (count_y - 1)
        start = Point2D(origin_x + 10.0, origin_y + 10.0)
        centers_found = rectangular_grid(start, count_x, count_y, pitch_x, pitch_y)
        holes = tuple(
            _circle_record(f"hole-{index}", center.as_tuple(), hole_radius)
            for index, center in enumerate(centers_found)
        )
        return RecognitionCase(
            _drawing_model((outline, *holes)),
            kind,
            {
                "hole_diameter_mm": 2.0 * hole_radius,
                "count_x": float(count_x),
                "count_y": float(count_y),
                "pitch_x_mm": pitch_x,
                "pitch_y_mm": pitch_y,
            },
        )

    if kind is RecognizedFeatureType.BOLT_CIRCLE_PATTERN:
        count = draw(st.sampled_from((4, 6, 8)))
        pcd = min(width, height) * 0.5
        center = Point2D(origin_x + width / 2.0, origin_y + height / 2.0)
        jitter = draw(
            st.floats(
                min_value=0.0,
                max_value=DEMO_TOLERANCE.coincidence_mm * 0.75,
                allow_nan=False,
                allow_infinity=False,
            )
        )
        exact_centers = bolt_circle(center, pcd, count, 0.0)
        centers_found = tuple(
            point.translated(
                *(lambda vector, sign: (vector.dx * sign * jitter, vector.dy * sign * jitter))(
                    center.vector_to(point).normalized(), 1.0 if index % 2 == 0 else -1.0
                )
            )
            for index, point in enumerate(exact_centers)
        )
        holes = tuple(
            _circle_record(f"hole-{index}", point.as_tuple(), hole_radius)
            for index, point in enumerate(centers_found)
        )
        return RecognitionCase(
            _drawing_model((outline, *holes)),
            kind,
            {
                "hole_diameter_mm": 2.0 * hole_radius,
                "pcd_mm": pcd,
                "count": float(count),
                "center_x_mm": center.x,
                "center_y_mm": center.y,
                "max_center_deviation_mm": jitter,
            },
        )

    if kind is RecognizedFeatureType.SLOT:
        slot_length = draw(st.floats(min_value=20.0, max_value=60.0, allow_nan=False))
        slot_width = draw(
            st.floats(min_value=4.0, max_value=min(15.0, slot_length / 2.0), allow_nan=False)
        )
        center = Point2D(origin_x + width / 2.0, origin_y + height / 2.0)
        points = slot_outline(center, slot_length, slot_width, 0.0)
        right_arc, left_arc = slot_end_arcs(points, slot_width)
        top_left, top_right, bottom_right, bottom_left = points
        entities = (
            _line_record("slot-top", top_left.as_tuple(), top_right.as_tuple()),
            _arc_record("slot-right", right_arc),
            _line_record("slot-bottom", bottom_right.as_tuple(), bottom_left.as_tuple()),
            _arc_record("slot-left", left_arc),
        )
        return RecognitionCase(
            _drawing_model(entities),
            kind,
            {"length_mm": slot_length, "width_mm": slot_width},
        )

    if kind is RecognizedFeatureType.FILLET_CORNER:
        radius = draw(st.floats(min_value=1.0, max_value=8.0, allow_nan=False))
        previous = Point2D(0.0, 0.0)
        vertex = Point2D(30.0, 0.0)
        following = Point2D(30.0, 30.0)
        fillet = fillet_vertex(previous, vertex, following, radius, DEMO_TOLERANCE)
        entities = (
            _line_record("fillet-in", previous.as_tuple(), fillet.tangent_in.as_tuple()),
            _arc_record("fillet-arc", fillet.arc),
            _line_record("fillet-out", fillet.tangent_out.as_tuple(), following.as_tuple()),
            _line_record("fillet-top", following.as_tuple(), (0.0, 30.0)),
            _line_record("fillet-close", (0.0, 30.0), previous.as_tuple()),
        )
        return RecognitionCase(_drawing_model(entities), kind, {"radius_mm": radius})

    distance = draw(st.floats(min_value=1.0, max_value=8.0, allow_nan=False))
    chamfer = _polyline_record(
        "chamfered-outline",
        ((0.0, 0.0), (30.0 - distance, 0.0), (30.0, distance), (30.0, 30.0), (0.0, 30.0)),
        closed=True,
    )
    return RecognitionCase(
        _drawing_model((chamfer,)),
        kind,
        {"distance_1_mm": distance, "distance_2_mm": distance},
    )


# --------------------------------------------------------------------------- #
# Pilot metrics sequences and baseline sets
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AuditEventSequenceCase:
    events: tuple[AuditEvent, ...]
    engineer_activity: tuple[EngineerActivityInterval, ...]
    manual_fixup_minutes: float


@st.composite
def audit_event_sequences(draw: st.DrawFn) -> AuditEventSequenceCase:
    """Ordered case activity with idle boundaries below, at, and above five minutes."""
    gap_minutes = draw(
        st.lists(
            st.sampled_from((0.0, 1.0, 4.999, 5.0, 5.001, 8.0, 15.0)),
            min_size=1,
            max_size=8,
        )
    )
    overlap_index = draw(
        st.one_of(st.none(), st.integers(min_value=0, max_value=len(gap_minutes) - 1))
    )
    manual_fixup = draw(st.integers(min_value=0, max_value=300).map(lambda tenths: tenths / 10.0))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    current = start
    event_types = [AuditEventType.JOB_CREATED.value]
    event_types.extend(AuditEventType.SPEC_CHANGED.value for _ in gap_minutes[:-1])
    event_types.append(AuditEventType.COMMIT_SUCCEEDED.value)
    timestamps = [start]
    for gap in gap_minutes:
        current += timedelta(minutes=gap)
        timestamps.append(current)
    events = tuple(
        AuditEvent(
            event_id=f"evt-{index}",
            event_type=event_type,
            job_id="job-pilot-property",
            actor_type="system" if index % 2 == 0 else "engineer",
            actor_id="property",
            payload={"entities": 1} if event_type == AuditEventType.COMMIT_SUCCEEDED else {},
            created_at=timestamps[index],
            previous_event_hash=None,
            event_hash=f"hash-{index}",
        )
        for index, event_type in enumerate(event_types)
    )
    engineer_activity: tuple[EngineerActivityInterval, ...] = ()
    if overlap_index is not None and gap_minutes[overlap_index] > 0.0:
        engineer_activity = (
            EngineerActivityInterval(
                started_at=timestamps[overlap_index]
                + timedelta(minutes=gap_minutes[overlap_index] * 0.25),
                ended_at=timestamps[overlap_index]
                + timedelta(minutes=gap_minutes[overlap_index] * 0.75),
            ),
        )
    return AuditEventSequenceCase(events, engineer_activity, manual_fixup)


@dataclass(frozen=True, slots=True)
class BaselineSetCase:
    baseline: tuple[BaselineCase, ...]
    efforts: tuple[EffortRecord, ...]


@st.composite
def baseline_sets(draw: st.DrawFn) -> BaselineSetCase:
    """Even/odd, biased, insufficient, incomplete, and group-boundary pilot sets."""
    size = draw(st.sampled_from((3, 4, 5, 9, 10, 11, 12, 15, 16)))
    biased_index = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=size - 1)))
    incomplete_index = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=size - 1)))
    single_session = draw(st.booleans())
    harness_tenths = draw(
        st.lists(st.integers(min_value=0, max_value=2_000), min_size=size, max_size=size)
    )
    baseline = tuple(
        BaselineCase(
            case_id=f"case-{index}",
            capability_group=("B", "D", "E")[index % 3],
            work_label="ve_moi" if index % 2 == 0 else "sua_ban_co_san",
            manual_minutes=100.0,
            manual_measured_by=f"engineer-{index % 5}",
            manual_measurement_biased=index == biased_index,
            manual_measured_in_single_session=single_session,
        )
        for index in range(size)
    )
    efforts = tuple(
        EffortRecord(
            record_id=f"effort-{index}",
            case_id=f"case-{index}",
            job_id=f"job-{index}",
            harness_minutes=harness_tenths[index] / 10.0,
            idle_minutes_excluded=0.0,
            manual_fixup_minutes=0.0,
            spec_change_count=index % 4,
            entities_created=10,
            entities_manually_edited=index % 3,
            first_preview_clean=index % 4 != 0,
            completed=index != incomplete_index,
            failure_reason=(FailureReason.ADAPTER_FAILURE if index == incomplete_index else None),
        )
        for index in range(size)
    )
    return BaselineSetCase(baseline, efforts)
