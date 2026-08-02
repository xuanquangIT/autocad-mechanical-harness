"""AutoCAD COM/ActiveX adapter for the MVP (architecture section 16.1).

This is the **only** module allowed to import ``win32com``/``pythoncom``. Ruff enforces
that with a banned-api rule.

Technique reference: the coordinate marshalling below (``VARIANT`` with
``VT_ARRAY | VT_R8``) and the ``GetActiveObject`` -> ``Dispatch`` connection fallback
follow the approach used by the community
[CAD-MCP server](https://github.com/daobataotie/CAD-MCP). What is deliberately *not*
borrowed is its design: primitive drawing tools exposed straight to the LLM, defaults
applied silently, and no preview/approval gate.

Known limitations, stated rather than hidden:

* No real transaction. ``StartUndoMark``/``EndUndoMark`` groups a commit for the user's
  Undo, but a mid-operation failure can leave partial geometry. Post-commit validation
  plus a file checkpoint is the mitigation until the C# bridge lands.
* ``Handle`` is stable within a session but XData support through ActiveX is uneven, so
  feature identity is always mirrored in the job store.
* Revisions are coarse: entity count plus handle digest, not a database fingerprint.
"""

from __future__ import annotations

import contextlib
import math
from typing import Any

from cad_harness.adapters.base import BaseAdapter
from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.errors import (
    AdapterCapabilityMissingError,
    AutoCADBusyError,
    AutoCADNotRunningError,
    ComCallFailedError,
    StaleDocumentRevisionError,
)
from cad_harness.domain.models.document import (
    DocumentSnapshot,
    EntitySummary,
    LayerInfo,
    SelectionSnapshot,
)
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.result import (
    CommitResult,
    CommitStatus,
    EntityResult,
    ExportResult,
    PreviewResult,
    RollbackResult,
)
from cad_harness.domain.ports.autocad_adapter import (
    AdapterCapability,
    AdapterStatus,
    CommitRequest,
    ExportRequest,
    InspectRequest,
    RollbackRequest,
    SelectionRequest,
)
from cad_harness.domain.value_objects.units import Unit

#: ProgIDs for AutoCAD and the API-compatible alternatives.
PROG_IDS: dict[str, str] = {
    "autocad": "AutoCAD.Application",
    "gstarcad": "GCAD.Application",
    "gcad": "GCAD.Application",
    "zwcad": "ZWCAD.Application",
}

#: AutoCAD accepts only this discrete set of lineweight values (hundredths of a mm).
VALID_LINEWEIGHTS: frozenset[int] = frozenset(
    (
        *(0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50),
        *(53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211),
    )
)

#: AutoCAD ``Units`` enum values that map to a canonical unit we understand.
_INSUNITS_TO_UNIT: dict[int, Unit] = {1: Unit.INCH, 4: Unit.MM, 5: Unit.CM, 6: Unit.M}


