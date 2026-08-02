"""Render an OperationPlan to a temporary DXF using ezdxf.

This is the safe preview path: it produces a standalone file the engineer can open,
without any connection to the live drawing.
"""

from __future__ import annotations

from pathlib import Path

from cad_harness.domain.models.operation_plan import OperationPlan, OperationType

#: Preview colours (architecture section 7.6). Applied to the preview file only.
COLOR_NEW = 3  # green
COLOR_MODIFIED = 2  # yellow
COLOR_DELETED = 1  # red
COLOR_VIOLATION = 6  # magenta


def write_dxf(plan: OperationPlan, target: Path) -> Path:
    """Write ``plan`` to ``target`` and return the path.

    Raises:
        RuntimeError: if ezdxf is unavailable. Preview is optional at install time,
            so the failure is explicit rather than a silent no-op.
    """
    try:
        import ezdxf
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError(
            "ezdxf is required for DXF preview. Install the base dependencies."
        ) from exc

    document = ezdxf.new(setup=True)
    model = document.modelspace()

    for operation in plan.operations:
        layer = operation.layer
        if layer not in document.layers:
            document.layers.add(layer)
        attributes = {"layer": layer, "color": COLOR_NEW}

        if operation.type in {
            OperationType.CREATE_CLOSED_POLYLINE,
            OperationType.CREATE_POLYLINE,
        }:
            vertices = [(float(v[0]), float(v[1])) for v in operation.geometry["vertices_mm"]]
            model.add_lwpolyline(
                vertices,
                close=operation.type is OperationType.CREATE_CLOSED_POLYLINE,
                dxfattribs=attributes,
            )
        elif operation.type is OperationType.CREATE_CIRCLES:
            radius = float(operation.geometry["diameter_mm"]) / 2.0
            for center in operation.geometry["centers_mm"]:
                model.add_circle(
                    (float(center[0]), float(center[1])), radius, dxfattribs=attributes
                )
        elif operation.type is OperationType.CREATE_CIRCLE:
            center = operation.geometry["center_mm"]
            model.add_circle(
                (float(center[0]), float(center[1])),
                float(operation.geometry["diameter_mm"]) / 2.0,
                dxfattribs=attributes,
            )
        elif operation.type is OperationType.CREATE_LINE:
            start, end = operation.geometry["start_mm"], operation.geometry["end_mm"]
            model.add_line(
                (float(start[0]), float(start[1])),
                (float(end[0]), float(end[1])),
                dxfattribs=attributes,
            )
        elif operation.type is OperationType.CREATE_CENTERMARK:
            center = operation.geometry["center_mm"]
            size = 2.5
            cx, cy = float(center[0]), float(center[1])
            model.add_line((cx - size, cy), (cx + size, cy), dxfattribs=attributes)
            model.add_line((cx, cy - size), (cx, cy + size), dxfattribs=attributes)
        # Unmapped operation types are skipped here and reported as a preview
        # capability gap by the preview service, not silently dropped.

    target.parent.mkdir(parents=True, exist_ok=True)
    document.saveas(target)
    return target


def unsupported_operations(plan: OperationPlan) -> list[str]:
    """Operation types the DXF preview cannot render yet."""
    renderable = {
        OperationType.CREATE_CLOSED_POLYLINE,
        OperationType.CREATE_POLYLINE,
        OperationType.CREATE_CIRCLE,
        OperationType.CREATE_CIRCLES,
        OperationType.CREATE_LINE,
        OperationType.CREATE_CENTERMARK,
    }
    return sorted({op.type.value for op in plan.operations if op.type not in renderable})
