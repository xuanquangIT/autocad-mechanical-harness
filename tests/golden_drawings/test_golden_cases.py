"""Golden case runner. Compares semantics, never DWG bytes (ADR-006)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from cad_harness.application.services.harness_service import HarnessService
from cad_harness.domain.errors import HarnessError
from cad_harness.domain.models.validation import ValidationStage
from cad_harness.golden_comparison import compare_semantic_entities, compare_takeoff_reports

CASES_DIR = Path(__file__).parent
POSITIVE_CASES = [
    path for path in CASES_DIR.iterdir() if path.is_dir() and not path.name.startswith("_")
]
NEGATIVE_CASES = [path for path in (CASES_DIR / "_negative").iterdir() if path.is_dir()]
CASE_PATHS = sorted((*POSITIVE_CASES, *NEGATIVE_CASES), key=lambda path: path.name)
if selected_case := os.environ.get("CAD_HARNESS_GOLDEN_CASE"):
    CASE_PATHS = [path for path in CASE_PATHS if path.name == selected_case]


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def expected_error(case: Path) -> dict[str, Any] | None:
    path = case / "expected_error.json"
    return load_json(path) if path.is_file() else None


def assert_expected_error(case: Path, service: HarnessService, spec: dict[str, Any]) -> bool:
    expected = expected_error(case)
    if expected is None:
        return False
    with pytest.raises(HarnessError) as caught:
        service.submit_spec(service.create_job().job_id, spec)
    assert caught.value.code.value == expected["error_code"]
    return True


@pytest.fixture(params=CASE_PATHS, ids=lambda path: path.name)
def case(request: pytest.FixtureRequest) -> Path:
    assert isinstance(request.param, Path)
    return request.param


pytestmark = pytest.mark.golden


class TestGoldenCases:
    def test_case_has_profile_and_reference_preview(self, case: Path) -> None:
        if expected_error(case) is not None:
            return
        profile = case / "company_profile.yaml"
        preview = case / "preview_reference.svg"
        assert profile.is_file(), f"{case.name}: missing company_profile.yaml"
        assert preview.is_file(), f"{case.name}: missing preview_reference.svg"
        assert "<svg" in preview.read_text(encoding="utf-8")

    def test_plan_matches_expected_operations(self, case: Path, service: HarnessService) -> None:
        spec = load_json(case / "input_spec.json")
        if assert_expected_error(case, service, spec):
            return
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
        if assert_expected_error(case, service, spec):
            return
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
        if assert_expected_error(case, service, spec):
            return
        expected_validation = load_json(case / "expected_validation.json")

        job = service.create_job()
        submitted = service.submit_spec(job.job_id, spec)
        service.preview(job.job_id)
        report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)
        if not expected_validation["commit_allowed"]:
            assert not report.gate_allows_commit()
            assert len(service.adapter.document.entities) == 0
            return
        expected = load_json(case / "expected_semantic_entities.json")
        acknowledged = tuple(f.rule_id for f in report.findings)
        _, token = service.approve(job.job_id, "golden-runner", acknowledged)

        result = service.commit(
            job.job_id,
            idempotency_key=f"golden-{case.name}",
            expected_revision=job.expected_revision,
            plan_hash=str(submitted["plan_hash"]),
            approval_token=token,
        )

        plan = service.store.get_plan(job.job_id)
        assert plan is not None
        operations = {operation.operation_id: operation for operation in plan.operations}
        actual_entities = []
        for entity in result.entity_results:
            operation = operations[entity.operation_id]
            payload: dict[str, Any] = {
                "operation_id": entity.operation_id,
                "feature_id": entity.feature_id,
                "entity_type": entity.entity_type,
                "layer": operation.layer,
                "measurements": entity.measurements,
            }
            style: dict[str, str] = {}
            if "dimstyle" in operation.geometry:
                style["dimension_style"] = str(operation.geometry["dimstyle"])
            if "textstyle" in operation.geometry:
                style["text_style"] = str(operation.geometry["textstyle"])
            if style:
                payload["style"] = style
            actual_entities.append(payload)
        compare_semantic_entities(
            expected,
            {"entities": actual_entities},
        ).assert_matches()

    def test_optional_dxf_takeoff_matches_semantically(self, case: Path) -> None:
        drawing = case / "input_drawing.dxf"
        request_path = case / "takeoff_request.json"
        expected_path = case / "expected_takeoff.json"
        if not any(path.exists() for path in (drawing, request_path, expected_path)):
            return
        assert all(path.is_file() for path in (drawing, request_path, expected_path))

        from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
        from cad_harness.company_rules.material_loader import load_material_table
        from cad_harness.comprehension.takeoff import compute_takeoff
        from cad_harness.domain.models.drawing_model import ReadScope
        from cad_harness.domain.models.takeoff import TakeoffRequest
        from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef
        from cad_harness.geometry.tolerance import DEMO_TOLERANCE

        model = DxfDrawingReader(DEMO_TOLERANCE).read(
            DrawingReadRequest(
                source=DrawingSourceRef(kind="file", format="dxf", ref=str(drawing)),
                scope=ReadScope(kind="model_space"),
                max_entities=20_000,
                max_block_nesting_depth=10,
            )
        )
        raw_request = load_json(request_path)
        raw_request["document_id"] = model.document_id
        request = TakeoffRequest.model_validate(raw_request)
        report = compute_takeoff(
            model,
            request,
            materials=load_material_table(request.material_profile_ref),
            tolerance=DEMO_TOLERANCE,
        )
        expected = load_json(expected_path)
        # File/path-derived identity and revision legitimately differ between regenerations.
        expected["document_id"] = report.document_id
        expected["revision"] = report.revision
        compare_takeoff_reports(expected, report).assert_matches()

    def test_plan_hash_is_reproducible(self, case: Path, service: HarnessService) -> None:
        """Two compiles of the same spec must agree, or approvals cannot be trusted."""
        spec = load_json(case / "input_spec.json")
        if expected_error(case) is not None:
            assert_expected_error(case, service, spec)
            assert_expected_error(case, service, spec)
            return
        first = service.submit_spec(service.create_job().job_id, spec)
        second = service.submit_spec(service.create_job().job_id, spec)
        assert first["plan_hash"] == second["plan_hash"]
