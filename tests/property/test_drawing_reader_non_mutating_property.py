"""Property 33: bounded DXF reads are complete and non-mutating."""

from pathlib import Path

import ezdxf
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
from cad_harness.domain.models.drawing_model import ReadScope
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef


# Feature: cad-ai-production-roadmap, Property 33: reader is non-mutating and complete within scope
@given(
    target_count=st.integers(min_value=0, max_value=12),
    other_count=st.integers(min_value=0, max_value=12),
    scope_kind=st.sampled_from(("model_space", "selection", "layer", "layout")),
    target_layer_state=st.sampled_from(("visible", "frozen", "off")),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
def test_reader_preserves_file_and_returns_exact_scope(
    tmp_path: Path,
    target_count: int,
    other_count: int,
    scope_kind: str,
    target_layer_state: str,
) -> None:
    """**Validates: Requirements 13.1, 13.2, 13.3, 13.4**"""
    assume(scope_kind != "selection" or target_count > 0)
    path = tmp_path / "scope.dxf"
    document = ezdxf.new("R2018", setup=True)
    document.header["$INSUNITS"] = 4
    document.layers.add("TARGET")
    document.layers.add("OTHER")
    target_layer = document.layers.get("TARGET")
    if target_layer_state == "frozen":
        target_layer.freeze()
    elif target_layer_state == "off":
        target_layer.off()
    model = document.modelspace()
    paper = document.layouts.new("Sheet1")
    target_refs: list[str] = []
    for index in range(target_count):
        destination = paper if scope_kind == "layout" else model
        entity = destination.add_line((index, 0), (index + 1, 1), dxfattribs={"layer": "TARGET"})
        target_refs.append(str(entity.dxf.handle))
    for index in range(other_count):
        model.add_line((index, 2), (index + 1, 3), dxfattribs={"layer": "OTHER"})
    document.saveas(path)
    before = path.read_bytes()

    if scope_kind == "layer":
        scope = ReadScope(kind="layer", layer_name="TARGET")
    elif scope_kind == "layout":
        scope = ReadScope(kind="layout", layout_name="Sheet1")
    elif scope_kind == "selection":
        scope = ReadScope(kind="selection", entity_refs=tuple(target_refs))
    else:
        scope = ReadScope()
    request = DrawingReadRequest(
        source=DrawingSourceRef(kind="file", format="dxf", ref=str(path)),
        scope=scope,
        max_entities=100,
        max_block_nesting_depth=3,
    )
    reader = DxfDrawingReader()
    revision_before = reader.current_revision(str(path))
    result = reader.read(request)

    expected = (
        target_count
        if scope_kind in {"selection", "layer", "layout"}
        else target_count + other_count
    )
    assert len(result.entities) == expected
    assert len({entity.entity_ref for entity in result.entities}) == expected
    assert all(
        entity.space == ("paper:Sheet1" if scope_kind == "layout" else "model")
        for entity in result.entities
    )
    target_entities = tuple(entity for entity in result.entities if entity.layer == "TARGET")
    assert all(entity.visible is (target_layer_state == "visible") for entity in target_entities)
    assert path.read_bytes() == before
    assert reader.current_revision(str(path)) == revision_before
