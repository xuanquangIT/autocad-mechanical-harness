"""Deeply immutable, side-effect-free data for the engineer approval surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from cad_harness.diff.semantic_diff import SemanticDiff
from cad_harness.domain.canonical import hash_prefix
from cad_harness.domain.models.drawing_spec import (
    Assumption,
    DefaultRecord,
    DrawingSpec,
    MissingInput,
)
from cad_harness.domain.models.job import CadJob, JobState
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.result import PreviewArtifact
from cad_harness.domain.models.validation import Finding, Severity, ValidationReport

type FrozenJson = (
    str | int | float | bool | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"] | None
)


class DiffColor(StrEnum):
    """Required semantic-diff colors; these are not pass/fail statuses."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class OverlayColor(StrEnum):
    """Required preview color for a validation finding."""

    PURPLE = "purple"


@dataclass(frozen=True, slots=True)
class PreviewArtifactView:
    kind: str
    artifact_ref: str
    byte_size: int | None


@dataclass(frozen=True, slots=True)
class PreviewPane:
    """One explicitly labelled side of the before/after comparison."""

    label: str
    artifacts: tuple[PreviewArtifactView, ...]


@dataclass(frozen=True, slots=True)
class MissingInputView:
    path: str
    reason: str
    accepted_formats: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DefaultRecordView:
    path: str
    value: FrozenJson
    source: str
    source_version: str
    reason: str
    impact: str
    override_allowed: bool


@dataclass(frozen=True, slots=True)
class AssumptionView:
    path: str
    statement: str
    affects_geometry: bool
    requires_approval: bool


@dataclass(frozen=True, slots=True)
class DiffEntryView:
    change: str
    color: DiffColor
    feature_id: str
    operation_id: str
    entity_type: str
    layer: str
    summary: str
    target_entity_ref: str | None
    measurements: Mapping[str, FrozenJson]


@dataclass(frozen=True, slots=True)
class FindingView:
    rule_id: str
    severity: Severity
    message: str
    feature_id: str | None
    entity_ref: str | None
    operation_id: str | None
    expected: FrozenJson
    actual: FrozenJson
    tolerance: float | None
    suggested_fix: str | None
    measurement: Mapping[str, FrozenJson]


@dataclass(frozen=True, slots=True)
class ValidationOverlay:
    color: OverlayColor
    finding: FindingView


@dataclass(frozen=True, slots=True)
class ApprovalViewInputs:
    """Typed, already-read inputs. The builder never calls a service or CAD."""

    job: CadJob
    spec: DrawingSpec
    plan: OperationPlan | None
    current_revision: str | None = None
    missing_inputs: tuple[MissingInput, ...] = ()
    defaults_applied: tuple[DefaultRecord, ...] = ()
    assumptions: tuple[Assumption, ...] = ()
    before_artifacts: tuple[PreviewArtifact, ...] = ()
    after_artifacts: tuple[PreviewArtifact, ...] = ()
    semantic_diff: SemanticDiff | None = None
    validation_report: ValidationReport | None = None


@dataclass(frozen=True, slots=True)
class ApprovalViewModel:
    """Complete decision evidence, detached from mutable inputs and credentials."""

    document_id: str
    revision: str
    current_revision: str
    state: JobState
    plan_hash_prefix: str | None
    spec_parameters: Mapping[str, FrozenJson]
    missing_inputs: tuple[MissingInputView, ...]
    defaults_applied: tuple[DefaultRecordView, ...]
    assumptions: tuple[AssumptionView, ...]
    before_preview: PreviewPane
    after_preview: PreviewPane
    semantic_diff: tuple[DiffEntryView, ...]
    validation_overlays: tuple[ValidationOverlay, ...]
    findings: tuple[FindingView, ...]


def freeze_json(value: Any) -> FrozenJson:
    """Take an immutable snapshot of a JSON-compatible value."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"Approval evidence is not JSON-compatible: {type(value).__name__}")


def thaw_json(value: FrozenJson) -> Any:
    """Return ordinary JSON containers solely for rendering/serialization."""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def build_approval_view_model(inputs: ApprovalViewInputs) -> ApprovalViewModel:
    """Build a deterministic deep snapshot from typed values, with no side effects."""
    entries: tuple[DiffEntryView, ...] = ()
    if inputs.semantic_diff is not None:
        entries = tuple(
            DiffEntryView(
                change=entry.change,
                color=_diff_color(entry.change),
                feature_id=entry.feature_id,
                operation_id=entry.operation_id,
                entity_type=entry.entity_type,
                layer=entry.layer,
                summary=entry.summary,
                target_entity_ref=entry.target_entity_ref,
                measurements=_frozen_mapping(entry.measurements),
            )
            for entry in inputs.semantic_diff.entries
        )
    findings = tuple(_finding_view(finding) for finding in _findings(inputs))
    return ApprovalViewModel(
        document_id=inputs.job.document_id,
        revision=inputs.job.expected_revision,
        current_revision=inputs.current_revision or inputs.job.expected_revision,
        state=inputs.job.state,
        plan_hash_prefix=(
            hash_prefix(inputs.plan.plan_hash)
            if inputs.plan is not None and inputs.plan.plan_hash
            else None
        ),
        spec_parameters=_frozen_mapping(inputs.spec.model_dump(mode="json")),
        missing_inputs=tuple(
            MissingInputView(item.path, item.reason, item.accepted_formats)
            for item in inputs.missing_inputs
        ),
        defaults_applied=tuple(
            DefaultRecordView(
                item.path,
                freeze_json(item.value),
                item.source,
                item.source_version,
                item.reason,
                item.impact,
                item.override_allowed,
            )
            for item in inputs.defaults_applied
        ),
        assumptions=tuple(
            AssumptionView(
                item.path,
                item.statement,
                item.affects_geometry,
                item.requires_approval,
            )
            for item in inputs.assumptions
        ),
        before_preview=PreviewPane(
            label="before", artifacts=tuple(_artifact_view(x) for x in inputs.before_artifacts)
        ),
        after_preview=PreviewPane(
            label="after", artifacts=tuple(_artifact_view(x) for x in inputs.after_artifacts)
        ),
        semantic_diff=entries,
        validation_overlays=tuple(
            ValidationOverlay(color=OverlayColor.PURPLE, finding=finding) for finding in findings
        ),
        findings=findings,
    )


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, FrozenJson]:
    frozen = freeze_json(value)
    assert isinstance(frozen, Mapping)
    return frozen


def _findings(inputs: ApprovalViewInputs) -> tuple[Finding, ...]:
    return inputs.validation_report.findings if inputs.validation_report is not None else ()


def _finding_view(finding: Finding) -> FindingView:
    return FindingView(
        rule_id=finding.rule_id,
        severity=finding.severity,
        message=finding.message,
        feature_id=finding.feature_id,
        entity_ref=finding.entity_ref,
        operation_id=finding.operation_id,
        expected=freeze_json(finding.expected),
        actual=freeze_json(finding.actual),
        tolerance=finding.tolerance,
        suggested_fix=finding.suggested_fix,
        measurement=_frozen_mapping(finding.measurement),
    )


def _artifact_view(artifact: PreviewArtifact) -> PreviewArtifactView:
    return PreviewArtifactView(artifact.kind, artifact.artifact_ref, artifact.byte_size)


def _diff_color(change: str) -> DiffColor:
    """Map the three semantic changes to their documented, stable colors."""
    return {
        "added": DiffColor.GREEN,
        "modified": DiffColor.YELLOW,
        "deleted": DiffColor.RED,
    }[change]
