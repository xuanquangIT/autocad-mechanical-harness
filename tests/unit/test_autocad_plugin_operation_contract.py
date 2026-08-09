from __future__ import annotations

import ast
import re
from pathlib import Path

from cad_harness.domain.models.operation_plan import Operation, OperationType

ROOT = Path(__file__).resolve().parents[2]
DISPATCHER = (
    ROOT / "dotnet" / "AutoCADBridge" / "CadBridge.Plugin" / "AutoCadOperationDispatcher.cs"
)
PRODUCER_ROOTS = (
    ROOT / "src" / "cad_harness" / "feature_catalog",
    ROOT / "src" / "cad_harness" / "annotation",
    ROOT / "src" / "cad_harness" / "application" / "services" / "remediation_service.py",
)
PRODUCED_TYPES = {
    "create_arc",
    "create_centerline",
    "create_centermark",
    "create_circle",
    "create_circles",
    "create_closed_polyline",
    "create_diameter_dimension",
    "create_line",
    "create_linear_dimension",
    "create_text",
    "delete_entity",
    "update_entity",
}


def _producer_files() -> list[Path]:
    files: list[Path] = []
    for root in PRODUCER_ROOTS:
        files.extend(root.rglob("*.py") if root.is_dir() else [root])
    return files


def _literal_operation_types() -> set[str]:
    operation_types: set[str] = set()
    for path in _producer_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "Operation":
                continue
            type_node = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "type"), None
            )
            if (
                isinstance(type_node, ast.Attribute)
                and isinstance(type_node.value, ast.Name)
                and type_node.value.id == "OperationType"
            ):
                operation_types.add(OperationType[type_node.attr].value)
    return operation_types


