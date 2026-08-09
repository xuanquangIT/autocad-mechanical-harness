"""Read-only DXF implementation of :class:`DrawingSourcePort`."""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import ezdxf
from ezdxf import bbox as dxf_bbox

from cad_harness.application.process_runner import (
    JsonValue,
    ProcessWorkerCommand,
    run_process_worker,
)
from cad_harness.domain.errors import DocumentNotFoundError, UnsupportedInputFormatError
from cad_harness.domain.models.document import LayerInfo
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    BlockReferenceGeometry,
    CircleGeometry,
    DimensionGeometry,
    DrawingModel,
    DrawingSummary,
    EllipseGeometry,
    EntityGeometry,
    EntityRecord,
    HatchGeometry,
    LineGeometry,
    PolylineGeometry,
    PolylineVertex,
    TextGeometry,
    UnsupportedEntityCount,
)
from cad_harness.domain.ports.drawing_source import DrawingReadRequest
from cad_harness.domain.ports.repositories import CancellationTokenPort
from cad_harness.geometry.tolerance import DEMO_TOLERANCE, ToleranceProfile

_SUPPORTED_TYPES = frozenset(
    {
        "LINE",
        "POLYLINE",
        "LWPOLYLINE",
        "CIRCLE",
        "ARC",
        "ELLIPSE",
        "TEXT",
        "MTEXT",
        "DIMENSION",
        "HATCH",
        "INSERT",
    }
)
_ENTITY_NAMES = {
    "LINE": "AcDbLine",
    "POLYLINE": "AcDb2dPolyline",
    "LWPOLYLINE": "AcDbPolyline",
    "CIRCLE": "AcDbCircle",
    "ARC": "AcDbArc",
    "ELLIPSE": "AcDbEllipse",
    "TEXT": "AcDbText",
    "MTEXT": "AcDbMText",
    "DIMENSION": "AcDbDimension",
    "HATCH": "AcDbHatch",
    "INSERT": "AcDbBlockReference",
}
_UNSUPPORTED_NAMES = {
    "SPLINE": "spline",
    "3DSOLID": "3d_solid",
    "REGION": "region",
    "SURFACE": "surface",
    "PLANESURFACE": "surface",
    "EXTRUDEDSURFACE": "surface",
    "LOFTEDSURFACE": "surface",
    "REVOLVEDSURFACE": "surface",
    "SWEPTSURFACE": "surface",
    "OLE2FRAME": "ole",
    "ACAD_PROXY_ENTITY": "proxy",
}
_UNIT_FACTORS: dict[int, tuple[str, float]] = {
    1: ("inch", 25.4),
    2: ("foot", 304.8),
    4: ("mm", 1.0),
    5: ("cm", 10.0),
    6: ("m", 1000.0),
    7: ("km", 1_000_000.0),
    8: ("microinch", 0.0000254),
    9: ("mil", 0.0254),
    10: ("yard", 914.4),
    11: ("angstrom", 0.0000001),
    12: ("nm", 0.000001),
    13: ("micron", 0.001),
    14: ("dm", 100.0),
}


