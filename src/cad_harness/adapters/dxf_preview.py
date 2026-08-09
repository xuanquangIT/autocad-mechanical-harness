"""Preview-only adapter. Writes DXF/SVG artifacts and refuses to commit.

Useful as the default adapter during development: the whole pipeline can be exercised
end to end with no CAD installed and no possibility of touching a real drawing.
"""

from __future__ import annotations

from pathlib import Path

from cad_harness.adapters.base import BaseAdapter
from cad_harness.domain.models.operation_plan import OperationPlan, OperationType
from cad_harness.domain.models.result import PreviewArtifact, PreviewResult
from cad_harness.domain.ports.autocad_adapter import AdapterCapability, AdapterStatus
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id
from cad_harness.preview.dxf_writer import write_dxf
from cad_harness.preview.svg_writer import write_svg


class DxfPreviewAdapter(BaseAdapter):
    adapter_type = "dxf_preview"
    capabilities = frozenset({AdapterCapability.PREVIEW})
    renderable_operations = frozenset(
        {
            OperationType.CREATE_CLOSED_POLYLINE,
            OperationType.CREATE_POLYLINE,
            OperationType.CREATE_CIRCLE,
            OperationType.CREATE_CIRCLES,
            OperationType.CREATE_ARC,
            OperationType.CREATE_LINE,
            OperationType.CREATE_TEXT,
            OperationType.CREATE_CENTERLINE,
            OperationType.CREATE_CENTERMARK,
            OperationType.CREATE_LINEAR_DIMENSION,
            OperationType.CREATE_ALIGNED_DIMENSION,
            OperationType.CREATE_DIAMETER_DIMENSION,
            OperationType.CREATE_RADIUS_DIMENSION,
            OperationType.CREATE_ANGULAR_DIMENSION,
        }
    )
    supported_operations = renderable_operations
    unrenderable_operations = frozenset(OperationType) - renderable_operations

    def __init__(self, preview_directory: Path, *, company_approved: bool = False) -> None:
        self.preview_directory = preview_directory
        self.company_approved = company_approved

    def status(self) -> AdapterStatus:
        return AdapterStatus(
            adapter_type=self.adapter_type,
            available=True,
            capabilities=tuple(sorted(self.capabilities, key=lambda c: c.value)),
            message="Preview only. Commit, inspect and export are not available.",
        )

    def preview(self, plan: OperationPlan) -> PreviewResult:
        self.require(AdapterCapability.PREVIEW)
        preview_id = new_id(IdPrefix.PREVIEW)
        folder = self.preview_directory / preview_id
        dxf_path = write_dxf(plan, folder / "preview.dxf")
        svg_path = write_svg(plan, folder / "preview.svg")

        return PreviewResult(
            preview_id=preview_id,
            job_id=plan.job_id,
            plan_hash=plan.plan_hash or plan.compute_hash(),
            artifacts=(
                PreviewArtifact(
                    kind="dxf",
                    artifact_ref=str(dxf_path),
                    byte_size=dxf_path.stat().st_size,
                ),
                PreviewArtifact(
                    kind="svg",
                    artifact_ref=str(svg_path),
                    byte_size=svg_path.stat().st_size,
                ),
            ),
            company_approved=self.company_approved,
        )

    def validate_revision(self, document_id: str, expected_revision: str) -> bool:
        """No live document to compare against, so nothing can be confirmed."""
        return False

    def preview_gaps(self, plan: OperationPlan) -> list[str]:
        """Operation types this adapter cannot draw, reported instead of dropped."""
        return sorted(
            {
                operation.type.value
                for operation in plan.operations
                if operation.type in self.unrenderable_operations
            }
        )
