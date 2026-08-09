# Feature: cad-ai-production-roadmap, Property 9: Mọi OperationType được ánh xạ hoặc khai báo thiếu và preflight chặn preview

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.adapters.base import BaseAdapter
from cad_harness.application.services.plan_compiler import PlanCompilerService
from cad_harness.domain.errors import AdapterCapabilityMissingError
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType


def _plan(types: list[OperationType]) -> OperationPlan:
    return OperationPlan(
        plan_id="plan-property-9",
        job_id="job-property-9",
        document_id="doc-property-9",
        expected_revision="sha256:revision",
        profile_ref="profile@1",
        operations=tuple(
            Operation(operation_id=f"op-{index}", feature_id="feature", type=kind, layer="OBJECT")
            for index, kind in enumerate(types)
        ),
    )


@given(
    plan_types=st.lists(st.sampled_from(list(OperationType)), unique=True),
    supported=st.sets(st.sampled_from(list(OperationType))),
)
@settings(max_examples=100, deadline=None)
def test_operation_mapping_or_explicit_gap_blocks_before_preview(
    plan_types: list[OperationType], supported: set[OperationType]
) -> None:
    """**Validates: Requirements 4.1, 4.3, 4.4**"""

    class RecordingAdapter(BaseAdapter):
        adapter_type = "recording"

        def __init__(self) -> None:
            self.supported_operations = frozenset(supported)
            self.preview_calls = 0

        def preview(self, plan: OperationPlan):
            self.preview_calls += 1
            return None

    adapter = RecordingAdapter()
    plan = _plan(plan_types)
    expected = set(plan_types) - supported
    assert set(adapter.unsupported_operations(plan)) == expected

    compiler = PlanCompilerService(None, None, adapter)  # type: ignore[arg-type]
    if expected:
        with pytest.raises(AdapterCapabilityMissingError) as captured:
            compiler.preflight(plan)
        assert set(captured.value.details["missing_operations"]) == {
            item.value for item in expected
        }
        assert adapter.preview_calls == 0
    else:
        compiler.preflight(plan)
        adapter.preview(plan)
        assert adapter.preview_calls == 1
