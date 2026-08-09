"""Property 38: block identity, depth bounds, and non-uniform scale survive reads."""

from pathlib import Path

import ezdxf
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
from cad_harness.domain.models.drawing_model import (
    BlockReferenceGeometry,
    EllipseGeometry,
    ReadScope,
)
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef


def _walk(geometry: BlockReferenceGeometry):
    for child in geometry.child_entities:
        yield child.geometry
        if isinstance(child.geometry, BlockReferenceGeometry):
            yield from _walk(child.geometry)


# Feature: cad-ai-production-roadmap, Property 38: blocks preserved, depth handled, non-uniform scale flagged
@given(
    nesting=st.integers(min_value=1, max_value=5), depth_limit=st.integers(min_value=1, max_value=3)
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
def test_blocks_remain_single_entities_with_bounded_children_and_distorted_circles(
    tmp_path: Path, nesting: int, depth_limit: int
) -> None:
    """**Validates: Requirements 13.13, 13.14**"""
    path = tmp_path / "blocks.dxf"
    document = ezdxf.new("R2018")
    document.header["$INSUNITS"] = 4
    inner = document.blocks.new("BLOCK_0")
    inner.add_circle((0, 0), radius=2.0)
    previous = "BLOCK_0"
    for level in range(1, nesting):
        current = f"BLOCK_{level}"
        block = document.blocks.new(current)
        block.add_blockref(previous, (1, 0))
        previous = current
    document.modelspace().add_blockref(
        previous, (10, 20), dxfattribs={"xscale": 2.0, "yscale": 1.0}
    )
    document.saveas(path)

    result = DxfDrawingReader().read(
        DrawingReadRequest(
            source=DrawingSourceRef(kind="file", format="dxf", ref=str(path)),
            scope=ReadScope(),
            max_entities=100,
            max_block_nesting_depth=depth_limit,
        )
    )

    assert len(result.entities) == 1
    top = result.entities[0]
    assert top.entity_type == "AcDbBlockReference"
    assert isinstance(top.geometry, BlockReferenceGeometry)
    assert top.geometry.non_uniform_scale
    assert all(child.non_uniform_scale for child in top.geometry.child_entities)
    descendants = tuple(_walk(top.geometry))
    if depth_limit >= nesting:
        assert any(isinstance(item, EllipseGeometry) for item in descendants)
        assert top.geometry.children_beyond_depth == 0
    else:
        assert top.geometry.children_beyond_depth > 0
