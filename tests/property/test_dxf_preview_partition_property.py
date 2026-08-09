# Feature: cad-ai-production-roadmap, Property 12: Preview phân hoạch mọi operation thành renderable và unrenderable

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import ezdxf
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.adapters.dxf_preview import DxfPreviewAdapter
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType


def _geometry_for(operation_type: OperationType) -> dict[str, Any]:
    line_geometry = {
        "start_mm": [0.0, 0.0],
        "end_mm": [10.0, 10.0],
    }
    dimension_geometry = {
        **line_geometry,
        "center_mm": [5.0, 5.0],
        "text_position_mm": [5.0, 7.0],
        "text_value": "10",
    }
    geometry_by_type: dict[OperationType, dict[str, Any]] = {
        OperationType.CREATE_CLOSED_POLYLINE: {
            "vertices_mm": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]],
        },
        OperationType.CREATE_POLYLINE: {
            "vertices_mm": [[0.0, 0.0], [10.0, 10.0]],
        },
        OperationType.CREATE_CIRCLE: {
            "center_mm": [5.0, 5.0],
            "diameter_mm": 2.0,
        },
        OperationType.CREATE_CIRCLES: {
            "centers_mm": [[2.0, 2.0]],
            "diameter_mm": 2.0,
        },
        OperationType.CREATE_ARC: {
            "center_mm": [5.0, 5.0],
            "radius_mm": 2.0,
            "start_angle_deg": 0.0,
            "end_angle_deg": 180.0,
        },
        OperationType.CREATE_LINE: line_geometry,
        OperationType.CREATE_CENTERLINE: line_geometry,
        OperationType.CREATE_TEXT: {
            "position_mm": [5.0, 5.0],
            "text": "preview",
        },
        OperationType.CREATE_CENTERMARK: {"center_mm": [5.0, 5.0]},
        OperationType.CREATE_LINEAR_DIMENSION: dimension_geometry,
        OperationType.CREATE_ALIGNED_DIMENSION: dimension_geometry,
        OperationType.CREATE_DIAMETER_DIMENSION: dimension_geometry,
        OperationType.CREATE_RADIUS_DIMENSION: dimension_geometry,
        OperationType.CREATE_ANGULAR_DIMENSION: dimension_geometry,
    }
    return geometry_by_type.get(operation_type, {})


@given(types=st.lists(st.sampled_from(list(OperationType)), unique=True))
@settings(max_examples=100, deadline=None)
def test_preview_partitions_every_operation_type(types: list[OperationType]) -> None:
    """**Validates: Requirements 4.7**"""
    plan = OperationPlan(
        plan_id="plan-property-12",
        job_id="job-property-12",
        document_id="doc-property-12",
        expected_revision="sha256:r1",
        profile_ref="profile@1",
        operations=tuple(
            Operation(
                operation_id=f"op-{index}",
                feature_id="feature",
                type=kind,
                layer=f"OP_{index}",
                geometry=_geometry_for(kind),
            )
            for index, kind in enumerate(types)
        ),
    )

    with TemporaryDirectory(prefix="cad-harness-preview-property-") as directory:
        adapter = DxfPreviewAdapter(Path(directory))
        result = adapter.preview(plan)
        dxf_artifact = next(artifact for artifact in result.artifacts if artifact.kind == "dxf")
        document = ezdxf.readfile(dxf_artifact.artifact_ref)
        rendered_layers = {entity.dxf.layer for entity in document.modelspace()}

        present = set(types)
        rendered = {
            operation.type for operation in plan.operations if operation.layer in rendered_layers
        }
        unrenderable = {OperationType(value) for value in adapter.preview_gaps(plan)}

        assert rendered.isdisjoint(unrenderable)
        assert rendered | unrenderable == present
        assert adapter.renderable_operations | adapter.unrenderable_operations == frozenset(
            OperationType
        )
        assert adapter.renderable_operations.isdisjoint(adapter.unrenderable_operations)
