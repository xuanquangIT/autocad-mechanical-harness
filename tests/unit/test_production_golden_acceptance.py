"""Production golden-corpus evidence must be real, reviewed, and independent."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from scripts.check_production_golden_acceptance import main, verify_production_golden_acceptance


def _write_case(root: Path, index: int, *, takeoff: bool) -> dict[str, object]:
    case_id = f"case-{index:02d}"
    case_dir = root / case_id
    case_dir.mkdir()
    source = f"deidentified engineering drawing {index}".encode()
    (case_dir / "source.dxf").write_bytes(source)
    files = {
        "input_spec": "input_spec.json",
        "company_profile": "company_profile.yaml",
        "expected_plan": "expected_plan.json",
        "expected_semantic_entities": "expected_semantic_entities.json",
        "expected_validation": "expected_validation.json",
        "preview_reference": "preview_reference.svg",
    }
    for name in files.values():
        path = case_dir / name
        if path.suffix == ".yaml":
            path.write_text(yaml.safe_dump({"company_approved": True}), encoding="utf-8")
        elif path.suffix == ".svg":
            path.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        else:
            path.write_text("{}", encoding="utf-8")
    artifacts = {key: f"{case_id}/{value}" for key, value in files.items()}
    result: dict[str, object] = {
        "case_id": case_id,
        "case_type": "takeoff" if takeoff else "design",
        "engineer_selected": True,
        "artifacts": artifacts,
        "review": {
            "reviewer_identity": f"engineer-reviewer-{index}",
            "evidence_ref": f"review-record-{index}",
        },
        "source_drawing": {
            "artifact_ref": f"{case_id}/source.dxf",
            "provenance_ref": f"controlled-source-record-{index}",
            "sha256": hashlib.sha256(source).hexdigest(),
            "synthetic": False,
        },
    }
    if takeoff:
        (case_dir / "expected_takeoff.json").write_text("{}", encoding="utf-8")
        artifacts["input_drawing"] = f"{case_id}/source.dxf"
        artifacts["expected_takeoff"] = f"{case_id}/expected_takeoff.json"
        result["takeoff"] = {
            "calculation_source_ref": f"independent-calculation-{index}",
            "calculated_by": f"calculator-{index}",
            "reviewer_identity": f"takeoff-reviewer-{index}",
            "material_table": {"ref": "approved-material-table@2", "company_approved": True},
        }
    return result


def _manifest(tmp_path: Path) -> Path:
    cases = [_write_case(tmp_path, index, takeoff=index < 5) for index in range(30)]
    path = tmp_path / "production_manifest.json"
    path.write_text(json.dumps({"schema_version": "1.0", "cases": cases}), encoding="utf-8")
    return path


def test_valid_minimum_production_corpus_passes(tmp_path: Path) -> None:
    summary = verify_production_golden_acceptance(_manifest(tmp_path))

    assert summary == {
        "passed": True,
        "case_count": 30,
        "takeoff_case_count": 5,
        "errors": [],
    }


def test_missing_manifest_fails_closed_without_logging_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "customer-secret" / "production.json"

    assert main([str(missing)]) == 1
    output = capsys.readouterr().out
    assert str(missing) not in output
    assert json.loads(output)["errors"] == [{"code": "MANIFEST_UNREADABLE", "field": "manifest"}]


def test_current_synthetic_corpus_cannot_be_claimed_as_production() -> None:
    manifest = Path("tests/golden_drawings/production_manifest.json")

    summary = verify_production_golden_acceptance(manifest)

    assert summary["passed"] is False
    assert any(error["code"] == "MANIFEST_UNREADABLE" for error in summary["errors"])


def test_unapproved_company_profile_fails(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    profile = tmp_path / "case-07" / "company_profile.yaml"
    profile.write_text("company_approved: false\n", encoding="utf-8")

    summary = verify_production_golden_acceptance(manifest)

    assert summary["passed"] is False
    assert {
        "code": "COMPANY_PROFILE_UNAPPROVED",
        "field": "company_profile.company_approved",
        "case_id": "case-07",
    } in summary["errors"]


def test_takeoff_calculator_cannot_review_own_result(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["cases"][0]["takeoff"]["reviewer_identity"] = "calculator-0"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    summary = verify_production_golden_acceptance(manifest)

    assert summary["passed"] is False
    assert {
        "code": "TAKEOFF_NOT_INDEPENDENT",
        "field": "takeoff.reviewer_identity",
        "case_id": "case-00",
    } in summary["errors"]
