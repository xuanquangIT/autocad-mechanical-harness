"""Golden case runner. Compares semantics, never DWG bytes (ADR-006)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cad_harness.application.services.harness_service import HarnessService
from cad_harness.domain.models.validation import ValidationStage

CASES_DIR = Path(__file__).parent
CASE_NAMES = sorted(
    p.name for p in CASES_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")
)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


@pytest.fixture(params=CASE_NAMES)
def case(request: pytest.FixtureRequest) -> Path:
    return CASES_DIR / request.param


pytestmark = pytest.mark.golden


class TestGoldenCases:
    def test_plan_matches_expected_operations(self, case: Path, service: HarnessService) -> None:
        spec = load_json(case / "input_spec.json")
        expected = load_json(case / "expected_plan.json")

        job = service.create_job()
        result = service.submit_spec(job.job_id, spec)
        assert result["status"] == "ok", result

        plan = service.store.get_plan(job.job_id)
        assert plan is not None
        actual = [
            op.model_dump(mode="json", exclude_none=True, exclude={"target_entity_ref"})
            for op in plan.operations
        ]
        assert actual == expected["operations"]
        assert plan.profile_ref == expected["profile_ref"]
        assert plan.canonical_units.value == expected["canonical_units"]

    def test_validation_matches_expected_findings(
        self, case: Path, service: HarnessService
    ) -> None:
        spec = load_json(case / "input_spec.json")
        expected = load_json(case / "expected_validation.json")

        job = service.create_job()
        service.submit_spec(job.job_id, spec)
        report = service.validate(job.job_id, ValidationStage(expected["stage"]))

        assert report.blocking_count == expected["blocking_count"]
        assert report.error_count == expected["error_count"]
        assert report.warning_count == expected["warning_count"]
        assert report.gate_allows_commit() == expected["commit_allowed"]
        assert sorted((f.rule_id, f.severity.value) for f in report.findings) == sorted(
            (f["rule_id"], f["severity"]) for f in expected["findings"]
        )

    def test_committed_entities_match_expected_measurements(
        self, case: Path, service: HarnessService
    ) -> None:
        spec = load_json(case / "input_spec.json")
        expected = load_json(case / "expected_semantic_entities.json")

        job = service.create_job()
        submitted = service.submit_spec(job.job_id, spec)
        service.preview(job.job_id)
        report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)
        acknowledged = tuple(f.rule_id for f in report.findings)
        _, token = service.approve(job.job_id, "golden-runner", acknowledged)

        result = service.commit(
            job.job_id,
            idempotency_key=f"golden-{case.name}",
            expected_revision=job.expected_revision,
            plan_hash=str(submitted["plan_hash"]),
            approval_token=token,
        )

        assert len(result.entity_results) == expected["entity_count"]
        for actual_entity, expected_entity in zip(
            result.entity_results, expected["entities"], strict=True
        ):
            assert actual_entity.operation_id == expected_entity["operation_id"]
            assert actual_entity.feature_id == expected_entity["feature_id"]
            assert actual_entity.entity_type == expected_entity["entity_type"]
            for key, value in expected_entity["measurements"].items():
                assert actual_entity.measurements[key] == pytest.approx(value)

    def test_plan_hash_is_reproducible(self, case: Path, service: HarnessService) -> None:
        """Two compiles of the same spec must agree, or approvals cannot be trusted."""
        spec = load_json(case / "input_spec.json")
        first = service.submit_spec(service.create_job().job_id, spec)
        second = service.submit_spec(service.create_job().job_id, spec)
        assert first["plan_hash"] == second["plan_hash"]
