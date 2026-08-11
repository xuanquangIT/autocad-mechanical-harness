"""Fail-closed verifier for the engineer-reviewed production golden corpus.

The golden runner proves deterministic behavior.  This verifier proves that the
corpus used to make a *production* claim is real, reviewed, and independent of
the runner that produced the synthetic development fixtures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

MIN_CASES = 30
MAX_CASES = 50
MIN_TAKEOFF_CASES = 5
_SAFE_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_BASE_ARTIFACTS = (
    "input_spec",
    "company_profile",
    "expected_plan",
    "expected_semantic_entities",
    "expected_validation",
    "preview_reference",
)
_TAKEOFF_ARTIFACTS = ("input_drawing", "expected_takeoff")


def _error(code: str, field: str, case_id: str | None = None) -> dict[str, str]:
    result = {"code": code, "field": field}
    if case_id is not None:
        result["case_id"] = case_id
    return result


def _safe_case_id(value: object, index: int) -> str:
    if isinstance(value, str) and _SAFE_CASE_ID.fullmatch(value):
        return value
    return f"case-{index + 1:03d}"


def _non_empty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _load_mapping(path: Path) -> Mapping[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
        value = (
            yaml.safe_load(text)
            if path.suffix.casefold() in {".yaml", ".yml"}
            else json.loads(text)
        )
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return None
    return value if isinstance(value, Mapping) else None


def _artifact_path(root: Path, reference: object) -> Path | None:
    if not _non_empty(reference):
        return None
    raw = str(reference).replace("\\", "/")
    relative = Path(raw)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        return None
    try:
        resolved = (root / relative).resolve(strict=True)
        corpus_root = root.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_file() and resolved.is_relative_to(corpus_root) else None


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _validate_review(case: Mapping[str, Any], case_id: str) -> list[dict[str, str]]:
    review = case.get("review")
    if not isinstance(review, Mapping):
        return [_error("REVIEW_MISSING", "review", case_id)]
    errors = []
    for field in ("reviewer_identity", "evidence_ref"):
        if not _non_empty(review.get(field)):
            errors.append(_error("REVIEW_EVIDENCE_MISSING", f"review.{field}", case_id))
    return errors


def _validate_source(
    case: Mapping[str, Any], artifacts: Mapping[str, Any], root: Path, case_id: str
) -> list[dict[str, str]]:
    source = case.get("source_drawing")
    if not isinstance(source, Mapping):
        return [_error("SOURCE_PROVENANCE_MISSING", "source_drawing", case_id)]
    errors = []
    if source.get("synthetic") is not False:
        errors.append(_error("SOURCE_NOT_REAL", "source_drawing.synthetic", case_id))
    if not _non_empty(source.get("provenance_ref")):
        errors.append(_error("SOURCE_PROVENANCE_MISSING", "source_drawing.provenance_ref", case_id))
    expected_hash = source.get("sha256")
    if not isinstance(expected_hash, str) or _SHA256.fullmatch(expected_hash) is None:
        errors.append(_error("SOURCE_HASH_INVALID", "source_drawing.sha256", case_id))
        return errors
    source_path = _artifact_path(root, source.get("artifact_ref"))
    if source_path is None:
        errors.append(_error("SOURCE_ARTIFACT_MISSING", "source_drawing.artifact_ref", case_id))
        return errors
    if _sha256(source_path) != expected_hash.casefold():
        errors.append(_error("SOURCE_HASH_MISMATCH", "source_drawing.sha256", case_id))
    source_ref = source.get("artifact_ref")
    if artifacts.get("input_drawing") is not None and artifacts.get("input_drawing") != source_ref:
        errors.append(_error("SOURCE_REFERENCE_MISMATCH", "artifacts.input_drawing", case_id))
    return errors


def _validate_profile(
    artifacts: Mapping[str, Any], root: Path, case_id: str
) -> list[dict[str, str]]:
    profile_path = _artifact_path(root, artifacts.get("company_profile"))
    if profile_path is None:
        return []  # The generic artifact diagnostic already identifies this failure.
    profile = _load_mapping(profile_path)
    if profile is None:
        return [_error("COMPANY_PROFILE_INVALID", "artifacts.company_profile", case_id)]
    if profile.get("company_approved") is not True:
        return [_error("COMPANY_PROFILE_UNAPPROVED", "company_profile.company_approved", case_id)]
    return []


def _validate_takeoff(case: Mapping[str, Any], case_id: str) -> list[dict[str, str]]:
    takeoff = case.get("takeoff")
    if not isinstance(takeoff, Mapping):
        return [_error("TAKEOFF_EVIDENCE_MISSING", "takeoff", case_id)]
    errors = []
    for field in ("calculation_source_ref", "calculated_by", "reviewer_identity"):
        if not _non_empty(takeoff.get(field)):
            errors.append(_error("TAKEOFF_EVIDENCE_MISSING", f"takeoff.{field}", case_id))
    calculated_by = takeoff.get("calculated_by")
    reviewer = takeoff.get("reviewer_identity")
    if (
        _non_empty(calculated_by)
        and _non_empty(reviewer)
        and str(calculated_by).strip().casefold() == str(reviewer).strip().casefold()
    ):
        errors.append(_error("TAKEOFF_NOT_INDEPENDENT", "takeoff.reviewer_identity", case_id))
    material_table = takeoff.get("material_table")
    if not isinstance(material_table, Mapping):
        errors.append(_error("MATERIAL_TABLE_APPROVAL_MISSING", "takeoff.material_table", case_id))
    else:
        if not _non_empty(material_table.get("ref")):
            errors.append(
                _error("MATERIAL_TABLE_APPROVAL_MISSING", "takeoff.material_table.ref", case_id)
            )
        if material_table.get("company_approved") is not True:
            errors.append(
                _error(
                    "MATERIAL_TABLE_UNAPPROVED",
                    "takeoff.material_table.company_approved",
                    case_id,
                )
            )
    return errors


def verify_production_golden_acceptance(manifest_path: Path) -> dict[str, Any]:
    """Return a deterministic redacted acceptance summary for *manifest_path*."""
    manifest = _load_mapping(manifest_path)
    if manifest is None:
        return {
            "passed": False,
            "case_count": 0,
            "takeoff_case_count": 0,
            "errors": [_error("MANIFEST_UNREADABLE", "manifest")],
        }

    raw_cases = manifest.get("cases")
    cases = raw_cases if isinstance(raw_cases, list) else []
    errors: list[dict[str, str]] = []
    if manifest.get("schema_version") != "1.0":
        errors.append(_error("MANIFEST_SCHEMA_UNSUPPORTED", "schema_version"))
    if not isinstance(raw_cases, list):
        errors.append(_error("CASES_MISSING", "cases"))
    if not MIN_CASES <= len(cases) <= MAX_CASES:
        errors.append(_error("CASE_COUNT_OUT_OF_RANGE", "cases"))

    root = manifest_path.parent
    takeoff_count = 0
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(cases):
        case_id = _safe_case_id(
            raw_case.get("case_id") if isinstance(raw_case, Mapping) else None, index
        )
        if not isinstance(raw_case, Mapping):
            errors.append(_error("CASE_INVALID", "case", case_id))
            continue
        if raw_case.get("case_id") != case_id:
            errors.append(_error("CASE_ID_INVALID", "case_id", case_id))
        elif case_id in seen_ids:
            errors.append(_error("CASE_ID_DUPLICATE", "case_id", case_id))
        seen_ids.add(case_id)
        if raw_case.get("engineer_selected") is not True:
            errors.append(_error("ENGINEER_SELECTION_MISSING", "engineer_selected", case_id))

        artifacts = raw_case.get("artifacts")
        if not isinstance(artifacts, Mapping):
            errors.append(_error("ARTIFACTS_MISSING", "artifacts", case_id))
            artifacts = {}
        for field in _BASE_ARTIFACTS:
            if _artifact_path(root, artifacts.get(field)) is None:
                errors.append(_error("ARTIFACT_MISSING", f"artifacts.{field}", case_id))

        is_takeoff = raw_case.get("case_type") == "takeoff"
        if is_takeoff:
            takeoff_count += 1
            for field in _TAKEOFF_ARTIFACTS:
                if _artifact_path(root, artifacts.get(field)) is None:
                    errors.append(_error("ARTIFACT_MISSING", f"artifacts.{field}", case_id))
            errors.extend(_validate_takeoff(raw_case, case_id))
        elif raw_case.get("case_type") != "design":
            errors.append(_error("CASE_TYPE_INVALID", "case_type", case_id))

        errors.extend(_validate_review(raw_case, case_id))
        errors.extend(_validate_source(raw_case, artifacts, root, case_id))
        errors.extend(_validate_profile(artifacts, root, case_id))

    if takeoff_count < MIN_TAKEOFF_CASES:
        errors.append(_error("TAKEOFF_CASE_COUNT_TOO_LOW", "cases"))
    errors.sort(key=lambda item: (item.get("case_id", ""), item["code"], item["field"]))
    return {
        "passed": not errors,
        "case_count": len(cases),
        "takeoff_case_count": takeoff_count,
        "errors": errors,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=Path("tests/golden_drawings/production_manifest.json"),
        help="production corpus manifest (path is never emitted in the summary)",
    )
    args = parser.parse_args(argv)
    summary = verify_production_golden_acceptance(args.manifest)
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
