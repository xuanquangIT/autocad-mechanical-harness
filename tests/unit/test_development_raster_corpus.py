from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import cv2
import ezdxf
import numpy as np
import pytest
from scripts.evaluate_development_raster_corpus import (
    DevelopmentRasterEvaluationError,
    evaluate_development_raster_corpus,
    render_evaluation,
)


def _png() -> bytes:
    image = np.full((180, 220), 255, dtype=np.uint8)
    cv2.line(image, (20, 30), (100, 30), 0, 2, cv2.LINE_8)
    cv2.circle(image, (160, 80), 25, 0, 2, cv2.LINE_8)
    success, encoded = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    assert success
    return encoded.tobytes()


def _dxf() -> bytes:
    document = ezdxf.new("R2010")
    document.units = 4
    modelspace = document.modelspace()
    modelspace.add_line((0.0, 0.0), (80.0, 0.0))
    modelspace.add_circle((140.0, 50.0), radius=25.0)
    stream = __import__("io").StringIO()
    document.write(stream)
    return stream.getvalue().encode("utf-8")


def _write_lock(root: Path, artifacts: list[tuple[str, str, bytes]]) -> str:
    entries: list[dict[str, Any]] = []
    for source_id, relative_name, payload in artifacts:
        target = root.joinpath(*relative_name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        entries.append(
            {
                "source": {
                    "source_id": source_id,
                    "output": relative_name,
                    "max_bytes": 64 * 1024 * 1024,
                },
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    lock = {
        "schema_version": "1.0",
        "manifest_sha256": "a" * 64,
        "manifest": {
            "production_evidence": False,
            "customer_inputs_allowed": False,
        },
        "source_count": len(entries),
        "sources": entries,
    }
    lock_bytes = (json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (root / "development-corpus.lock.json").write_bytes(lock_bytes)
    return f"sha256:{hashlib.sha256(lock_bytes).hexdigest()}"


def _base_manifest(lock_digest: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "manifest_kind": "development_raster_evaluation",
        "production_evidence": False,
        "customer_inputs_allowed": False,
        "corpus_lock_sha256": lock_digest,
        "tracer": {
            "confidence_threshold": 0.72,
            "limits": {
                "max_bytes": 2 * 1024 * 1024,
                "max_pixels": 1_000_000,
                "max_dimension_px": 2_000,
                "max_reference_dxf_bytes": 2 * 1024 * 1024,
                "max_reference_entities": 1_000,
            },
        },
        "cases": [
            {
                "case_id": "case-0001",
                "image": {"kind": "locked", "source_id": "public-raster"},
                "calibration": {
                    "pixel_a": {"x": 10.0, "y": 170.0},
                    "pixel_b": {"x": 210.0, "y": 170.0},
                    "reference_distance_mm": 200.0,
                    "origin_mm": [0.0, 0.0],
                },
                "calibration_evidence": {
                    "kind": "explicit_control_points",
                    "evidence_id": "control-0001",
                },
                "reference": {
                    "kind": "locked_dxf",
                    "source_id": "public-vector",
                    "millimetres_per_unit": 1.0,
                    "maximum_size_error_mm": 3.0,
                },
                "variants": [
                    {"variant_id": "original", "kind": "original"},
                    {
                        "variant_id": "noise-0001",
                        "kind": "gaussian_noise",
                        "seed": 37,
                        "sigma": 2.0,
                    },
                    {
                        "variant_id": "blur-0001",
                        "kind": "gaussian_blur",
                        "kernel_size": 3,
                    },
                ],
            }
        ],
    }


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _paired_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], bytes]:
    root = tmp_path / "public"
    root.mkdir()
    image = _png()
    digest = _write_lock(
        root,
        [
            ("public-raster", "public/source.png", image),
            ("public-vector", "public/reference.dxf", _dxf()),
        ],
    )
    manifest = _base_manifest(digest)
    manifest_path = tmp_path / "evaluation.json"
    _write_manifest(manifest_path, manifest)
    return root, manifest_path, manifest, image


def test_hash_locked_pair_and_noisy_variants_are_canonical_and_redacted(tmp_path: Path) -> None:
    root, manifest_path, _, source_image = _paired_fixture(tmp_path)

    first = evaluate_development_raster_corpus(root, manifest_path)
    second = evaluate_development_raster_corpus(root, manifest_path)
    rendered = render_evaluation(first)

    assert first == second
    assert rendered == render_evaluation(second)
    assert first["production_evidence"] is False
    assert first["production_acceptance_eligible"] is False
    assert first["engineer_reviewed"] is False
    assert first["company_approved"] is False
    assert first["customer_data_used"] is False
    assert first["summary"]["case_count"] == 1
    assert first["summary"]["variant_count"] == 3
    assert first["summary"]["deterministic_repeat_count"] == 3
    assert first["summary"]["geometric_accuracy_measured"] is False
    case = first["cases"][0]
    assert case["source_sha256"] == f"sha256:{hashlib.sha256(source_image).hexdigest()}"
    assert {item["variant_kind"] for item in case["variants"]} == {
        "original",
        "gaussian_noise",
        "gaussian_blur",
    }
    assert all(item["deterministic_repeat"] for item in case["variants"])
    original = next(item for item in case["variants"] if item["variant_kind"] == "original")
    comparison = original["reference_comparison"]
    assert comparison["available"] is True
    assert comparison["diagnostic_only"] is True
    assert comparison["geometric_accuracy_measured"] is False
    assert comparison["reference_primitive_count"] == 2
    assert comparison["candidate_size_comparison_count"] >= 1
    assert "source.png" not in rendered
    assert "reference.dxf" not in rendered
    assert str(tmp_path) not in rendered
    assert "public-raster" not in rendered
    assert "public-vector" not in rendered


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing_lock", "CORPUS_LOCK_UNREADABLE"),
        ("missing_calibration", "CALIBRATION_REQUIRED"),
        ("source_hash_mismatch", "LOCKED_SOURCE_HASH_MISMATCH"),
        ("reference_unit_mismatch", "REFERENCE_UNIT_MISMATCH"),
    ],
)
def test_missing_or_changed_evidence_fails_closed(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    root, manifest_path, manifest, _ = _paired_fixture(tmp_path)
    if mutation == "missing_lock":
        (root / "development-corpus.lock.json").unlink()
    elif mutation == "missing_calibration":
        manifest["cases"][0]["calibration"] = None
        _write_manifest(manifest_path, manifest)
    elif mutation == "source_hash_mismatch":
        (root / "public" / "source.png").write_bytes(_png() + b"tampered")
    else:
        manifest["cases"][0]["reference"]["millimetres_per_unit"] = 25.4
        _write_manifest(manifest_path, manifest)

    with pytest.raises(DevelopmentRasterEvaluationError) as caught:
        evaluate_development_raster_corpus(root, manifest_path)

    assert caught.value.code == expected_code
    assert str(tmp_path) not in str(caught.value)


def test_explicitly_hash_bound_scan_derivative_is_observation_only(tmp_path: Path) -> None:
    root = tmp_path / "public"
    root.mkdir()
    lock_digest = _write_lock(
        root,
        [("public-book", "public-domain/source.pdf", b"%PDF-1.4\n%%EOF\n")],
    )
    scan = _png()
    scan_path = root / "scans" / "page-049.png"
    scan_path.parent.mkdir()
    scan_path.write_bytes(scan)
    manifest = _base_manifest(lock_digest)
    case = manifest["cases"][0]
    case["image"] = {
        "kind": "derived",
        "relative_path": "scans/page-049.png",
        "sha256": f"sha256:{hashlib.sha256(scan).hexdigest()}",
        "derived_from_source_id": "public-book",
        "derivation": {
            "kind": "pdf_page_raster",
            "page_number": 49,
            "renderer_id": "poppler-0001",
        },
    }
    case["reference"] = None
    case["variants"] = [{"variant_id": "original", "kind": "original"}]
    manifest_path = tmp_path / "scan-evaluation.json"
    _write_manifest(manifest_path, manifest)

    report = evaluate_development_raster_corpus(root, manifest_path)

    assert report["summary"]["observation_only_case_count"] == 1
    assert report["summary"]["reference_case_count"] == 0
    result = report["cases"][0]
    assert result["derivation_status"] == "declared_derivative_not_recomputed"
    comparison = result["variants"][0]["reference_comparison"]
    assert comparison == {
        "available": False,
        "geometric_accuracy_measured": False,
        "reason": "no_explicit_vector_reference",
    }
    rendered = render_evaluation(report)
    assert "page-049.png" not in rendered
    assert "source.pdf" not in rendered
    assert "public-book" not in rendered


def test_materialized_variants_are_atomic_deterministic_redacted_and_no_overwrite(
    tmp_path: Path,
) -> None:
    root, manifest_path, manifest, source_image = _paired_fixture(tmp_path)
    manifest["cases"][0]["variants"].append(
        {
            "variant_id": "noise-0002",
            "kind": "gaussian_noise",
            "seed": 38,
            "sigma": 8.0,
        }
    )
    _write_manifest(manifest_path, manifest)
    data_root = tmp_path / "data"
    allowed_root = data_root / "derived"
    allowed_root.mkdir(parents=True)
    first_root = allowed_root / "materialized-0001"
    second_root = allowed_root / "materialized-0002"
    source_hash_before = hashlib.sha256((root / "public" / "source.png").read_bytes()).hexdigest()

    first_report = evaluate_development_raster_corpus(
        root,
        manifest_path,
        materialize_root=first_root,
        materialize_allowed_root=allowed_root,
        materialize_data_root=data_root,
    )
    second_report = evaluate_development_raster_corpus(
        root,
        manifest_path,
        materialize_root=second_root,
        materialize_allowed_root=allowed_root,
        materialize_data_root=data_root,
    )

    assert first_report == second_report
    first_manifest_bytes = (first_root / "derivation-manifest.json").read_bytes()
    second_manifest_bytes = (second_root / "derivation-manifest.json").read_bytes()
    assert first_manifest_bytes == second_manifest_bytes
    derivation_manifest = json.loads(first_manifest_bytes)
    assert derivation_manifest["production_evidence"] is False
    assert derivation_manifest["production_acceptance_eligible"] is False
    assert derivation_manifest["engineer_reviewed"] is False
    assert derivation_manifest["company_approved"] is False
    assert derivation_manifest["customer_data_used"] is False
    assert derivation_manifest["derivation_count"] == 3
    assert {
        (item["variant_id"], item["transform"]["kind"])
        for item in derivation_manifest["derivations"]
    } == {
        ("blur-0001", "gaussian_blur"),
        ("noise-0001", "gaussian_noise"),
        ("noise-0002", "gaussian_noise"),
    }
    noise = [
        item["transform"]
        for item in derivation_manifest["derivations"]
        if item["transform"]["kind"] == "gaussian_noise"
    ]
    assert {item["seed"] for item in noise} == {37, 38}
    assert {item["sigma"] for item in noise} == {2.0, 8.0}
    for derivation in derivation_manifest["derivations"]:
        relative = Path(derivation["artifact_ref"])
        first_bytes = (first_root / relative).read_bytes()
        second_bytes = (second_root / relative).read_bytes()
        assert first_bytes == second_bytes
        assert first_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        assert derivation["output_sha256"] == (f"sha256:{hashlib.sha256(first_bytes).hexdigest()}")
    assert not (first_root / "case-0001" / "original.png").exists()
    assert not list(first_root.rglob("*.tmp"))
    assert hashlib.sha256((root / "public" / "source.png").read_bytes()).hexdigest() == (
        source_hash_before
    )
    assert (root / "public" / "source.png").read_bytes() == source_image
    serialized = first_manifest_bytes.decode("utf-8")
    assert "source.png" not in serialized
    assert "reference.dxf" not in serialized
    assert "public-raster" not in serialized
    assert "public-vector" not in serialized
    assert str(tmp_path) not in serialized

    hashes_before = {
        path.relative_to(first_root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first_root.rglob("*")
        if path.is_file()
    }
    with pytest.raises(DevelopmentRasterEvaluationError) as caught:
        evaluate_development_raster_corpus(
            root,
            manifest_path,
            materialize_root=first_root,
            materialize_allowed_root=allowed_root,
            materialize_data_root=data_root,
        )
    assert caught.value.code == "MATERIALIZATION_ALREADY_EXISTS"
    assert hashes_before == {
        path.relative_to(first_root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first_root.rglob("*")
        if path.is_file()
    }


def test_materialization_rejects_insufficient_variants_and_path_escape(tmp_path: Path) -> None:
    root, manifest_path, _, _ = _paired_fixture(tmp_path)
    data_root = tmp_path / "data"
    allowed_root = data_root / "derived"
    allowed_root.mkdir(parents=True)

    with pytest.raises(DevelopmentRasterEvaluationError) as insufficient:
        evaluate_development_raster_corpus(
            root,
            manifest_path,
            materialize_root=allowed_root / "materialized-0001",
            materialize_allowed_root=allowed_root,
            materialize_data_root=data_root,
        )

    assert insufficient.value.code == "MATERIALIZATION_VARIANTS_INSUFFICIENT"
    assert not (allowed_root / "materialized-0001").exists()

    second_fixture_root = tmp_path / "second"
    second_fixture_root.mkdir()
    _, _, manifest, _ = _paired_fixture(second_fixture_root)
    manifest["cases"][0]["variants"].append(
        {
            "variant_id": "noise-0002",
            "kind": "gaussian_noise",
            "seed": 38,
            "sigma": 8.0,
        }
    )
    escaped_manifest = tmp_path / "second" / "escaped.json"
    _write_manifest(escaped_manifest, manifest)
    outside = data_root / "outside-0001"
    with pytest.raises(DevelopmentRasterEvaluationError) as escaped:
        evaluate_development_raster_corpus(
            tmp_path / "second" / "public",
            escaped_manifest,
            materialize_root=outside,
            materialize_allowed_root=allowed_root,
            materialize_data_root=data_root,
        )

    assert escaped.value.code == "MATERIALIZATION_PATH_NOT_ALLOWED"
    assert not outside.exists()
