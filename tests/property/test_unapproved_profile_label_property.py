"""Property 28: demo-profile provenance is propagated to reports and previews."""

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.company_rules.loader import load_profile
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.validation import ValidationStage
from cad_harness.validation.engine import RuleContext, default_engine


# Feature: cad-ai-production-roadmap, Property 28: Kết quả dựa trên profile chưa được công ty phê duyệt luôn được gắn nhãn
@given(suffix=st.text(alphabet="abcdef0123456789", min_size=1, max_size=12))
@settings(max_examples=100, deadline=None)
def test_unapproved_profile_always_labels_reports_and_previews(suffix: str) -> None:
    """**Validates: Requirements 10.6, 15.6**"""
    profile = load_profile("demo-profile").model_copy(update={"company_approved": False})
    plan = OperationPlan(
        plan_id=f"p-{suffix}",
        job_id=f"j-{suffix}",
        document_id="d",
        expected_revision="r",
        profile_ref=profile.as_ref(),
    ).with_hash()
    report = default_engine().run(
        ValidationStage.PLAN,
        RuleContext(profile=profile, tolerance=profile.tolerance(), plan=plan),
        job_id=plan.job_id,
    )
    preview = FakeAutoCADAdapter().preview(plan)
    assert report.company_approved is False
    assert preview.company_approved is False