def _literal_geometry_shapes() -> list[tuple[str, frozenset[str]]]:
    shapes: list[tuple[str, frozenset[str]]] = []
    for path in _producer_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "Operation":
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            type_node = keywords.get("type")
            if not (
                isinstance(type_node, ast.Attribute)
                and isinstance(type_node.value, ast.Name)
                and type_node.value.id == "OperationType"
            ):
                continue
            operation_type = OperationType[type_node.attr].value
            geometry = keywords.get("geometry")
            if geometry is None:
                shapes.append((operation_type, frozenset()))
            elif isinstance(geometry, ast.Dict):
                literal_keys = frozenset(
                    key.value
                    for key in geometry.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
                shapes.append((operation_type, literal_keys))
    return shapes


def _dispatcher_schemas() -> dict[str, list[tuple[frozenset[str], frozenset[str]]]]:
    source = DISPATCHER.read_text(encoding="utf-8")
    pattern = re.compile(
        r'new GeometrySchema\(\s*"([^"]+)",\s*Set\((.*?)\),\s*Set\((.*?)\)\)',
        re.DOTALL,
    )
    schemas: dict[str, list[tuple[frozenset[str], frozenset[str]]]] = {}
    for operation_type, required_source, optional_source in pattern.findall(source):
        required = frozenset(re.findall(r'"([^"]+)"', required_source))
        optional = frozenset(re.findall(r'"([^"]+)"', optional_source))
        schemas.setdefault(operation_type, []).append((required, optional))
    return schemas


def _accepted(
    operation_type: str,
    geometry: dict[str, object] | frozenset[str],
    schemas: dict[str, list[tuple[frozenset[str], frozenset[str]]]],
) -> bool:
    keys = frozenset(geometry if isinstance(geometry, frozenset) else geometry.keys())
    return any(
        required <= keys <= required | optional
        for required, optional in schemas.get(operation_type, [])
    )


def _canonical_geometries() -> list[tuple[str, dict[str, object]]]:
    annotation = {
        "textstyle": "Standard",
        "text_height_mm": 2.5,
        "text_bbox_mm": [0.0, 0.0, 10.0, 2.5],
    }
    dimension = {
        "text_position_mm": [5.0, 5.0],
        "measurement_mm": 10.0,
        "text_value": "10",
        "dimstyle": "ISO-25",
        **annotation,
    }
    text_base = {"position_mm": [0.0, 0.0], "text": "NOTE", **annotation}
    return [
        ("create_line", {"start_mm": [0.0, 0.0], "end_mm": [10.0, 0.0]}),
        ("create_centerline", {"start_mm": [0.0, 0.0], "end_mm": [10.0, 0.0]}),
        ("create_closed_polyline", {"vertices_mm": [[0, 0], [10, 0], [0, 10]]}),
        ("create_circle", {"center_mm": [0.0, 0.0], "diameter_mm": 10.0}),
        ("create_circle", {"center_mm": [0.0, 0.0], "radius_mm": 5.0}),
        ("create_circles", {"centers_mm": [[0, 0], [10, 0]], "diameter_mm": 5.0}),
        (
            "create_arc",
            {
                "center_mm": [0.0, 0.0],
                "radius_mm": 5.0,
                "start_angle_deg": 0.0,
                "end_angle_deg": 90.0,
            },
        ),
        ("create_centermark", {"center_mm": [0.0, 0.0]}),
        (
            "create_linear_dimension",
            {
                "start_mm": [0.0, 0.0],
                "end_mm": [10.0, 0.0],
                "annotation_kind": "linear_dimension",
                **dimension,
            },
        ),
        (
            "create_diameter_dimension",
            {
                "center_mm": [0.0, 0.0],
                "annotation_kind": "hole_diameter",
                **dimension,
            },
        ),
        (
            "create_text",
            {
                **text_base,
                "annotation_kind": "hole_callout",
                "diameter_mm": 10.0,
                "count": 2,
            },
        ),
        (
            "create_text",
            {
                **text_base,
                "annotation_kind": "hole_table_row",
                "symbol": "A",
                "count": 2,
                "diameter_mm": 10.0,
            },
        ),
        (
            "create_text",
            {
                **text_base,
                "annotation_kind": "title_block_field",
                "field_name": "DRAWN_BY",
                "source": "drawing_spec",
                "source_version": "1.0",
            },
        ),
        (
            "create_text",
            {
                **text_base,
                "annotation_kind": "gdt_datum_symbol",
                "datum_identifier": "A",
            },
        ),
        (
            "create_text",
            {
                **text_base,
                "annotation_kind": "gdt_feature_control_frame",
                "frame_id": "fcf-1",
                "datum_references": ["A"],
                "certifies_tolerance_chain": False,
            },
        ),
        ("update_entity", {"properties": {}}),
        ("update_entity", {"properties": {"StyleName": "Standard"}}),
        ("update_entity", {"properties": {"TextOverride": ""}}),
        ("update_entity", {"properties": {"StartPoint": [1.0, 2.0, 0.0]}}),
        ("update_entity", {"properties": {"EndPoint": [1.0, 2.0, 0.0]}}),
        ("delete_entity", {}),
    ]


def test_dispatcher_advertises_exactly_mined_python_producers() -> None:
    schemas = _dispatcher_schemas()
    assert _literal_operation_types() == PRODUCED_TYPES
    assert set(schemas) == PRODUCED_TYPES


def test_real_emitter_shapes_and_generated_plans_match_closed_dispatch_schemas() -> None:
    schemas = _dispatcher_schemas()
    for operation_type, keys in _literal_geometry_shapes():
        assert _accepted(operation_type, keys, schemas), (operation_type, keys)

    for index, (operation_type, geometry) in enumerate(_canonical_geometries()):
        target = "acad:handle:1A" if operation_type in {"update_entity", "delete_entity"} else None
        operation = Operation(
            operation_id=f"op:contract:{index}",
            feature_id=f"feature:contract:{index}",
            type=OperationType(operation_type),
            layer="0",
            geometry=geometry,
            expected={},
            target_entity_ref=target,
        )
        dumped = operation.model_dump(mode="json")
        assert _accepted(operation_type, dumped["geometry"], schemas)
        assert not _accepted(operation_type, {**geometry, "unexpected": True}, schemas)


def test_parse_path_enforces_schema_and_dispatch_uses_canonical_annotation_fields() -> None:
    source = DISPATCHER.read_text(encoding="utf-8")
    assert "ValidateGeometry(type, geometry, targetEntityRef);" in source
    assert 'Point(geometry, "position_mm")' in source
    assert 'PositiveNumber(geometry, "text_height_mm")' in source
    assert 'Point(geometry, "text_position_mm")' in source
    assert 'PositiveNumber(geometry, "measurement_mm")' in source
    for legacy_field in (
        "insertion_point_mm",
        "height_mm",
        "extension_line_1_mm",
        "extension_line_2_mm",
        "dimension_line_point_mm",
        "chord_point_mm",
        "far_chord_point_mm",
        "leader_length_mm",
        "boundary_refs",
    ):
        assert f'"{legacy_field}"' not in source
