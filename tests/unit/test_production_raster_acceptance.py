"""Production raster evidence requires raw measurements and two trusted reviewers."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from scripts.check_production_raster_acceptance import (
    EXECUTION_KEY_SHA256_ENV,
    EXECUTION_PUBLIC_KEY_ENV,
    TRUST_POLICY_ENV,
    TRUST_POLICY_SHA256_ENV,
    main,
    raster_accuracy_attestation_claims,
    raster_engineer_attestation_claims,
    verify_production_raster_acceptance,
)

from cad_harness.domain.canonical import canonical_json
from cad_harness.security.evidence_attestation import (
    EvidenceRole,
    EvidenceTrustPolicy,
    TrustPolicyIdentity,
    issue_attestation,
    trust_policy_sha256,
)
from cad_harness.security.execution_receipt import (
    ExecutionReceiptClaims,
    execution_public_key_sha256,
    issue_execution_receipt,
)

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _keypair(marker: int) -> tuple[str, str]:
    key = Ed25519PrivateKey.from_private_bytes(bytes([marker]) * 32)
    private = _b64(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    public = _b64(
        key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    return private, public


ENGINEER_PRIVATE, ENGINEER_PUBLIC = _keypair(1)
ACCURACY_PRIVATE, ACCURACY_PUBLIC = _keypair(2)
EXECUTION_PRIVATE, EXECUTION_PUBLIC = _keypair(3)


def _write_json(path: Path, value: Mapping[str, object]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _identity(identity_id: str, role: EvidenceRole, public_key: str) -> TrustPolicyIdentity:
    return TrustPolicyIdentity(
        identity_id=identity_id,
        allowed_roles=(role,),
        public_key=public_key,
    )


def _write_policy(path: Path, identities: tuple[TrustPolicyIdentity, ...]) -> None:
    policy = EvidenceTrustPolicy(identities=identities)
    path.write_text(
        json.dumps(
            policy.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _execution_key_sha256() -> str:
    return execution_public_key_sha256(EXECUTION_PUBLIC)


def _execution_receipt(readback: Mapping[str, object]) -> dict[str, object]:
    claims = ExecutionReceiptClaims.model_validate(
        {
            "adapter_type": readback["adapter_type"],
            "process_id": readback["process_id"],
            "document_id": readback["document_id"],
            "pre_revision": readback["pre_revision"],
            "post_revision": readback["post_revision"],
            "plan_hash": readback["plan_hash"],
            "job_id": readback["job_id"],
            "validation_report_sha256": readback["validation_report_sha256"],
            "result_sha256": f"sha256:{readback['sha256']}",
        }
    )
    return issue_execution_receipt(
        claims,
        signer_id="owned-bridge-signer",
        private_key=EXECUTION_PRIVATE,
        issued_at=NOW - timedelta(minutes=30),
    ).to_external_dict()


def _accuracy_samples(
    index: int, source_sha256: str, candidate_ref: str, entity_ref: str
) -> list[dict[str, object]]:
    return [
        {
            "sample_id": f"sample-{index}-{sample_index}",
            "candidate_ref": candidate_ref,
            "entity_ref": entity_ref,
            "geometry_kind": "line",
            "unit": "mm",
            "source_sha256": source_sha256,
            "kind": "length",
            "measurement_key": f"measurement-{sample_index}",
            "expected_mm": float(sample_index),
            "observed_mm": sample_index + 0.125,
        }
        if sample_index % 2 == 0
        else {
            "sample_id": f"sample-{index}-{sample_index}",
            "candidate_ref": candidate_ref,
            "entity_ref": entity_ref,
            "geometry_kind": "line",
            "unit": "mm",
            "source_sha256": source_sha256,
            "kind": "point",
            "measurement_key": f"measurement-{sample_index}",
            "expected_mm": [float(sample_index), float(sample_index)],
            "observed_mm": [sample_index + 0.125, float(sample_index)],
        }
        for sample_index in range(8)
    ]


def _case(root: Path, index: int) -> dict[str, object]:
    case_id = f"scan-{index:02d}"
    directory = root / case_id
    directory.mkdir()
    image = np.zeros((32, 32), dtype=np.uint8)
    image[4:28, 4 + index : 6 + index] = 255
    encoded, source_array = cv2.imencode(".png", image)
    assert encoded
    source = source_array.tobytes()
    (directory / "source.png").write_bytes(source)
    source_hash = hashlib.sha256(source).hexdigest()
    source_binding = f"sha256:{source_hash}"
    types = ["line"]
    if index == 0:
        types = ["line", "circle", "arc", "closed_polyline"]
    calibration: dict[str, object] = {
        "source_sha256": source_binding,
        "pixel_distance": 100.0,
        "real_distance_mm": 25.0,
        "engineer_id": f"calibration-engineer-{index}",
        "evidence_ref": f"calibration-record-{index}",
    }
    calibration["artifact_ref"] = f"{case_id}/calibration.json"
    calibration["sha256"] = _write_json(directory / "calibration.json", calibration)
    trace: dict[str, object] = {
        "source_sha256": source_binding,
        "deterministic_runs": 2,
        "detected_types": types,
    }
    trace["artifact_ref"] = f"{case_id}/trace.json"
    trace["sha256"] = _write_json(directory / "trace.json", trace)
    candidate_ref = f"candidate-{index}"
    candidate_payload: dict[str, object] = {
        "schema_version": "1.0",
        "source_sha256": source_binding,
        "candidates": [
            {
                "candidate_ref": candidate_ref,
                "geometry_kind": "line",
                "geometry": {
                    "start": [float(index), 0.0],
                    "end": [float(index) + 25.0, 0.0],
                },
            }
        ],
    }
    candidate_set_sha256 = (
        f"sha256:{hashlib.sha256(canonical_json(candidate_payload).encode()).hexdigest()}"
    )
    candidate_geometry = {
        "artifact_ref": f"{case_id}/candidates.json",
        "sha256": _write_json(directory / "candidates.json", candidate_payload),
    }
    acceptance: dict[str, object] = {
        "engineer_id": f"acceptance-engineer-{index}",
        "evidence_ref": f"acceptance-record-{index}",
        "source_sha256": source_binding,
        "candidate_set_sha256": candidate_set_sha256,
        "accepted_candidate_count": 1,
        "accepted_candidate_refs": [candidate_ref],
    }
    acceptance["artifact_ref"] = f"{case_id}/acceptance.json"
    acceptance["sha256"] = _write_json(directory / "acceptance.json", acceptance)
    entity_ref = f"entity-{index}"
    samples = _accuracy_samples(index, source_binding, candidate_ref, entity_ref)
    accuracy: dict[str, object] = {
        "source_sha256": source_binding,
        "calculated_by": f"metrologist-{index}",
        "reviewed_by": "accuracy-reviewer",
        "samples": samples,
        "sample_count": len(samples),
        "tolerance_mm": 0.25,
        "maximum_error_mm": 0.125,
        "rmse_mm": 0.125,
    }
    accuracy["artifact_ref"] = f"{case_id}/accuracy.json"
    accuracy["sha256"] = _write_json(directory / "accuracy.json", accuracy)
    readback: dict[str, object] = {
        "source_sha256": source_binding,
        "acceptance_sha256": f"sha256:{acceptance['sha256']}",
        "trace_sha256": f"sha256:{trace['sha256']}",
        "candidate_set_sha256": acceptance["candidate_set_sha256"],
        "accepted_candidate_refs": acceptance["accepted_candidate_refs"],
        "job_id": f"job-raster-{index}",
        "plan_hash": "sha256:" + f"{index + 101:064x}",
        "adapter_type": "dotnet_bridge",
        "process_id": 10_000 + index,
        "document_id": f"document-{index}",
        "validation_passed": True,
        "autocad_version": "26.0",
        "pre_revision": "sha256:" + f"{index + 1:064x}",
        "post_revision": "sha256:" + f"{index + 11:064x}",
        "measured_geometry": [
            {
                "candidate_ref": candidate_ref,
                "entity_ref": entity_ref,
                "geometry_kind": "line",
                "unit": "mm",
                "measurements": {
                    str(sample["measurement_key"]): sample["observed_mm"] for sample in samples
                },
            }
        ],
        "validation_report_sha256": "sha256:" + f"{index + 201:064x}",
    }
    readback["artifact_ref"] = f"{case_id}/readback.json"
    readback["sha256"] = _write_json(directory / "readback.json", readback)
    receipt = _execution_receipt(readback)
    execution_receipt = {
        "artifact_ref": f"{case_id}/execution-receipt.json",
        "sha256": _write_json(directory / "execution-receipt.json", receipt),
    }
    return {
        "case_id": case_id,
        "source": {
            "artifact_ref": f"{case_id}/source.png",
            "sha256": source_hash,
            "media_type": "image/png",
            "synthetic": False,
            "generated": False,
            "simulated": False,
            "shop_scan": True,
            "deidentified": True,
            "scan_quality": "noisy" if index == 0 else "clean",
            "provenance_ref": f"controlled-shop-record-{index}",
            "prepared_by": f"shop-engineer-{index}",
        },
        "calibration": calibration,
        "trace": trace,
        "candidate_geometry": candidate_geometry,
        "engineer_acceptance": acceptance,
        "accuracy": accuracy,
        "live_readback": readback,
        "execution_receipt": execution_receipt,
    }


def _sign_case(
    case: dict[str, object],
    engineer: TrustPolicyIdentity,
    accuracy: TrustPolicyIdentity,
    *,
    engineer_role: EvidenceRole = EvidenceRole.RASTER_ENGINEER_REVIEWER,
    engineer_issued_at: datetime = NOW - timedelta(hours=1),
    engineer_expires_at: datetime = NOW + timedelta(days=1),
) -> None:
    case["engineer_review_attestation"] = issue_attestation(
        raster_engineer_attestation_claims(case),
        engineer,
        engineer_role,
        ENGINEER_PRIVATE if engineer.public_key == ENGINEER_PUBLIC else ACCURACY_PRIVATE,
        issued_at=engineer_issued_at,
        expires_at=engineer_expires_at,
    ).to_external_dict()
    case["accuracy_review_attestation"] = issue_attestation(
        raster_accuracy_attestation_claims(case),
        accuracy,
        EvidenceRole.RASTER_ACCURACY_REVIEWER,
        ACCURACY_PRIVATE if accuracy.public_key == ACCURACY_PUBLIC else ENGINEER_PRIVATE,
        issued_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    ).to_external_dict()


def _packet(root: Path, *, same_reviewer: bool = False) -> tuple[Path, Path, dict[str, str]]:
    engineer = _identity(
        "engineer-reviewer", EvidenceRole.RASTER_ENGINEER_REVIEWER, ENGINEER_PUBLIC
    )
    accuracy = _identity(
        "accuracy-reviewer", EvidenceRole.RASTER_ACCURACY_REVIEWER, ACCURACY_PUBLIC
    )
    if same_reviewer:
        engineer = TrustPolicyIdentity(
            identity_id="dual-role-reviewer",
            allowed_roles=(
                EvidenceRole.RASTER_ENGINEER_REVIEWER,
                EvidenceRole.RASTER_ACCURACY_REVIEWER,
            ),
            public_key=ENGINEER_PUBLIC,
        )
        accuracy = engineer
    cases = [_case(root, index) for index in range(5)]
    for case in cases:
        _sign_case(case, engineer, accuracy)
    manifest = root / "production-raster.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "evidence_kind": "production_raster_acceptance",
                "production_evidence": True,
                "development_evidence": False,
                "synthetic_evidence": False,
                "generated_evidence": False,
                "simulated_evidence": False,
                "cases": cases,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    policy = root / "raster-trust-policy.json"
    identities = (engineer,) if same_reviewer else (engineer, accuracy)
    _write_policy(policy, identities)
    env = {
        TRUST_POLICY_SHA256_ENV: trust_policy_sha256(EvidenceTrustPolicy(identities=identities)),
        EXECUTION_PUBLIC_KEY_ENV: EXECUTION_PUBLIC,
        EXECUTION_KEY_SHA256_ENV: _execution_key_sha256(),
    }
    return manifest, policy, env


def _load_manifest(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _save_manifest(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _codes(result: Mapping[str, object]) -> set[str]:
    errors = result["errors"]
    assert isinstance(errors, list)
    return {str(error["code"]) for error in errors}


def _review_identities() -> tuple[TrustPolicyIdentity, TrustPolicyIdentity]:
    return (
        _identity("engineer-reviewer", EvidenceRole.RASTER_ENGINEER_REVIEWER, ENGINEER_PUBLIC),
        _identity("accuracy-reviewer", EvidenceRole.RASTER_ACCURACY_REVIEWER, ACCURACY_PUBLIC),
    )


def _resign_case(case: dict[str, object]) -> None:
    engineer, accuracy = _review_identities()
    _sign_case(case, engineer, accuracy)


def test_valid_five_case_packet_passes_with_environment_policy_fallback(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    env[TRUST_POLICY_ENV] = str(policy)

    result = verify_production_raster_acceptance(manifest, env=env, now=NOW)

    assert result == {"passed": True, "case_count": 5, "errors": []}


def test_missing_policy_and_missing_policy_pin_fail_closed(tmp_path: Path) -> None:
    manifest, policy, _ = _packet(tmp_path)

    missing_policy = verify_production_raster_acceptance(manifest, env={}, now=NOW)
    missing_pin = verify_production_raster_acceptance(manifest, policy, env={}, now=NOW)

    assert "TRUST_POLICY_MISSING" in _codes(missing_policy)
    assert "ATTESTATION_UNVERIFIED" in _codes(missing_policy)
    assert "TRUST_POLICY_DIGEST_MISSING" in _codes(missing_pin)
    assert "ATTESTATION_VERIFICATION_FAILED" in _codes(missing_pin)


def test_missing_execution_trust_config_fails_closed(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    env.pop(EXECUTION_PUBLIC_KEY_ENV)
    env.pop(EXECUTION_KEY_SHA256_ENV)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "EXECUTION_TRUST_CONFIG_MISSING" in _codes(result)
    assert "EXECUTION_TRUST_CONFIG_INVALID" in _codes(result)


def test_wrong_execution_key_pin_fails_without_private_key_leakage(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    env[EXECUTION_KEY_SHA256_ENV] = f"sha256:{'0' * 64}"

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)
    rendered = json.dumps(result, sort_keys=True)

    assert "EXECUTION_TRUST_KEY_MISMATCH" in _codes(result)
    assert EXECUTION_PRIVATE not in rendered
    assert ENGINEER_PRIVATE not in rendered
    assert ACCURACY_PRIVATE not in rendered


def test_wrong_role_tamper_and_expiry_fail_closed(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list)
    first = cases[0]
    assert isinstance(first, dict)
    accuracy_identity = _identity(
        "accuracy-reviewer", EvidenceRole.RASTER_ACCURACY_REVIEWER, ACCURACY_PUBLIC
    )
    first["engineer_review_attestation"] = issue_attestation(
        raster_engineer_attestation_claims(first),
        accuracy_identity,
        EvidenceRole.RASTER_ACCURACY_REVIEWER,
        ACCURACY_PRIVATE,
        issued_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    ).to_external_dict()
    second = cases[1]
    assert isinstance(second, dict)
    tampered = second["accuracy_review_attestation"]
    assert isinstance(tampered, dict)
    tampered["signature"] = "ed25519:" + _b64(b"\x00" * 64)
    third = cases[2]
    assert isinstance(third, dict)
    engineer_identity = _identity(
        "engineer-reviewer", EvidenceRole.RASTER_ENGINEER_REVIEWER, ENGINEER_PUBLIC
    )
    _sign_case(
        third,
        engineer_identity,
        accuracy_identity,
        engineer_issued_at=NOW - timedelta(days=2),
        engineer_expires_at=NOW - timedelta(days=1),
    )
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "ATTESTATION_ROLE_INVALID" in _codes(result)
    assert "ATTESTATION_VERIFICATION_FAILED" in _codes(result)
    assert (
        sum(error["code"] == "ATTESTATION_VERIFICATION_FAILED" for error in result["errors"]) >= 2
    )


def test_same_identity_cannot_review_engineering_and_accuracy(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path, same_reviewer=True)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "RASTER_REVIEWERS_NOT_INDEPENDENT" in _codes(result)


def test_self_declared_ids_and_booleans_do_not_replace_attestations(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list)
    for case in cases:
        assert isinstance(case, dict)
        case.pop("engineer_review_attestation")
        case.pop("accuracy_review_attestation")
        case["engineer_reviewed"] = True
        case["accuracy_reviewed"] = True
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "ATTESTATION_INVALID" in _codes(result)


def test_signed_nonsensical_aggregate_cannot_override_unchanged_samples(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list)
    case = cases[0]
    assert isinstance(case, dict)
    accuracy = case["accuracy"]
    assert isinstance(accuracy, dict)
    accuracy["sample_count"] = 999
    accuracy["maximum_error_mm"] = 0.0
    accuracy["rmse_mm"] = 0.0
    artifact = tmp_path / str(accuracy["artifact_ref"])
    accuracy["sha256"] = _write_json(artifact, accuracy)
    accuracy_identity = _identity(
        "accuracy-reviewer", EvidenceRole.RASTER_ACCURACY_REVIEWER, ACCURACY_PUBLIC
    )
    case["accuracy_review_attestation"] = issue_attestation(
        raster_accuracy_attestation_claims(case),
        accuracy_identity,
        EvidenceRole.RASTER_ACCURACY_REVIEWER,
        ACCURACY_PRIVATE,
        issued_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    ).to_external_dict()
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "ACCURACY_AGGREGATE_MISMATCH" in _codes(result)


def test_artifact_tamper_and_path_escape_fail(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list)
    first = cases[0]
    assert isinstance(first, dict)
    trace = first["trace"]
    assert isinstance(trace, dict)
    trace["artifact_ref"] = "../outside.json"
    _save_manifest(manifest, payload)
    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)
    assert "ARTIFACT_MISSING" in _codes(result)

    second_root = tmp_path / "second"
    second_root.mkdir()
    manifest, policy, env = _packet(second_root)
    (second_root / "scan-00" / "accuracy.json").write_text("tampered", encoding="utf-8")
    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)
    assert "ARTIFACT_HASH_MISMATCH" in _codes(result)


def test_reviewer_repro_rejects_flags_invalid_image_and_reused_evidence(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    payload["generated_evidence"] = True
    cases = payload["cases"]
    assert isinstance(cases, list)
    first = cases[0]
    second = cases[1]
    assert isinstance(first, dict) and isinstance(second, dict)
    first_source = first["source"]
    second_source = second["source"]
    assert isinstance(first_source, dict) and isinstance(second_source, dict)
    source_path = tmp_path / str(first_source["artifact_ref"])
    source_path.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-decodable-image")
    second_source["artifact_ref"] = first_source["artifact_ref"]
    second_source["sha256"] = first_source["sha256"]
    second_calibration = second["calibration"]
    first_calibration = first["calibration"]
    assert isinstance(second_calibration, dict) and isinstance(first_calibration, dict)
    second_calibration["artifact_ref"] = first_calibration["artifact_ref"]
    second_calibration["sha256"] = first_calibration["sha256"]
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert {
        "MANIFEST_PRODUCTION_FLAGS_INVALID",
        "SOURCE_IMAGE_INVALID",
        "SOURCE_PATH_REUSED",
        "SOURCE_HASH_REUSED",
        "EVIDENCE_ARTIFACT_PATH_REUSED",
        "EVIDENCE_ARTIFACT_HASH_REUSED",
    }.issubset(_codes(result))


def test_declared_mime_must_match_exact_image_magic_and_suffix(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list) and isinstance(cases[0], dict)
    case = cases[0]
    source = case["source"]
    assert isinstance(source, dict)
    source["media_type"] = "image/jpeg"
    _resign_case(case)
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert {"SOURCE_MEDIA_MAGIC_MISMATCH", "SOURCE_MEDIA_SUFFIX_MISMATCH"}.issubset(_codes(result))


@pytest.mark.parametrize(("shape", "uniform"), [((8, 8), False), ((32, 32), True)])
def test_image_must_have_minimum_dimensions_and_nondegenerate_content(
    tmp_path: Path,
    shape: tuple[int, int],
    uniform: bool,
) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list) and isinstance(cases[0], dict)
    case = cases[0]
    source = case["source"]
    assert isinstance(source, dict)
    image = np.zeros(shape, dtype=np.uint8)
    if not uniform:
        image[0, 0] = 255
    encoded, source_array = cv2.imencode(".png", image)
    assert encoded
    source_bytes = source_array.tobytes()
    (tmp_path / str(source["artifact_ref"])).write_bytes(source_bytes)
    source["sha256"] = hashlib.sha256(source_bytes).hexdigest()
    _resign_case(case)
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "SOURCE_IMAGE_DEGENERATE" in _codes(result)


def test_candidate_geometry_is_parsed_and_candidate_set_hash_is_recomputed(
    tmp_path: Path,
) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list) and isinstance(cases[0], dict)
    case = cases[0]
    section = case["candidate_geometry"]
    assert isinstance(section, dict)
    artifact = tmp_path / str(section["artifact_ref"])
    geometry = _load_manifest(artifact)
    candidates = geometry["candidates"]
    assert isinstance(candidates, list) and isinstance(candidates[0], dict)
    line = candidates[0]["geometry"]
    assert isinstance(line, dict)
    line["end"] = [40.0, 0.0]
    section["sha256"] = _write_json(artifact, geometry)
    _resign_case(case)
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "CANDIDATE_SET_HASH_MISMATCH" in _codes(result)


def test_degenerate_candidate_geometry_and_global_candidate_reuse_fail(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list)
    first, second = cases[:2]
    assert isinstance(first, dict) and isinstance(second, dict)
    first_section = first["candidate_geometry"]
    assert isinstance(first_section, dict)
    artifact = tmp_path / str(first_section["artifact_ref"])
    geometry = _load_manifest(artifact)
    candidates = geometry["candidates"]
    assert isinstance(candidates, list) and isinstance(candidates[0], dict)
    line = candidates[0]["geometry"]
    assert isinstance(line, dict)
    line["end"] = line["start"]
    first_section["sha256"] = _write_json(artifact, geometry)
    _resign_case(first)
    first_acceptance = first["engineer_acceptance"]
    second_acceptance = second["engineer_acceptance"]
    assert isinstance(first_acceptance, dict) and isinstance(second_acceptance, dict)
    second_acceptance["candidate_set_sha256"] = first_acceptance["candidate_set_sha256"]
    second_acceptance["accepted_candidate_refs"] = first_acceptance["accepted_candidate_refs"]
    second_acceptance["sha256"] = _write_json(
        tmp_path / str(second_acceptance["artifact_ref"]), second_acceptance
    )
    _resign_case(second)
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert {
        "CANDIDATE_GEOMETRY_ARTIFACT_INVALID",
        "CANDIDATE_SET_HASH_REUSED",
        "CANDIDATE_REF_REUSED",
    }.issubset(_codes(result))


def test_reviewer_repro_rejects_signed_negative_unbound_samples(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list)
    case = cases[0]
    assert isinstance(case, dict)
    accuracy = case["accuracy"]
    assert isinstance(accuracy, dict)
    samples = accuracy["samples"]
    assert isinstance(samples, list)
    sample = samples[0]
    assert isinstance(sample, dict)
    sample["expected_mm"] = -1.0
    sample["candidate_ref"] = "not-an-accepted-candidate"
    artifact = tmp_path / str(accuracy["artifact_ref"])
    accuracy["sha256"] = _write_json(artifact, accuracy)
    accuracy_identity = _identity(
        "accuracy-reviewer", EvidenceRole.RASTER_ACCURACY_REVIEWER, ACCURACY_PUBLIC
    )
    case["accuracy_review_attestation"] = issue_attestation(
        raster_accuracy_attestation_claims(case),
        accuracy_identity,
        EvidenceRole.RASTER_ACCURACY_REVIEWER,
        ACCURACY_PRIVATE,
        issued_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    ).to_external_dict()
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "ACCURACY_SAMPLES_INVALID" in _codes(result)


def test_signed_accuracy_observation_must_match_live_measurement(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list) and isinstance(cases[0], dict)
    case = cases[0]
    accuracy = case["accuracy"]
    assert isinstance(accuracy, dict)
    samples = accuracy["samples"]
    assert isinstance(samples, list) and isinstance(samples[0], dict)
    samples[0]["observed_mm"] = 50.0
    errors = []
    for sample in samples:
        assert isinstance(sample, dict)
        expected = sample["expected_mm"]
        observed = sample["observed_mm"]
        if isinstance(expected, list) and isinstance(observed, list):
            errors.append(math.hypot(observed[0] - expected[0], observed[1] - expected[1]))
        else:
            errors.append(abs(float(observed) - float(expected)))
    accuracy["maximum_error_mm"] = max(errors)
    accuracy["rmse_mm"] = math.sqrt(sum(error * error for error in errors) / len(errors))
    accuracy["sha256"] = _write_json(tmp_path / str(accuracy["artifact_ref"]), accuracy)
    _, accuracy_identity = _review_identities()
    case["accuracy_review_attestation"] = issue_attestation(
        raster_accuracy_attestation_claims(case),
        accuracy_identity,
        EvidenceRole.RASTER_ACCURACY_REVIEWER,
        ACCURACY_PRIVATE,
        issued_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    ).to_external_dict()
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "ACCURACY_OBSERVED_LIVE_MISMATCH" in _codes(result)


def test_self_authored_readback_without_bridge_receipt_fails(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list) and isinstance(cases[0], dict)
    case = cases[0]
    case.pop("execution_receipt")
    _resign_case(case)
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "EXECUTION_RECEIPT_MISSING" in _codes(result)


def test_forged_bridge_receipt_signature_fails_even_when_human_resigns(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list) and isinstance(cases[0], dict)
    case = cases[0]
    pointer = case["execution_receipt"]
    assert isinstance(pointer, dict)
    artifact = tmp_path / str(pointer["artifact_ref"])
    receipt = _load_manifest(artifact)
    receipt["signature"] = f"ed25519:{_b64(b'forged' * 10 + b'fail')}"
    pointer["sha256"] = _write_json(artifact, receipt)
    _resign_case(case)
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "EXECUTION_RECEIPT_SIGNATURE_INVALID" in _codes(result)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("adapter_type", "com"),
        ("process_id", 99_999),
        ("document_id", "different-document"),
        ("pre_revision", "sha256:" + "8" * 64),
        ("post_revision", "sha256:" + "9" * 64),
        ("plan_hash", "sha256:" + "7" * 64),
        ("job_id", "different-job"),
        ("validation_report_sha256", "sha256:" + "6" * 64),
    ],
)
def test_bridge_receipt_exactly_binds_execution_context_and_result_hash(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list) and isinstance(cases[0], dict)
    case = cases[0]
    readback = case["live_readback"]
    assert isinstance(readback, dict)
    readback[field] = replacement
    readback["sha256"] = _write_json(tmp_path / str(readback["artifact_ref"]), readback)
    _resign_case(case)
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert "EXECUTION_RECEIPT_BINDING_INVALID" in _codes(result)


def test_reviewer_repro_rejects_signed_handwritten_readback_assertion(tmp_path: Path) -> None:
    manifest, policy, env = _packet(tmp_path)
    payload = _load_manifest(manifest)
    cases = payload["cases"]
    assert isinstance(cases, list)
    case = cases[0]
    assert isinstance(case, dict)
    readback = case["live_readback"]
    assert isinstance(readback, dict)
    readback["adapter_type"] = "fake"
    readback["accepted_candidate_refs"] = ["invented-candidate"]
    readback["measured_geometry"] = []
    artifact = tmp_path / str(readback["artifact_ref"])
    readback["sha256"] = _write_json(artifact, readback)
    engineer_identity = _identity(
        "engineer-reviewer", EvidenceRole.RASTER_ENGINEER_REVIEWER, ENGINEER_PUBLIC
    )
    case["engineer_review_attestation"] = issue_attestation(
        raster_engineer_attestation_claims(case),
        engineer_identity,
        EvidenceRole.RASTER_ENGINEER_REVIEWER,
        ENGINEER_PRIVATE,
        issued_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    ).to_external_dict()
    _save_manifest(manifest, payload)

    result = verify_production_raster_acceptance(manifest, policy, env=env, now=NOW)

    assert {
        "LIVE_READBACK_BINDING_MISMATCH",
        "LIVE_READBACK_EXECUTION_INVALID",
        "LIVE_READBACK_MEASUREMENT_UNBOUND",
    }.issubset(_codes(result))


def test_cli_accepts_explicit_policy_and_redacts_missing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest, policy, env = _packet(tmp_path)
    for name, secret in env.items():
        monkeypatch.setenv(name, secret)
    assert main([str(manifest), "--trust-policy", str(policy)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True

    missing = tmp_path / "customer-name" / "missing.json"
    assert main([str(missing), "--trust-policy", str(policy)]) == 1
    output = capsys.readouterr().out
    assert str(missing) not in output
    assert json.loads(output)["errors"] == [{"code": "MANIFEST_UNREADABLE", "field": "manifest"}]
