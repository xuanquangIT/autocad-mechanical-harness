"""Local-only effectiveness metrics for the measured pilot programme."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel

type CapabilityGroup = Literal["B", "D", "E"]
type WorkLabel = Literal["ve_moi", "sua_ban_co_san"]

_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


class FailureReason(StrEnum):
    """Finite classifications for cases below the configured saving floor."""

    UNSUPPORTED_FEATURE = "unsupported_feature"
    VALIDATION_BLOCKED = "validation_blocked"
    ADAPTER_FAILURE = "adapter_failure"
    TIMEOUT = "timeout"
    ENGINEER_REJECTED = "engineer_rejected"
    UNKNOWN_COMMIT_STATE = "unknown_commit_state"
    EXCESSIVE_SPEC_ITERATIONS = "excessive_spec_iterations"
    MANUAL_FIXUP = "manual_fixup"
    WORKFLOW_OVERHEAD = "workflow_overhead"
    MISSING_EFFORT_RECORD = "missing_effort_record"


class BaselineCase(ContractModel):
    pilot_run_id: str = Field(default="pilot_default", pattern=_OPAQUE_ID_PATTERN)
    case_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    capability_group: CapabilityGroup
    work_label: WorkLabel
    manual_minutes: float = Field(ge=0.0, allow_inf_nan=False)
    manual_measured_by: str = Field(pattern=_OPAQUE_ID_PATTERN)
    manual_measurement_biased: bool = False
    manual_measured_in_single_session: bool

    @field_validator("manual_minutes")
    @classmethod
    def _manual_minutes_are_stored_at_required_precision(cls, value: float) -> float:
        if round_minutes(value) != value:
            raise ValueError("manual_minutes must already be rounded to 0.1 minute")
        return value


class EffortRecord(ContractModel):
    pilot_run_id: str = Field(default="pilot_default", pattern=_OPAQUE_ID_PATTERN)
    record_id: str | None = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    case_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    job_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    harness_minutes: float = Field(ge=0.0, allow_inf_nan=False)
    idle_minutes_excluded: float = Field(ge=0.0, allow_inf_nan=False)
    manual_fixup_minutes: float = Field(ge=0.0, allow_inf_nan=False)
    spec_change_count: int = Field(ge=0)
    entities_created: int = Field(ge=0)
    entities_manually_edited: int = Field(ge=0)
    first_preview_clean: bool
    completed: bool
    failure_reason: FailureReason | None = None

    @field_validator("harness_minutes", "idle_minutes_excluded", "manual_fixup_minutes")
    @classmethod
    def _round_effort_minutes(cls, value: float) -> float:
        return round_minutes(value)

    @model_validator(mode="after")
    def _completion_and_failure_reason_are_consistent(self) -> EffortRecord:
        if self.completed and self.failure_reason is not None:
            raise ValueError("A completed effort record cannot carry a failure reason")
        if not self.completed and self.failure_reason is None:
            raise ValueError("An incomplete effort record requires a classified failure reason")
        return self


class Metric(ContractModel):
    """One aggregate together with the denominator and usability label."""

    name: str = ""
    value: float | None = Field(allow_inf_nan=False)
    sample_count: int = Field(ge=0)
    insufficient_sample: bool


class PilotCaseResult(ContractModel):
    case_id: str
    capability_group: CapabilityGroup
    work_label: WorkLabel
    manual_minutes: float
    harness_minutes: float | None
    saving: float
    completed: bool
    effort_record_present: bool
    failure_reason: FailureReason | None = None


class WorkLabelSummary(ContractModel):
    work_label: WorkLabel
    case_ids: tuple[str, ...]
    median_saving: Metric


class OperationMetricSummary(ContractModel):
    operation_name: Literal["compile", "preview", "commit", "read", "takeoff", "measure"]
    median_ms: Metric
    p95_ms: Metric


class PilotReport(ContractModel):
    schema_version: str = SCHEMA_VERSION
    pilot_run_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    report_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    baseline_valid: bool
    baseline_case_count: int = Field(ge=0)
    cases: tuple[PilotCaseResult, ...]
    overall_saving: Metric
    group_savings: tuple[Metric, ...]
    work_label_summaries: tuple[WorkLabelSummary, ...]
    first_preview_clean_rate: Metric
    median_spec_changes: Metric
    manual_entity_edit_rate: Metric
    committed_job_rate: Metric
    operation_metrics: tuple[OperationMetricSummary, ...] = ()
    biased_case_ids: tuple[str, ...] = ()
    goal_met: bool
    quality_gates_met: bool
    quality_gate_failures: tuple[str, ...] = ()
    pilot_acceptance_met: bool


def round_minutes(value: float) -> float:
    """Round elapsed minutes to the required one decimal place."""
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def round_saving(value: float) -> float:
    """Round saving ratios to two decimals while preserving negative values."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


__all__ = [
    "BaselineCase",
    "CapabilityGroup",
    "EffortRecord",
    "FailureReason",
    "Metric",
    "OperationMetricSummary",
    "PilotCaseResult",
    "PilotReport",
    "WorkLabel",
    "WorkLabelSummary",
    "round_minutes",
    "round_saving",
]
