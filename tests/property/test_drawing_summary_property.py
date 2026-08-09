"""Property 35: summaries count exactly without carrying detailed geometry."""

from pathlib import Path

import ezdxf
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
from cad_harness.domain.models.drawing_model import ReadScope
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef


# Feature: cad-ai-production-roadmap, Property 35: summary has no geometry and unsupported entities do not block
@given(
    supported_count=st.integers(min_value=0, max_value=12),
    unsupported_count=st.integers(min_value=0, max_value=12),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
def test_summary_counts_supported_and_unsupported_without_geometry(
    tmp_path: Path, supported_count: int, unsupported_count: int
) -> None:
    """**Validates: Requirements 13.5, 13.8**"""
    path = tmp_path / "summary.dxf"
    document = ezdxf.new("R2018")
    document.header["$INSUNITS"] = 4
    model = document.modelspace()
    for index in range(supported_count):
        model.add_line((index, 0), (index + 1, 1))
    for index in range(unsupported_count):
        model.add_spline([(index, 0), (index + 0.5, 1), (index + 1, 0)])
    document.saveas(path)

    source = DrawingSourceRef(kind="file", format="dxf", ref=str(path))
    reader = DxfDrawingReader()
    summary = reader.summarize(
        DrawingReadRequest(source=source, scope=None, max_entities=100, max_block_nesting_depth=3)
    )
    model_result = reader.read(
        DrawingReadRequest(
            source=source, scope=ReadScope(), max_entities=100, max_block_nesting_depth=3
        )
    )

    assert not hasattr(summary, "entities")
    assert summary.counts_by_entity_type.get("AcDbLine", 0) == supported_count
    assert summary.counts_by_entity_type.get("spline", 0) == unsupported_count
    assert sum(item.count for item in summary.unsupported) == unsupported_count
    assert summary.coverage_complete is (unsupported_count == 0)
    assert len(model_result.entities) == supported_count
    assert model_result.coverage_complete is (unsupported_count == 0)
