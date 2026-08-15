"""Pure COM-observation tests; no AutoCAD process is contacted."""

from __future__ import annotations

import math
from typing import Any

import pytest

from cad_harness.adapters.autocad_com import ComAutoCADAdapter
from cad_harness.domain.errors import ReadScopeTooLargeError
from cad_harness.domain.models.drawing_model import ReadScope
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef


class _Collection(list[Any]):
    @property
    def Count(self) -> int:  # noqa: N802 - mirrors ActiveX
        return len(self)


class _Layer:
    Name = "OBJECT"
    Color = 7
    Linetype = "Continuous"
    Lineweight = 25
    Freeze = False
    LayerOn = True
    Lock = False


class _Style:
    Name = "Standard"


class _Entity:
    Layer = "OBJECT"
    Visible = True

    def __init__(self, handle: str, entity_type: str, **values: Any) -> None:
        self.Handle = handle
        self.ObjectName = entity_type
        for name, value in values.items():
            setattr(self, name, value)

    def GetBulge(self, index: int) -> float:  # noqa: N802 - mirrors ActiveX
        return float(getattr(self, "Bulges", ())[index])


class _Document:
    Name = "existing-test.dwg"
    FullName = "C:\\controlled\\existing-test.dwg"
    ActiveSpace = 1
    ReadOnly = False

    def __init__(self, entities: list[_Entity]) -> None:
        self.ModelSpace = _Collection(entities)
        self.Layers = _Collection([_Layer()])
        self.DimStyles = _Collection([_Style()])
        self.TextStyles = _Collection([_Style()])

    @staticmethod
    def GetVariable(name: str) -> int:  # noqa: N802 - mirrors ActiveX
        assert name == "INSUNITS"
        return 4


class _SemanticAdapter(ComAutoCADAdapter):
    def __init__(self, document: _Document) -> None:
        super().__init__()
        self._semantic_document = document

    def _require_document(self) -> _Document:
        return self._semantic_document


def _request(adapter: _SemanticAdapter, *, max_entities: int = 20) -> DrawingReadRequest:
    document_id = adapter._document_id(adapter._semantic_document)
    return DrawingReadRequest(
        source=DrawingSourceRef(kind="active_document", format="dwg", ref=document_id),
        scope=ReadScope(kind="model_space"),
        max_entities=max_entities,
        max_block_nesting_depth=3,
    )


def test_com_semantic_read_observes_supported_geometry_and_counts_the_rest() -> None:
    entities = [
        _Entity("A1", "AcDbLine", StartPoint=(0, 0, 0), EndPoint=(10, 5, 0)),
        _Entity("B2", "AcDbCircle", Center=(20, 10, 0), Radius=4.0),
        _Entity(
            "C3",
            "AcDbArc",
            Center=(40, 10, 0),
            Radius=5.0,
            StartAngle=math.radians(300),
            EndAngle=math.radians(60),
        ),
        _Entity(
            "D4",
            "AcDbPolyline",
            Coordinates=(0, 20, 10, 20, 10, 25),
            Bulges=(0.0, 0.0, 0.0),
            Closed=True,
        ),
        _Entity("E5", "AcDbText"),
    ]
    adapter = _SemanticAdapter(_Document(entities))

    model = adapter.inspect_semantic_drawing(_request(adapter))

    assert model.revision == adapter._compute_revision(
        adapter._semantic_document, model.document_id
    )
    assert [entity.entity_ref for entity in model.entities] == [
        "acad:handle:A1",
        "acad:handle:B2",
        "acad:handle:C3",
        "acad:handle:D4",
    ]
    assert model.entities[0].bounding_box_mm == (0.0, 0.0, 10.0, 5.0)
    assert model.entities[1].bounding_box_mm == (16.0, 6.0, 24.0, 14.0)
    assert model.entities[2].bounding_box_mm == pytest.approx(
        (42.5, 5.6698729811, 45, 14.3301270189)
    )
    assert model.entities[3].bounding_box_mm == (0.0, 20.0, 10.0, 25.0)
    assert model.unsupported[0].entity_type == "AcDbText"
    assert model.coverage_complete is False
    assert model.source_unit_code == "mm"
    assert model.to_mm_factor == 1.0


def test_com_semantic_read_rejects_budget_and_curved_polyline_is_not_flattened() -> None:
    curved = _Entity(
        "BULGE",
        "AcDbPolyline",
        Coordinates=(0, 0, 10, 0),
        Bulges=(1.0, 0.0),
        Closed=False,
    )
    adapter = _SemanticAdapter(_Document([curved]))
    model = adapter.inspect_semantic_drawing(_request(adapter))
    assert model.entities == ()
    assert model.unsupported[0].entity_type == "AcDbPolyline:unsupported_geometry"

    oversized = _SemanticAdapter(
        _Document(
            [
                _Entity("L1", "AcDbLine", StartPoint=(0, 0, 0), EndPoint=(1, 0, 0)),
                _Entity("L2", "AcDbLine", StartPoint=(0, 1, 0), EndPoint=(1, 1, 0)),
            ]
        )
    )
    with pytest.raises(ReadScopeTooLargeError):
        oversized.inspect_semantic_drawing(_request(oversized, max_entities=1))
