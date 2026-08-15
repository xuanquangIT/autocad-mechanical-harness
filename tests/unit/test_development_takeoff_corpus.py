"""Development take-off corpus evaluator tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts import evaluate_development_takeoff_corpus as evaluator


@pytest.fixture(scope="module")
def report() -> dict[str, object]:
    return evaluator.evaluate_development_takeoff_corpus()


def test_five_real_dxf_reads_match_the_independent_oracle(report: dict[str, object]) -> None:
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary == {
        "case_count": 5,
        "oracle_match_count": 5,
        "deterministic_repeat_count": 5,
    }
    assert report["production_dxf_reader_exercised"] is True
    assert report["production_takeoff_engine_exercised"] is True
    assert report["independent_formula_oracle"] is True


def test_report_never_makes_human_or_company_claims(report: dict[str, object]) -> None:
    assert report["production_evidence"] is False
    assert report["production_acceptance_eligible"] is False
    assert report["engineer_selected"] is False
    assert report["independently_human_reviewed"] is False
    assert report["company_approved"] is False
    material = report["material_reference"]
    assert isinstance(material, dict)
    assert material["company_approved"] is False
    assert material["classification"] == "development_demo_only"


def test_case_metrics_cover_plain_round_rectangular_and_mixed_parts(
    report: dict[str, object],
) -> None:
    cases = report["cases"]
    assert isinstance(cases, list)
    by_id = {str(case["case_id"]): case for case in cases}
    assert set(by_id) == {
        "plate-plain",
        "plate-one-round-hole",
        "plate-two-round-holes",
        "plate-rectangular-cutout",
        "plate-mixed-cutouts",
    }
    assert by_id["plate-plain"]["inner_contour_count"] == 0
    assert by_id["plate-two-round-holes"]["inner_contour_count"] == 2
    assert by_id["plate-rectangular-cutout"]["inner_contour_count"] == 1
    assert by_id["plate-mixed-cutouts"]["inner_contour_count"] == 3


def test_report_is_canonical_and_repeatable(report: dict[str, object]) -> None:
    repeated = evaluator.evaluate_development_takeoff_corpus()
    assert repeated == report
    rendered = evaluator.render_evaluation(report)
    assert rendered.endswith("\n")
    assert json.loads(rendered) == report


def test_output_requires_allowlisted_root_and_never_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert evaluator.main(["--output", "report.json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "OUTPUT_ALLOWLIST_REQUIRED"

    assert evaluator.main(["--output", "report.json", "--output-root", str(tmp_path)]) == 0
    target = tmp_path / "report.json"
    assert json.loads(target.read_text(encoding="utf-8"))["summary"]["case_count"] == 5
    assert evaluator.main(["--output", "report.json", "--output-root", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "OUTPUT_ALREADY_EXISTS"


def test_material_density_drift_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    original = evaluator._load_development_material_table()
    changed = original.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(update={"density_kg_per_m3": 7000.0})
                if entry.material_code == evaluator.MATERIAL_CODE
                else entry
                for entry in original.entries
            )
        }
    )
    monkeypatch.setattr(evaluator, "_load_development_material_table", lambda: changed)
    with pytest.raises(
        evaluator.DevelopmentTakeoffEvaluationError,
        match="DEVELOPMENT_MATERIAL_DENSITY_DRIFT",
    ):
        evaluator.evaluate_development_takeoff_corpus()
