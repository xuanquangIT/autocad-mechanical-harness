"""Property 61: approval review content remains complete and display-only."""

from __future__ import annotations

from typing import Any, cast

import pytest
from apps.engineer_desktop.view_model import (
    ApprovalViewInputs,
    DiffColor,
    OverlayColor,
    build_approval_view_model,
    thaw_json,
)
from hypothesis import given
from hypothesis import strategies as st

from cad_harness.diff.semantic_diff import DiffEntry, SemanticDiff
from cad_harness.domain.models.drawing_spec import (
    Assumption,
    DefaultRecord,
    DrawingSpec,
    MissingInput,
    StandardProfileRef,
)
from cad_harness.domain.models.job import CadJob, JobState
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.result import PreviewArtifact
from cad_harness.domain.models.validation import (
    Finding,
    Severity,
    ValidationReport,
    ValidationStage,
)


def _inputs(changes: tuple[str, ...]) -> ApprovalViewInputs:
    job = CadJob(
        job_id="job", document_id="document", expected_revision="revision", state=JobState.VALIDATED
    )
    spec = DrawingSpec(
        spec_id="spec",
        document_id=job.document_id,
        standard_profile=StandardProfileRef(profile_id="company", version="1"),
    )
    plan = OperationPlan(
        plan_id="plan",
        job_id=job.job_id,
        document_id=job.document_id,
        expected_revision=job.expected_revision,
        profile_ref="company@1",
        plan_hash="sha256:1234567890abcdef",
    )
    diff = SemanticDiff(
        plan_hash=plan.plan_hash,
        document_id=job.document_id,
        from_revision=job.expected_revision,
        entries=[
            DiffEntry(
                change=change,
                feature_id=f"feature-{index}",
                operation_id=f"operation-{index}",
                entity_type="AcDbLine",
                layer="0",
                summary=change,
                measurements={"nested": {"value": index}},
            )
            for index, change in enumerate(changes)
        ],
    )
    finding = Finding(
        rule_id="RULE",
        severity=Severity.WARNING,
        message="review",
        expected=2,
        actual=1,
        tolerance=0.1,
    )
    report = ValidationReport(
        validation_id="validation",
        job_id=job.job_id,
        stage=ValidationStage.PRE_COMMIT,
        findings=(finding,),
    )
    return ApprovalViewInputs(
        job=job,
        spec=spec,
        plan=plan,
        current_revision="revision",
        defaults_applied=(
            DefaultRecord(
                path="annotations.dimensions",
                value="none",
                source="company",
                source_version="1",
                reason="standard",
                impact="annotation",
            ),
        ),
        missing_inputs=(
            MissingInput(path="features.0.diameter_mm", reason="engineer value required"),
        ),
        assumptions=(Assumption(path="drawing.view", statement="top", affects_geometry=False),),
        before_artifacts=(PreviewArtifact(kind="svg", artifact_ref="before.svg", byte_size=10),),
        after_artifacts=(PreviewArtifact(kind="svg", artifact_ref="after.svg", byte_size=20),),
        semantic_diff=diff,
        validation_report=report,
    )


# Feature: cad-ai-production-roadmap, Property 61: complete immutable approval view model
@given(changes=st.lists(st.sampled_from(("added", "modified", "deleted"))).map(tuple))
def test_approval_view_model_preserves_every_required_review_value(
    changes: tuple[str, ...],
) -> None:
    """**Validates: Requirements 25.2, Property 61**"""
    inputs = _inputs(changes)
    view = build_approval_view_model(inputs)

    assert (view.document_id, view.revision, view.current_revision, view.state) == (
        inputs.job.document_id,
        inputs.job.expected_revision,
        inputs.current_revision,
        inputs.job.state,
    )
    assert view.plan_hash_prefix == "1234567890ab"
    assert thaw_json(view.spec_parameters) == inputs.spec.model_dump(mode="json")
    assert view.missing_inputs[0].path == inputs.missing_inputs[0].path
    assert view.defaults_applied[0].source == "company"
    assert view.defaults_applied[0].source_version == "1"
    assert view.assumptions[0].statement == inputs.assumptions[0].statement
    assert view.before_preview.label == "before"
    assert view.before_preview.artifacts[0].artifact_ref == inputs.before_artifacts[0].artifact_ref
    assert view.after_preview.label == "after"
    assert view.after_preview.artifacts[0].artifact_ref == inputs.after_artifacts[0].artifact_ref
    assert [entry.color for entry in view.semantic_diff] == [
        {"added": DiffColor.GREEN, "modified": DiffColor.YELLOW, "deleted": DiffColor.RED}[change]
        for change in changes
    ]
    assert view.findings[0].rule_id == inputs.validation_report.findings[0].rule_id
    assert thaw_json(view.findings[0].expected) == inputs.validation_report.findings[0].expected
    assert all(overlay.color is OverlayColor.PURPLE for overlay in view.validation_overlays)
    assert not hasattr(view, "approval_token")
    with pytest.raises(TypeError):
        cast(dict[str, Any], view.spec_parameters)["document_id"] = "mutated"
    if view.semantic_diff:
        with pytest.raises(TypeError):
            cast(dict[str, Any], view.semantic_diff[0].measurements)["nested"] = "mutated"