class DxfDrawingReader:
    """Extract semantic records without writing to the source document or file."""

    def __init__(self, tolerance: ToleranceProfile = DEMO_TOLERANCE) -> None:
        self._tolerance = tolerance

    def current_revision(self, document_id: str) -> str:
        return self._current_revision(document_id, None)

    def current_revision_cancellable(
        self, document_id: str, deadline: CancellationTokenPort
    ) -> str:
        path = self._require_file(document_id)
        result = run_process_worker(
            deadline,
            ProcessWorkerCommand.DXF_CURRENT_REVISION,
            {"document_id": document_id},
            allowed_input_root=path.parent,
        )
        revision = result.get("revision")
        if not isinstance(revision, str):
            raise TypeError("DXF revision worker returned an invalid result")
        return revision

    def _current_revision(self, document_id: str, deadline: CancellationTokenPort | None) -> str:
        path = self._require_file(document_id)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if deadline is not None:
                    deadline.checkpoint()
                digest.update(chunk)
        if deadline is not None:
            deadline.checkpoint()
        return f"sha256:{digest.hexdigest()}"

    def summarize(self, request: DrawingReadRequest) -> DrawingSummary:
        return self._summarize(request, None)

    def summarize_cancellable(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort
    ) -> DrawingSummary:
        path = self._require_dxf_path(request)
        result = run_process_worker(
            deadline,
            ProcessWorkerCommand.DXF_SUMMARY,
            {
                "request": request.model_dump(mode="json"),
                "tolerance": self._tolerance_json(),
            },
            allowed_input_root=path.parent,
        )
        return DrawingSummary.model_validate(result.get("summary"))

    def _summarize(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort | None
    ) -> DrawingSummary:
        if deadline is not None:
            deadline.checkpoint()
        document, path = self._open(request)
        revision = self._current_revision(request.source.ref, deadline)
        by_type: Counter[str] = Counter()
        by_layer: Counter[str] = Counter()
        by_space: Counter[str] = Counter()
        unsupported: Counter[str] = Counter()
        for entity, space in self._entities_in_scope(document, request):
            if deadline is not None:
                deadline.checkpoint()
            raw_type = entity.dxftype()
            unsupported_name = self._unsupported_name(document, entity)
            entity_type = unsupported_name or _ENTITY_NAMES.get(raw_type, raw_type.lower())
            by_type[entity_type] += 1
            by_layer[str(entity.dxf.get("layer", "0"))] += 1
            by_space[space] += 1
            if unsupported_name is not None or raw_type not in _SUPPORTED_TYPES:
                unsupported[unsupported_name or raw_type.lower()] += 1
        return DrawingSummary(
            document_id=self._document_id(path),
            revision=revision,
            counts_by_entity_type=dict(by_type),
            counts_by_layer=dict(by_layer),
            counts_by_space=dict(by_space),
            unsupported=self._unsupported_records(unsupported),
            coverage_complete=not unsupported,
        )

    def read(self, request: DrawingReadRequest) -> DrawingModel:
        return self._read(request, None)

    def read_cancellable(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort
    ) -> DrawingModel:
        path = self._require_dxf_path(request)
        result = run_process_worker(
            deadline,
            ProcessWorkerCommand.DXF_MODEL,
            {
                "request": request.model_dump(mode="json"),
                "tolerance": self._tolerance_json(),
            },
            allowed_input_root=path.parent,
        )
        return DrawingModel.model_validate(result.get("model"))

    def _read(
        self, request: DrawingReadRequest, deadline: CancellationTokenPort | None
    ) -> DrawingModel:
        if request.scope is None:
            raise ValueError("Detailed reads require an explicit scope; use summarize otherwise")
        document, path = self._open(request)
        revision = self._current_revision(request.source.ref, deadline)
        unit_code, factor = self._units(document)
        scale = factor if factor is not None else 1.0
        unsupported: Counter[str] = Counter()
        records: list[EntityRecord] = []
        for index, (entity, space) in enumerate(self._entities_in_scope(document, request)):
            if deadline is not None:
                deadline.checkpoint()
            unsupported_name = self._unsupported_name(document, entity)
            if unsupported_name is not None or entity.dxftype() not in _SUPPORTED_TYPES:
                unsupported[unsupported_name or entity.dxftype().lower()] += 1
                continue
            record = self._entity_record(
                document,
                entity,
                space=space,
                scale=scale,
                max_depth=request.max_block_nesting_depth,
                depth=1,
                ref_prefix=f"root:{index}",
                unsupported=unsupported,
            )
            if record is not None:
                records.append(record)
        unsupported_records = self._unsupported_records(unsupported)
        return DrawingModel(
            document_id=self._document_id(path),
            revision=revision,
            display_name=path.name,
            source_unit_code=unit_code,
            to_mm_factor=factor,
            geometry_normalized=factor is not None,
            scope=request.scope,
            entities=tuple(records),
            layers=self._layers(document),
            dimension_styles=tuple(style.dxf.name for style in document.dimstyles),
            text_styles=tuple(style.dxf.name for style in document.styles),
            unsupported=unsupported_records,
            coverage_complete=not unsupported_records,
            arc_chord_tolerance_mm=self._tolerance.arc_chord_tolerance_mm,
        )

    def _open(self, request: DrawingReadRequest) -> tuple[Any, Path]:
        path = self._require_dxf_path(request)
        return ezdxf.readfile(path), path

    @staticmethod
    def _require_dxf_path(request: DrawingReadRequest) -> Path:
        source_format = request.source.format.strip().lower().lstrip(".")
        if request.source.kind != "file" or source_format != "dxf":
            raise UnsupportedInputFormatError(
                "DxfDrawingReader reads local DXF files only",
                details={"supported_formats": ["dxf"], "source_kind": request.source.kind},
            )
        return DxfDrawingReader._require_file(request.source.ref)

    @staticmethod
    def _require_file(document_id: str) -> Path:
        path = Path(document_id)
        if not path.is_file():
            raise DocumentNotFoundError(
                "Drawing file was not found",
                required_action="Select an existing DXF file",
                details={"display_name": path.name},
            )
        return path

    def _tolerance_json(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], asdict(self._tolerance))

    def _entities_in_scope(
        self, document: Any, request: DrawingReadRequest
    ) -> Iterator[tuple[Any, str]]:
        scope = request.scope
        if scope is None:
            layouts: Iterable[Any] = document.layouts
        elif scope.kind in {"model_space", "layer"}:
            layouts = (document.modelspace(),)
        elif scope.kind == "layout":
            layouts = (document.layouts.get(scope.layout_name),)
        else:
            layouts = document.layouts

        selected = (
            set(scope.entity_refs) if scope is not None and scope.kind == "selection" else None
        )
        for layout in layouts:
            space = "model" if layout.name.lower() == "model" else f"paper:{layout.name}"
            for entity in layout:
                if (
                    scope is not None
                    and scope.kind == "layer"
                    and str(entity.dxf.get("layer", "0")) != scope.layer_name
                ):
                    continue
                handle = str(entity.dxf.get("handle", ""))
                if selected is not None and handle not in selected:
                    continue
                yield entity, space

    @staticmethod
    def _document_id(path: Path) -> str:
        normalized = str(path.resolve()).casefold().encode("utf-8")
        digest = hashlib.sha256(normalized).hexdigest()[:26].upper()
        return f"doc_{digest}"

    @staticmethod
    def _units(document: Any) -> tuple[str, float | None]:
        code = int(document.header.get("$INSUNITS", 0) or 0)
        return _UNIT_FACTORS.get(code, ("unknown", None))

    @staticmethod
    def _unsupported_records(counts: Counter[str]) -> tuple[UnsupportedEntityCount, ...]:
        return tuple(
            UnsupportedEntityCount(entity_type=entity_type, count=count)
            for entity_type, count in sorted(counts.items())
        )

    @staticmethod
    def _unsupported_name(document: Any, entity: Any) -> str | None:
        raw_type = entity.dxftype()
        if raw_type == "INSERT":
            try:
                block = document.blocks.get(str(entity.dxf.name))
            except KeyError:
                return "xref"
            try:
                flags = int(block.block.dxf.get("flags", 0))
            except (AttributeError, TypeError, ValueError):
                flags = 0
            return "xref" if flags & 4 else None
        return _UNSUPPORTED_NAMES.get(raw_type)

    @staticmethod
    def _layers(document: Any) -> tuple[LayerInfo, ...]:
        return tuple(
            LayerInfo(
                name=str(layer.dxf.name),
                color_index=abs(int(layer.dxf.get("color", 7))),
                linetype=str(layer.dxf.get("linetype", "Continuous")),
                lineweight=int(layer.dxf.get("lineweight", -3)),
                frozen=bool(layer.is_frozen()),
                off=bool(layer.is_off()),
                locked=bool(layer.is_locked()),
            )
            for layer in document.layers
        )

    def _entity_record(
        self,
        document: Any,
        entity: Any,
        *,
        space: str,
        scale: float,
        max_depth: int,
        depth: int,
        ref_prefix: str,
        unsupported: Counter[str],
    ) -> EntityRecord | None:
        raw_type = entity.dxftype()
        unsupported_name = self._unsupported_name(document, entity)
        if unsupported_name is not None or raw_type not in _SUPPORTED_TYPES:
            unsupported[unsupported_name or raw_type.lower()] += 1
            return None
        entity_ref = str(entity.dxf.get("handle", "")) or ref_prefix
        geometry = self._geometry(
            document,
            entity,
            space=space,
            scale=scale,
            max_depth=max_depth,
            depth=depth,
            entity_ref=entity_ref,
            unsupported=unsupported,
        )
        layer_name = str(entity.dxf.get("layer", "0"))
        return EntityRecord(
            entity_ref=entity_ref,
            entity_type=_ENTITY_NAMES[raw_type],
            layer=layer_name,
            visible=self._is_visible(document, layer_name),
            space=space,
            geometry=geometry,
            bounding_box_mm=self._bounding_box(entity, geometry, scale),
        )

    def _geometry(
        self,
        document: Any,
        entity: Any,
        *,
        space: str,
        scale: float,
        max_depth: int,
        depth: int,
        entity_ref: str,
        unsupported: Counter[str],
    ) -> EntityGeometry:
        raw_type = entity.dxftype()
        if raw_type == "LINE":
            return LineGeometry(
                start_mm=self._point(entity.dxf.start, scale),
                end_mm=self._point(entity.dxf.end, scale),
            )
        if raw_type == "CIRCLE":
            return CircleGeometry(
                center_mm=self._point(entity.dxf.center, scale),
                radius_mm=float(entity.dxf.radius) * scale,
            )
        if raw_type == "ARC":
            return ArcGeometry(
                center_mm=self._point(entity.dxf.center, scale),
                radius_mm=float(entity.dxf.radius) * scale,
                start_angle_deg=float(entity.dxf.start_angle),
                end_angle_deg=float(entity.dxf.end_angle),
            )
        if raw_type == "ELLIPSE":
            major = entity.dxf.major_axis
            major_length = math.hypot(float(major.x), float(major.y)) * scale
            return EllipseGeometry(
                center_mm=self._point(entity.dxf.center, scale),
                major_axis_mm=major_length,
                minor_axis_mm=major_length * float(entity.dxf.ratio),
                rotation_deg=math.degrees(math.atan2(float(major.y), float(major.x))),
            )
        if raw_type in {"POLYLINE", "LWPOLYLINE"}:
            return self._polyline_geometry(entity, scale)
        if raw_type in {"TEXT", "MTEXT"}:
            return self._text_geometry(entity, scale)
        if raw_type == "DIMENSION":
            return self._dimension_geometry(entity, scale)
        if raw_type == "HATCH":
            return HatchGeometry(
                pattern_name=str(entity.dxf.get("pattern_name", "SOLID")),
                area_mm2=self._hatch_area(entity, scale),
            )
        return self._block_geometry(
            document,
            entity,
            space=space,
            scale=scale,
            max_depth=max_depth,
            depth=depth,
            entity_ref=entity_ref,
            unsupported=unsupported,
        )

    @staticmethod
    def _point(vector: Any, scale: float) -> tuple[float, float]:
        return (float(vector.x) * scale, float(vector.y) * scale)

    @staticmethod
    def _polyline_geometry(entity: Any, scale: float) -> PolylineGeometry:
        if entity.dxftype() == "LWPOLYLINE":
            vertices = tuple(
                PolylineVertex(
                    point_mm=(float(x) * scale, float(y) * scale),
                    bulge=float(bulge),
                )
                for x, y, bulge in entity.get_points("xyb")
            )
            closed = bool(entity.closed)
        else:
            vertices = tuple(
                PolylineVertex(
                    point_mm=(
                        float(vertex.dxf.location.x) * scale,
                        float(vertex.dxf.location.y) * scale,
                    ),
                    bulge=float(vertex.dxf.get("bulge", 0.0)),
                )
                for vertex in entity.vertices
            )
            closed = bool(entity.is_closed)
        return PolylineGeometry(vertices=vertices, closed=closed)

    def _text_geometry(self, entity: Any, scale: float) -> TextGeometry:
        if entity.dxftype() == "MTEXT":
            insertion = entity.dxf.insert
            height = float(entity.dxf.get("char_height", 0.0))
            content = str(entity.plain_text())
        else:
            insertion = entity.dxf.insert
            height = float(entity.dxf.get("height", 0.0))
            content = str(entity.dxf.get("text", ""))
        return TextGeometry(
            insertion_mm=self._point(insertion, scale),
            height_mm=height * scale,
            text_style=str(entity.dxf.get("style", "Standard")),
            content=content,
        )

    @staticmethod
    def _dimension_geometry(entity: Any, scale: float) -> DimensionGeometry:
        measurement: float | None
        try:
            measurement = float(entity.get_measurement()) * scale
        except (AttributeError, TypeError, ValueError, ezdxf.DXFError):
            measurement = None
        override = str(entity.dxf.get("text", ""))
        return DimensionGeometry(
            dimension_type=str(int(entity.dxf.get("dimtype", 0)) & 15),
            dimension_style=str(entity.dxf.get("dimstyle", "Standard")),
            measurement_mm=measurement,
            text_override=None if override in {"", "<>"} else override,
        )

    @staticmethod
    def _hatch_area(entity: Any, scale: float) -> float | None:
        try:
            area = float(entity.area)
        except (AttributeError, TypeError, ValueError):
            return None
        return area * scale * scale if math.isfinite(area) else None

    @staticmethod
    def _is_visible(document: Any, layer_name: str) -> bool:
        try:
            layer = document.layers.get(layer_name)
        except (KeyError, ezdxf.DXFTableEntryError):
            return True
        return not bool(layer.is_off() or layer.is_frozen())

    def _block_geometry(
        self,
        document: Any,
        entity: Any,
        *,
        space: str,
        scale: float,
        max_depth: int,
        depth: int,
        entity_ref: str,
        unsupported: Counter[str],
    ) -> BlockReferenceGeometry:
        x_scale = float(entity.dxf.get("xscale", 1.0))
        y_scale = float(entity.dxf.get("yscale", 1.0))
        non_uniform = not self._tolerance.length_close(abs(x_scale), abs(y_scale))
        if depth > max_depth:
            beyond = self._count_block_descendants(document, str(entity.dxf.name), set())
            children: tuple[EntityRecord, ...] = ()
            depth_read = max_depth
        else:
            child_records: list[EntityRecord] = []
            for index, child in enumerate(entity.virtual_entities()):
                child_ref = f"{entity_ref}/{child.dxftype()}:{index}"
                record = self._entity_record(
                    document,
                    child,
                    space=space,
                    scale=scale,
                    max_depth=max_depth,
                    depth=depth + 1,
                    ref_prefix=child_ref,
                    unsupported=unsupported,
                )
                if record is not None:
                    if non_uniform:
                        record = record.model_copy(update={"non_uniform_scale": True})
                    child_records.append(record)
            children = tuple(child_records)
            child_blocks = (
                child.geometry
                for child in children
                if isinstance(child.geometry, BlockReferenceGeometry)
            )
            child_block_list = tuple(child_blocks)
            beyond = sum(child.children_beyond_depth for child in child_block_list)
            depth_read = max(
                (child.nested_depth_read for child in child_block_list),
                default=depth,
            )
        return BlockReferenceGeometry(
            block_name=str(entity.dxf.name),
            insertion_mm=self._point(entity.dxf.insert, scale),
            scale=(x_scale, y_scale),
            rotation_deg=float(entity.dxf.get("rotation", 0.0)),
            non_uniform_scale=non_uniform,
            nested_depth_read=depth_read,
            child_entities=children,
            children_beyond_depth=beyond,
        )

    def _count_block_descendants(self, document: Any, block_name: str, visiting: set[str]) -> int:
        if block_name in visiting:
            return 0
        try:
            block = document.blocks.get(block_name)
        except KeyError:
            return 0
        nested_visiting = {*visiting, block_name}
        count = 0
        for child in block:
            count += 1
            if child.dxftype() == "INSERT":
                count += self._count_block_descendants(
                    document, str(child.dxf.name), nested_visiting
                )
        return count

    def _bounding_box(
        self, entity: Any, geometry: EntityGeometry, scale: float
    ) -> tuple[float, float, float, float]:
        try:
            bounds = dxf_bbox.extents((entity,), fast=True)
            if bounds.has_data:
                return (
                    float(bounds.extmin.x) * scale,
                    float(bounds.extmin.y) * scale,
                    float(bounds.extmax.x) * scale,
                    float(bounds.extmax.y) * scale,
                )
        except (AttributeError, TypeError, ValueError, ezdxf.DXFError):
            pass
        if isinstance(geometry, LineGeometry):
            return self._points_box((geometry.start_mm, geometry.end_mm))
        if isinstance(geometry, CircleGeometry):
            x, y = geometry.center_mm
            radius = geometry.radius_mm
            return (x - radius, y - radius, x + radius, y + radius)
        if isinstance(geometry, ArcGeometry):
            x, y = geometry.center_mm
            radius = geometry.radius_mm
            return (x - radius, y - radius, x + radius, y + radius)
        if isinstance(geometry, EllipseGeometry):
            x, y = geometry.center_mm
            radius = geometry.major_axis_mm
            return (x - radius, y - radius, x + radius, y + radius)
        if isinstance(geometry, PolylineGeometry):
            return self._points_box(tuple(vertex.point_mm for vertex in geometry.vertices))
        if isinstance(geometry, TextGeometry):
            x, y = geometry.insertion_mm
            return (x, y, x, y)
        if isinstance(geometry, BlockReferenceGeometry) and geometry.child_entities:
            boxes = tuple(child.bounding_box_mm for child in geometry.child_entities)
            return (
                min(box[0] for box in boxes),
                min(box[1] for box in boxes),
                max(box[2] for box in boxes),
                max(box[3] for box in boxes),
            )
        if isinstance(geometry, BlockReferenceGeometry):
            x, y = geometry.insertion_mm
            return (x, y, x, y)
        return (0.0, 0.0, 0.0, 0.0)

    @staticmethod
    def _points_box(points: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float]:
        if not points:
            return (0.0, 0.0, 0.0, 0.0)
        xs = tuple(point[0] for point in points)
        ys = tuple(point[1] for point in points)
        return (min(xs), min(ys), max(xs), max(ys))