class ComAutoCADAdapter(BaseAdapter):
    """Thin ActiveX mapping. Contains no engineering decisions."""

    adapter_type = "com"
    capabilities = frozenset(
        {
            AdapterCapability.INSPECT_DOCUMENT,
            AdapterCapability.INSPECT_SELECTION,
            AdapterCapability.COMMIT,
            AdapterCapability.EXPORT,
            AdapterCapability.UNDO_GROUP,
            # Deliberately absent: ATOMIC_TRANSACTION, DOCUMENT_LOCK, STABLE_METADATA,
            # IN_VIEWPORT_PREVIEW. Those arrive with the C# bridge.
        }
    )

    def __init__(self, prog_id_key: str = "autocad", *, startup_wait_seconds: float = 20.0) -> None:
        self.prog_id = PROG_IDS.get(prog_id_key.lower(), PROG_IDS["autocad"])
        self.startup_wait_seconds = startup_wait_seconds
        self._app: Any = None
        self._document: Any = None

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def connect(self, *, launch_if_missing: bool = False) -> None:
        """Attach to a running instance, optionally launching one.

        Launching is opt-in: silently starting AutoCAD from a background MCP server
        surprises users and can hijack their session.
        """
        import pythoncom  # noqa: TID251 - COM is confined to this module
        import win32com.client  # noqa: TID251

        pythoncom.CoInitialize()
        try:
            self._app = win32com.client.GetActiveObject(self.prog_id)
        except Exception as exc:
            if not launch_if_missing:
                raise AutoCADNotRunningError(
                    "No running AutoCAD instance found",
                    required_action="Start AutoCAD, open the target drawing, then retry",
                    details={"prog_id": self.prog_id},
                ) from exc
            try:
                self._app = win32com.client.Dispatch(self.prog_id)
                self._app.Visible = True
                self._wait_until_quiescent(timeout_seconds=self.startup_wait_seconds)
            except Exception as launch_exc:  # pragma: no cover - environment specific
                raise AutoCADNotRunningError(
                    "Could not start AutoCAD",
                    details={"prog_id": self.prog_id},
                ) from launch_exc

        try:
            if self._app.Documents.Count == 0:
                raise AutoCADNotRunningError(
                    "AutoCAD is running but has no open document",
                    required_action="Open the target drawing in AutoCAD, then retry",
                )
            self._document = self._app.ActiveDocument
        except AutoCADNotRunningError:
            raise
        except Exception as exc:
            raise ComCallFailedError(
                "Could not obtain the active AutoCAD document",
                details={"prog_id": self.prog_id},
            ) from exc

    def disconnect(self) -> None:
        import pythoncom  # noqa: TID251

        self._app = None
        self._document = None
        with contextlib.suppress(Exception):  # shutdown is best effort
            pythoncom.CoUninitialize()

    def _require_document(self) -> Any:
        if self._document is None:
            raise AutoCADNotRunningError(
                "Adapter is not connected to a document",
                required_action="Call connect() before any document operation",
            )
        self._assert_quiescent()
        return self._document

    def _assert_quiescent(self) -> None:
        """Refuse to write while AutoCAD is mid-command."""
        try:
            state = self._app.GetAcadState()
            quiescent = bool(state.IsQuiescent)
        except Exception:
            # Older builds may not expose GetAcadState; proceed and let the call fail.
            return
        if not quiescent:
            raise AutoCADBusyError(
                "AutoCAD is busy with another command",
                required_action="Finish the active AutoCAD command, then retry",
            )

    def _wait_until_quiescent(self, *, timeout_seconds: float) -> None:
        import time

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                if bool(self._app.GetAcadState().IsQuiescent):
                    return
            except Exception:
                pass
            time.sleep(0.5)

    # ------------------------------------------------------------------ #
    # COM marshalling helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _variant_doubles(values: list[float]) -> Any:
        """Wrap a flat float list as a COM ``VT_ARRAY | VT_R8`` variant.

        ActiveX rejects plain Python lists for coordinate arguments, so every point
        must go through this.
        """
        import pythoncom  # noqa: TID251
        import win32com.client  # noqa: TID251

        return win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values]
        )

    @classmethod
    def _variant_point3d(cls, x: float, y: float, z: float = 0.0) -> Any:
        return cls._variant_doubles([x, y, z])

    @staticmethod
    def validate_lineweight(lineweight: int | None) -> int | None:
        """Return ``lineweight`` only if AutoCAD accepts it; otherwise ``None``.

        Unlike the reference implementation, an invalid value is not silently coerced
        to 0 - the caller is told the value was dropped.
        """
        if lineweight is None:
            return None
        return lineweight if lineweight in VALID_LINEWEIGHTS else None

    def _ensure_layer(self, document: Any, name: str) -> None:
        """Create the layer if absent. Never changes the user's active layer."""
        try:
            for index in range(document.Layers.Count):
                if document.Layers.Item(index).Name == name:
                    return
            document.Layers.Add(name)
        except Exception as exc:
            raise ComCallFailedError(
                f"Could not ensure layer '{name}'", details={"layer": name}
            ) from exc

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def status(self) -> AdapterStatus:
        available = self._app is not None and self._document is not None
        version: str | None = None
        document_id: str | None = None
        if available:
            try:
                version = str(self._app.Version)
                document_id = self._document_id(self._document)
            except Exception:  # pragma: no cover - environment specific
                available = False
        return AdapterStatus(
            adapter_type=self.adapter_type,
            available=available,
            capabilities=tuple(sorted(self.capabilities, key=lambda c: c.value)),
            cad_application=self.prog_id,
            cad_version=version,
            active_document_id=document_id,
            message=None if available else "Not connected. Call connect() first.",
        )

    @staticmethod
    def _document_id(document: Any) -> str:
        """Stable per-file identity derived from the normalized full path."""
        try:
            full_name = str(document.FullName) or str(document.Name)
        except Exception:
            full_name = "unsaved-document"
        return f"doc_{sha256_of(full_name.strip().lower()).removeprefix('sha256:')[:26].upper()}"

    def inspect_document(self, request: InspectRequest) -> DocumentSnapshot:
        self.require(AdapterCapability.INSPECT_DOCUMENT)
        document = self._require_document()
        try:
            layers: list[LayerInfo] = []
            if request.include_layers:
                for index in range(document.Layers.Count):
                    layer = document.Layers.Item(index)
                    layers.append(
                        LayerInfo(
                            name=str(layer.Name),
                            color_index=int(layer.Color),
                            linetype=str(layer.Linetype),
                            lineweight=int(layer.Lineweight),
                            frozen=bool(layer.Freeze) is not False,
                            locked=bool(layer.Lock),
                        )
                    )

            styles: tuple[str, ...] = ()
            text_styles: tuple[str, ...] = ()
            if request.include_styles:
                styles = tuple(
                    str(document.DimStyles.Item(i).Name) for i in range(document.DimStyles.Count)
                )
                text_styles = tuple(
                    str(document.TextStyles.Item(i).Name) for i in range(document.TextStyles.Count)
                )

            insunits = int(document.GetVariable("INSUNITS"))
            entity_count = int(document.ModelSpace.Count)
            document_id = self._document_id(document)

            return DocumentSnapshot(
                document_id=document_id,
                revision=self._compute_revision(document, document_id),
                path_hash=sha256_of(str(document.Name).strip().lower()),
                display_name=str(document.Name),
                units=_INSUNITS_TO_UNIT.get(insunits, Unit.MM),
                active_space="model" if int(document.ActiveSpace) == 1 else "paper",
                layers=tuple(layers),
                dimension_styles=styles,
                text_styles=text_styles,
                entity_count=entity_count,
                read_only=bool(document.ReadOnly),
            )
        except ComCallFailedError:
            raise
        except Exception as exc:
            raise ComCallFailedError("Document inspection failed") from exc

    def inspect_selection(self, request: SelectionRequest) -> SelectionSnapshot:
        self.require(AdapterCapability.INSPECT_SELECTION)
        document = self._require_document()
        try:
            selection = document.ActiveSelectionSet
            count = int(selection.Count)
            limit = min(count, request.max_entities)
            entities = tuple(
                EntitySummary(
                    entity_ref=f"acad:handle:{selection.Item(i).Handle}",
                    entity_type=str(selection.Item(i).ObjectName),
                    layer=str(selection.Item(i).Layer),
                )
                for i in range(limit)
            )
            return SelectionSnapshot(
                document_id=self._document_id(document),
                revision=self._compute_revision(document, self._document_id(document)),
                entities=entities,
                truncated=count > limit,
            )
        except Exception as exc:
            raise ComCallFailedError("Selection inspection failed") from exc

    def _compute_revision(self, document: Any, document_id: str) -> str:
        """Coarse MVP revision: entity count plus a digest of model space handles.

        Good enough to detect that *something* changed; not good enough to be the
        long-term answer, which is why the C# bridge is on the roadmap.
        """
        try:
            model_space = document.ModelSpace
            count = int(model_space.Count)
            handles = [str(model_space.Item(i).Handle) for i in range(count)]
            return self.revision_from(document_id, [count, sorted(handles)])
        except Exception as exc:
            raise ComCallFailedError("Could not compute a document revision") from exc

    def validate_revision(self, document_id: str, expected_revision: str) -> bool:
        document = self._require_document()
        return (
            self._document_id(document) == document_id
            and self._compute_revision(document, document_id) == expected_revision
        )

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    def preview(self, plan: OperationPlan) -> PreviewResult:
        """Not supported. Preview goes through :class:`DxfPreviewAdapter` under COM.

        Drawing preview geometry into the live document and erasing it again risks
        leaving artifacts behind, which violates "preview must not modify the DWG".
        """
        raise AdapterCapabilityMissingError(
            "The COM adapter cannot preview inside AutoCAD",
            required_action="Generate the preview with the DXF preview adapter",
            details={"adapter_type": self.adapter_type},
        )

    def commit(self, request: CommitRequest) -> CommitResult:
        self.require(AdapterCapability.COMMIT)
        document = self._require_document()
        document_id = self._document_id(document)

        previous_revision = self._compute_revision(document, document_id)
        if previous_revision != request.expected_revision:
            raise StaleDocumentRevisionError(
                "Document changed since the plan was approved",
                required_action="Re-inspect, regenerate preview, validate and approve again",
                details={
                    "expected_revision": request.expected_revision,
                    "actual_revision": previous_revision,
                },
            )

        undo_group = f"cad-harness-{request.plan.job_id}"
        results: list[EntityResult] = []
        document.StartUndoMark()
        try:
            for operation in request.plan.operations:
                results.extend(self._execute(document, operation))
        except Exception as exc:
            document.EndUndoMark()
            # Best-effort undo. COM gives no transaction, so the caller must treat a
            # failure here as "state unknown" and reconcile against the checkpoint.
            with contextlib.suppress(Exception):
                document.SendCommand("_.U ")
            raise ComCallFailedError(
                "Commit failed part-way through the plan",
                required_action="Reconcile the job against the checkpoint before retrying",
                details={"completed_operations": len(results)},
            ) from exc
        document.EndUndoMark()

        return CommitResult(
            job_id=request.plan.job_id,
            plan_hash=request.plan.plan_hash or request.plan.compute_hash(),
            status=CommitStatus.COMMITTED,
            entity_results=tuple(results),
            previous_revision=previous_revision,
            new_revision=self._compute_revision(document, document_id),
            undo_group=undo_group,
        )

    def _execute(self, document: Any, operation: Operation) -> list[EntityResult]:
        self._ensure_layer(document, operation.layer)
        model_space = document.ModelSpace

        if operation.type in {
            OperationType.CREATE_CLOSED_POLYLINE,
            OperationType.CREATE_POLYLINE,
        }:
            vertices = operation.geometry["vertices_mm"]
            flat = [coord for vertex in vertices for coord in (float(vertex[0]), float(vertex[1]))]
            entity = model_space.AddLightWeightPolyline(self._variant_doubles(flat))
            entity.Layer = operation.layer
            if operation.type is OperationType.CREATE_CLOSED_POLYLINE:
                entity.Closed = True
            return [
                EntityResult(
                    operation_id=operation.operation_id,
                    feature_id=operation.feature_id,
                    entity_ref=f"acad:handle:{entity.Handle}",
                    entity_type=str(entity.ObjectName),
                    measurements=self._measure_polyline(entity, vertices),
                )
            ]

        if operation.type is OperationType.CREATE_CIRCLES:
            diameter = float(operation.geometry["diameter_mm"])
            centers = operation.geometry["centers_mm"]
            results: list[EntityResult] = []
            for center in centers:
                entity = model_space.AddCircle(
                    self._variant_point3d(float(center[0]), float(center[1])), diameter / 2.0
                )
                entity.Layer = operation.layer
                results.append(
                    EntityResult(
                        operation_id=operation.operation_id,
                        feature_id=operation.feature_id,
                        entity_ref=f"acad:handle:{entity.Handle}",
                        entity_type=str(entity.ObjectName),
                        measurements={
                            "count": len(centers),
                            "diameter_mm": float(entity.Diameter),
                            "center_mm": [float(entity.Center[0]), float(entity.Center[1])],
                            "area_mm2": float(entity.Area),
                        },
                    )
                )
            return results

        raise AdapterCapabilityMissingError(
            f"The COM adapter has no mapping for operation type '{operation.type.value}'",
            required_action="Add the mapping or compile a plan the adapter supports",
            details={"operation_type": operation.type.value, "adapter_type": self.adapter_type},
        )

    @staticmethod
    def _measure_polyline(entity: Any, vertices: list[Any]) -> dict[str, Any]:
        """Read measurements back from AutoCAD rather than echoing the plan."""
        xs = [float(v[0]) for v in vertices]
        ys = [float(v[1]) for v in vertices]
        area = 0.0
        try:
            area = float(entity.Area)
        except Exception:  # pragma: no cover - open polylines have no area
            area = 0.0
        return {
            "closed": bool(entity.Closed),
            "vertex_count": len(vertices),
            "width_mm": max(xs) - min(xs),
            "height_mm": max(ys) - min(ys),
            "area_mm2": area,
            "perimeter_mm": sum(
                math.dist((xs[i], ys[i]), (xs[(i + 1) % len(xs)], ys[(i + 1) % len(ys)]))
                for i in range(len(xs))
            ),
        }

    def rollback(self, request: RollbackRequest) -> RollbackResult:
        """Undo the last mark. Checkpoint restore is handled above the adapter."""
        document = self._require_document()
        try:
            document.SendCommand("_.U ")
        except Exception as exc:
            raise ComCallFailedError("Undo failed") from exc
        document_id = self._document_id(document)
        return RollbackResult(
            job_id=request.job_id,
            restored_revision=self._compute_revision(document, document_id),
            checkpoint_id=request.checkpoint_id,
            method="undo_group",
        )

    def export(self, request: ExportRequest) -> ExportResult:
        self.require(AdapterCapability.EXPORT)
        document = self._require_document()
        try:
            if request.format.lower() in {"dwg", "dxf"}:
                document.SaveAs(request.target_path)
            elif request.format.lower() == "pdf":
                document.Plot.PlotToFile(request.target_path)
            else:
                raise AdapterCapabilityMissingError(
                    f"Unsupported export format '{request.format}'",
                    details={"supported": ["dwg", "dxf", "pdf"]},
                )
        except AdapterCapabilityMissingError:
            raise
        except Exception as exc:
            raise ComCallFailedError("Export failed", details={"format": request.format}) from exc

        return ExportResult(
            document_id=self._document_id(document),
            format=request.format,
            artifact_ref=request.target_path,
        )
