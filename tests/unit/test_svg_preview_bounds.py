"""SVG preview bounds include full circle extents, not only their centres."""

from __future__ import annotations

import re
from pathlib import Path

from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.preview.svg_writer import write_svg


def test_standalone_circle_is_not_clipped_by_the_svg_viewport(tmp_path: Path) -> None:
    plan = OperationPlan(
        plan_id="plan-circle-bounds",
        job_id="job-circle-bounds",
        document_id="doc-circle-bounds",
        expected_revision="sha256:circle-bounds",
        profile_ref="demo-profile@1.0",
        operations=(
            Operation(
                operation_id="op:circle:circle",
                feature_id="circle",
                type=OperationType.CREATE_CIRCLE,
                layer="0",
                geometry={"center_mm": [0.0, 0.0], "diameter_mm": 200.0},
                expected={"radius_mm": 100.0},
            ),
        ),
    ).with_hash()

    target = write_svg(plan, tmp_path / "circle.svg")
    svg = target.read_text(encoding="utf-8")
    viewport = re.search(
        r'<svg[^>]* width="([0-9.]+)" height="([0-9.]+)"',
        svg,
    )
    assert viewport is not None
    width, height = (float(item) for item in viewport.groups())
    circle = re.search(r'<circle cx="([0-9.]+)" cy="([0-9.]+)" r="([0-9.]+)"', svg)
    assert circle is not None
    center_x, center_y, radius = (float(item) for item in circle.groups())

    assert center_x - radius > 0.0
    assert center_y - radius > 0.0
    assert center_x + radius < width
    assert center_y + radius < height


def test_recognized_radius_circle_uses_the_same_complete_svg_bounds(tmp_path: Path) -> None:
    plan = OperationPlan(
        plan_id="plan-recognized-circle-bounds",
        job_id="job-recognized-circle-bounds",
        document_id="doc-recognized-circle-bounds",
        expected_revision="sha256:recognized-circle-bounds",
        profile_ref="demo-profile@1.0",
        operations=(
            Operation(
                operation_id="op:recognized:source-circle",
                feature_id="recognized-circle",
                type=OperationType.CREATE_CIRCLE,
                layer="OBJECT",
                geometry={"center_mm": [10.0, 20.0], "radius_mm": 25.0},
                expected={"diameter_mm": 50.0},
            ),
        ),
    ).with_hash()

    svg = write_svg(plan, tmp_path / "recognized-circle.svg").read_text(encoding="utf-8")
    viewport = re.search(
        r'<svg[^>]* width="([0-9.]+)" height="([0-9.]+)"',
        svg,
    )
    circle = re.search(r'<circle cx="([0-9.]+)" cy="([0-9.]+)" r="([0-9.]+)"', svg)

    assert viewport is not None and circle is not None
    width, height = (float(item) for item in viewport.groups())
    center_x, center_y, radius = (float(item) for item in circle.groups())
    assert center_x - radius > 0.0
    assert center_y - radius > 0.0
    assert center_x + radius < width
    assert center_y + radius < height
