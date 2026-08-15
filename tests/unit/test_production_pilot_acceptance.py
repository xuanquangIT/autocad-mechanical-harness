"""Production pilot claims require locked human evidence, not development fixtures."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import scripts.check_production_pilot_acceptance as pilot_verifier
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from scripts.check_production_pilot_acceptance import (
    main,
    verify_production_pilot_acceptance,
)

from cad_harness.domain.canonical import canonical_json
from cad_harness.domain.models.metrics import BaselineCase, EffortRecord, FailureReason
from cad_harness.metrics.collector import MetricsCollector, load_pilot_thresholds
from cad_harness.security.evidence_attestation import (
    EvidenceRole,
    EvidenceTrustPolicy,
    TrustPolicyIdentity,
    issue_attestation,
    trust_policy_from_mapping,
    trust_policy_sha256,
)

_RUN_ID = "pilot-run-2026-01"
_ENGINEER_ID = "engineer-opaque-01"
_REVIEWER_ID = "reviewer-opaque-01"
_ATTESTED_AT = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)
_VERIFICATION_TIME = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
_FLAGS = {
    "production_evidence": True,
    "synthetic": False,
    "simulated": False,
    "generated": False,
    "development": False,
}


def _encode_key(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _keypair(marker: int) -> tuple[str, str]:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes([marker]) * 32)
    private = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _encode_key(private), _encode_key(public)


_ENGINEER_PRIVATE_KEY, _ENGINEER_PUBLIC_KEY = _keypair(1)
_REVIEWER_PRIVATE_KEY, _REVIEWER_PUBLIC_KEY = _keypair(2)


def _write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_json(payload).encode("utf-8")
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _write_dxf(path: Path, source_id: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "0\nSECTION\n2\nHEADER\n9\n$COMMENT\n1\n"
        f"{source_id}\n"
        "0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
    ).encode("ascii")
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _write_evidence(
    root: Path, name: str, evidence_ref: str, payload: dict[str, Any]
) -> dict[str, str]:
    path = root / "evidence" / name
    digest = _write_json(path, payload)
    return {
        "evidence_ref": evidence_ref,
        "artifact_ref": path.relative_to(root).as_posix(),
        "sha256": digest,
    }


def _rewrite_evidence(root: Path, pointer: dict[str, Any], payload: dict[str, Any]) -> None:
    path = root / str(pointer["artifact_ref"])
    pointer["sha256"] = _write_json(path, payload)


def _header(kind: str, evidence_ref: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "evidence_kind": kind,
        **_FLAGS,
        "pilot_run_id": _RUN_ID,
        "evidence_ref": evidence_ref,
    }


def _identity(identity_id: str, role: EvidenceRole, public_key: str) -> TrustPolicyIdentity:
    return TrustPolicyIdentity(
        identity_id=identity_id,
        allowed_roles=(role,),
        public_key=public_key,
    )


def _engineer_identity() -> TrustPolicyIdentity:
    return _identity(_ENGINEER_ID, EvidenceRole.PILOT_ENGINEER, _ENGINEER_PUBLIC_KEY)


def _reviewer_identity() -> TrustPolicyIdentity:
    return _identity(_REVIEWER_ID, EvidenceRole.PILOT_REVIEWER, _REVIEWER_PUBLIC_KEY)


def _attest(
    claims: dict[str, Any],
    identity: TrustPolicyIdentity,
    role: EvidenceRole,
    private_key: str,
    *,
    expires_at: datetime | None = None,
) -> dict[str, str | None]:
    return issue_attestation(
        claims,  # type: ignore[arg-type]
        identity,
        role,
        private_key,
        issued_at=_ATTESTED_AT,
        expires_at=expires_at,
    ).to_external_dict()


def _attestable_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in evidence.items() if key != "attestation"}


def _attestable_consent(evidence: dict[str, Any]) -> dict[str, Any]:
    claims = dict(evidence)
    claims["participants"] = [
        {key: value for key, value in participant.items() if key != "attestation"}
        for participant in evidence["participants"]
    ]
    return claims


def _trust_policy() -> EvidenceTrustPolicy:
    return EvidenceTrustPolicy(identities=(_engineer_identity(), _reviewer_identity()))


def _write_trust_policy(root: Path) -> Path:
    path = root / "trust-policy.json"
    _write_json(path, _trust_policy().to_canonical_dict())
    return path


def _policy_pin(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return trust_policy_sha256(trust_policy_from_mapping(payload))


def _verify(
    manifest_path: Path,
    thresholds_path: Path = Path("config/pilot.yaml"),
    *,
    environment: dict[str, str] | None = None,
    now: datetime = _VERIFICATION_TIME,
) -> dict[str, Any]:
    return verify_production_pilot_acceptance(
        manifest_path,
        thresholds_path,
        trust_policy_path := manifest_path.parent / "trust-policy.json",
        expected_trust_policy_sha256=_policy_pin(trust_policy_path),
        environment={} if environment is None else environment,
        now=now,
    )


def _case_evidence(
    *,
    kind: str,
    evidence_ref: str,
    case_id: str,
    source_hash: str,
    recorded_by: str,
    measurement_started_at: str,
    measurement_ended_at: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    return {
        **_header(kind, evidence_ref),
        "case_id": case_id,
        "drawing_source_sha256": source_hash,
        "recorded_by": recorded_by,
        "measurement_started_at": measurement_started_at,
        "measurement_ended_at": measurement_ended_at,
        "record": record,
    }


def _build_bundle(
    root: Path,
    *,
    harness_minutes: float = 10.0,
    first_preview_clean: bool = True,
) -> Path:
    _write_trust_policy(root)
    engineer_identity = _engineer_identity()
    reviewer_identity = _reviewer_identity()
    consent_ref = "consent-register-01"
    consent = {
        **_header("human_participant_consent", consent_ref),
        "participants": [
            {
                "participant_id": _ENGINEER_ID,
                "consent_given": True,
                "consent_record_ref": "consent-record-01",
                "consented_at": "2026-07-01T08:00:00+07:00",
            }
        ],
    }
    consent["participants"][0]["attestation"] = _attest(
        _attestable_consent(consent),
        engineer_identity,
        EvidenceRole.PILOT_ENGINEER,
        _ENGINEER_PRIVATE_KEY,
    )
    consent_pointer = _write_evidence(root, "consent.json", consent_ref, consent)

    cases: list[dict[str, Any]] = []
    case_ids: list[str] = []
    baselines: list[BaselineCase] = []
    efforts: list[EffortRecord] = []
    case_artifact_bindings: list[dict[str, Any]] = []
    groups = ("B", "D", "E")
    for index in range(15):
        case_id = f"pilot-case-{index + 1:02d}"
        case_ids.append(case_id)
        group = groups[index % len(groups)]
        work_label = "ve_moi" if index % 2 == 0 else "sua_ban_co_san"
        source_path = root / "sources" / f"controlled-source-{index + 1:02d}.dxf"
        source_hash = _write_dxf(source_path, f"source-{index + 1:02d}")
        baseline_ref = f"baseline-record-{index + 1:02d}"
        baseline_record = {
            "pilot_run_id": _RUN_ID,
            "case_id": case_id,
            "capability_group": group,
            "work_label": work_label,
            "manual_minutes": 100.0,
            "manual_measured_by": _ENGINEER_ID,
            "manual_measurement_biased": False,
            "manual_measured_in_single_session": True,
        }
        baselines.append(BaselineCase.model_validate(baseline_record, strict=True))
        baseline_evidence = _case_evidence(
            kind="human_manual_baseline",
            evidence_ref=baseline_ref,
            case_id=case_id,
            source_hash=source_hash,
            recorded_by=_ENGINEER_ID,
            measurement_started_at="2026-07-02T08:00:00+07:00",
            measurement_ended_at="2026-07-02T09:40:00+07:00",
            record=baseline_record,
        )
        baseline_evidence["attestation"] = _attest(
            baseline_evidence,
            engineer_identity,
            EvidenceRole.PILOT_ENGINEER,
            _ENGINEER_PRIVATE_KEY,
        )
        baseline_pointer = _write_evidence(
            root,
            f"baseline-{index + 1:02d}.json",
            baseline_ref,
            baseline_evidence,
        )
        effort_ref = f"effort-record-{index + 1:02d}"
        effort_record = {
            "pilot_run_id": _RUN_ID,
            "record_id": effort_ref,
            "case_id": case_id,
            "job_id": f"pilot-job-{index + 1:02d}",
            "harness_minutes": harness_minutes,
            "idle_minutes_excluded": 0.0,
            "manual_fixup_minutes": 0.0,
            "spec_change_count": 0,
            "entities_created": 10,
            "entities_manually_edited": 0,
            "first_preview_clean": first_preview_clean,
            "completed": True,
            "failure_reason": None,
        }
        efforts.append(EffortRecord.model_validate(effort_record, strict=True))
        effort_evidence = _case_evidence(
            kind="human_harness_effort",
            evidence_ref=effort_ref,
            case_id=case_id,
            source_hash=source_hash,
            recorded_by=_ENGINEER_ID,
            measurement_started_at="2026-07-03T08:00:00+07:00",
            measurement_ended_at="2026-07-03T08:10:00+07:00",
            record=effort_record,
        )
        effort_evidence["attestation"] = _attest(
            effort_evidence,
            engineer_identity,
            EvidenceRole.PILOT_ENGINEER,
            _ENGINEER_PRIVATE_KEY,
        )
        effort_pointer = _write_evidence(
            root,
            f"effort-{index + 1:02d}.json",
            effort_ref,
            effort_evidence,
        )
        cases.append(
            {
                "case_id": case_id,
                "capability_group": group,
                "work_label": work_label,
                "engineer_selected": True,
                "engineer_participant_id": _ENGINEER_ID,
                "manual_measured_by": _ENGINEER_ID,
                "harness_operated_by": _ENGINEER_ID,
                "drawing_source_artifact_ref": source_path.relative_to(root).as_posix(),
                "drawing_source_sha256": source_hash,
                "baseline_evidence": baseline_pointer,
                "harness_evidence": effort_pointer,
            }
        )
        case_artifact_bindings.append(
            {
                "baseline_evidence_sha256": baseline_pointer["sha256"],
                "case_id": case_id,
                "drawing_source_sha256": source_hash,
                "harness_evidence_sha256": effort_pointer["sha256"],
            }
        )

    thresholds = load_pilot_thresholds()
    report = MetricsCollector(thresholds).aggregate(
        report_id="production-pilot-acceptance",
        pilot_run_id=_RUN_ID,
        baseline=baselines,
        efforts=efforts,
    )
    metrics, _ = pilot_verifier._report_metrics(report, thresholds)
    review_ref = "independent-review-01"
    review = {
        **_header("independent_human_review", review_ref),
        "reviewer_id": _REVIEWER_ID,
        "reviewer_is_human": True,
        "independent": True,
        "attestation_given": True,
        "attested_at": "2026-07-31T17:00:00+07:00",
        "review_record_ref": "review-attestation-01",
        "reviewed_case_ids": case_ids,
        "review_scope_sha256": pilot_verifier._review_scope_sha256(
            pilot_run_id=_RUN_ID,
            case_artifact_bindings=case_artifact_bindings,
            metrics=metrics,
            thresholds=thresholds,
        ),
    }
    review["attestation"] = _attest(
        review,
        reviewer_identity,
        EvidenceRole.PILOT_REVIEWER,
        _REVIEWER_PRIVATE_KEY,
    )
    review_pointer = _write_evidence(root, "review.json", review_ref, review)
    manifest = {
        "schema_version": "1.0",
        "evidence_kind": "production_pilot",
        **_FLAGS,
        "pilot_run_id": _RUN_ID,
        "human_engineer_participants": [
            {"participant_id": _ENGINEER_ID, "role": "mechanical_engineer", "human": True}
        ],
        "consent_evidence": consent_pointer,
        "independent_review": {
            "reviewer_id": _REVIEWER_ID,
            "evidence": review_pointer,
        },
        "cases": cases,
    }
    manifest_path = root / "production-pilot.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def _load_manifest(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _save_manifest(path: Path, payload: dict[str, Any]) -> None:
    _write_json(path, payload)


def _load_pointed(root: Path, pointer: dict[str, Any]) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((root / str(pointer["artifact_ref"])).read_text(encoding="utf-8")),
    )


def _resign_engineer_evidence(evidence: dict[str, Any]) -> None:
    evidence["attestation"] = _attest(
        _attestable_evidence(evidence),
        _engineer_identity(),
        EvidenceRole.PILOT_ENGINEER,
        _ENGINEER_PRIVATE_KEY,
    )


def _renew_reviewer_attestation(
    manifest_path: Path,
    thresholds_path: Path = Path("config/pilot.yaml"),
) -> None:
    root = manifest_path.parent
    manifest = _load_manifest(manifest_path)
    baselines: list[BaselineCase] = []
    efforts: list[EffortRecord] = []
    bindings: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        baseline_pointer = case["baseline_evidence"]
        effort_pointer = case["harness_evidence"]
        baseline = _load_pointed(root, baseline_pointer)["record"]
        effort = dict(_load_pointed(root, effort_pointer)["record"])
        if isinstance(effort.get("failure_reason"), str):
            effort["failure_reason"] = FailureReason(effort["failure_reason"])
        baselines.append(BaselineCase.model_validate(baseline, strict=True))
        efforts.append(EffortRecord.model_validate(effort, strict=True))
        bindings.append(
            {
                "baseline_evidence_sha256": baseline_pointer["sha256"],
                "case_id": case["case_id"],
                "drawing_source_sha256": case["drawing_source_sha256"],
                "harness_evidence_sha256": effort_pointer["sha256"],
            }
        )
    thresholds = load_pilot_thresholds(thresholds_path)
    report = MetricsCollector(thresholds).aggregate(
        report_id="production-pilot-acceptance",
        pilot_run_id=_RUN_ID,
        baseline=baselines,
        efforts=efforts,
    )
    metrics, _ = pilot_verifier._report_metrics(report, thresholds)
    review_pointer = manifest["independent_review"]["evidence"]
    review = _load_pointed(root, review_pointer)
    review["review_scope_sha256"] = pilot_verifier._review_scope_sha256(
        pilot_run_id=_RUN_ID,
        case_artifact_bindings=bindings,
        metrics=metrics,
        thresholds=thresholds,
    )
    review["attestation"] = _attest(
        _attestable_evidence(review),
        _reviewer_identity(),
        EvidenceRole.PILOT_REVIEWER,
        _REVIEWER_PRIVATE_KEY,
    )
    _rewrite_evidence(root, review_pointer, review)
    _save_manifest(manifest_path, manifest)


def _update_first_effort(
    manifest_path: Path,
    *,
    record_updates: dict[str, Any] | None = None,
    measurement_started_at: str | None = None,
    measurement_ended_at: str | None = None,
) -> None:
    root = manifest_path.parent
    manifest = _load_manifest(manifest_path)
    pointer = manifest["cases"][0]["harness_evidence"]
    evidence = _load_pointed(root, pointer)
    evidence["record"].update(record_updates or {})
    if measurement_started_at is not None:
        evidence["measurement_started_at"] = measurement_started_at
    if measurement_ended_at is not None:
        evidence["measurement_ended_at"] = measurement_ended_at
    _resign_engineer_evidence(evidence)
    _rewrite_evidence(root, pointer, evidence)
    _save_manifest(manifest_path, manifest)


def test_valid_hash_locked_human_pilot_passes_and_is_deterministic(tmp_path: Path) -> None:
    manifest = _build_bundle(tmp_path)

    first = _verify(manifest)
    second = _verify(manifest)

    assert first == second
    assert first == {
        "passed": True,
        "production_evidence_verified": True,
        "case_count": 15,
        "participant_count": 1,
        "group_case_counts": {"B": 5, "D": 5, "E": 5},
        "metrics": {
            "overall_median_saving": 0.9,
            "group_median_saving": {"B": 0.9, "D": 0.9, "E": 0.9},
            "first_preview_clean_rate": 1.0,
            "median_spec_changes": 0.0,
            "manual_entity_edit_rate": 0.0,
            "committed_job_rate": 1.0,
            "goal_met": True,
            "quality_gates_met": True,
            "all_cases_meet_saving_floor": True,
            "pilot_acceptance_met": True,
        },
        "errors": [],
    }


def test_harness_wall_interval_reconciles_active_fixup_and_idle_minutes(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    _update_first_effort(
        manifest_path,
        record_updates={
            "harness_minutes": 12.0,
            "manual_fixup_minutes": 2.0,
            "idle_minutes_excluded": 5.0,
        },
        measurement_ended_at="2026-07-03T08:15:00+07:00",
    )
    _renew_reviewer_attestation(manifest_path)

    summary = _verify(manifest_path)

    assert summary["passed"] is True
    assert summary["production_evidence_verified"] is True
    assert not any("HARNESS_DURATION" in error["code"] for error in summary["errors"])


@pytest.mark.parametrize(
    ("record_updates", "measurement_ended_at", "expected_code"),
    [
        (
            {
                "harness_minutes": 12.0,
                "manual_fixup_minutes": 2.0,
                "idle_minutes_excluded": 5.0,
            },
            "2026-07-03T08:14:00+07:00",
            "HARNESS_DURATION_EVIDENCE_MISMATCH",
        ),
        ({}, "2026-07-03T08:10:03+07:00", "HARNESS_DURATION_PRECISION_INVALID"),
        (
            {"harness_minutes": 1.0, "manual_fixup_minutes": 2.0},
            "2026-07-03T08:00:00+07:00",
            "HARNESS_ACTIVE_DURATION_NEGATIVE",
        ),
    ],
)
def test_harness_wall_interval_fails_closed_on_invalid_duration_evidence(
    tmp_path: Path,
    record_updates: dict[str, Any],
    measurement_ended_at: str,
    expected_code: str,
) -> None:
    manifest_path = _build_bundle(tmp_path)
    _update_first_effort(
        manifest_path,
        record_updates=record_updates,
        measurement_ended_at=measurement_ended_at,
    )

    summary = _verify(manifest_path)

    assert any(error["code"] == expected_code for error in summary["errors"])
    assert summary["production_evidence_verified"] is False


def test_reviewer_attestation_must_be_renewed_after_effort_evidence_changes(
    tmp_path: Path,
) -> None:
    manifest_path = _build_bundle(tmp_path)
    _update_first_effort(
        manifest_path,
        record_updates={"harness_minutes": 11.0},
        measurement_ended_at="2026-07-03T08:11:00+07:00",
    )

    summary = _verify(manifest_path)

    assert {
        "code": "REVIEW_SCOPE_DIGEST_MISMATCH",
        "field": "independent_review.evidence.review_scope_sha256",
    } in summary["errors"]
    assert not any(
        error["code"]
        in {"EVIDENCE_ATTESTATION_CLAIMS_MISMATCH", "HARNESS_DURATION_EVIDENCE_MISMATCH"}
        for error in summary["errors"]
    )


def test_reviewer_attestation_binds_threshold_policy_digest(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    thresholds = load_pilot_thresholds().model_dump(mode="json")
    thresholds["minimum_overall_saving"] = 0.51
    thresholds_path = tmp_path / "changed-thresholds.yaml"
    thresholds_path.write_text(yaml.safe_dump(thresholds), encoding="utf-8")

    summary = _verify(manifest_path, thresholds_path)

    assert any(error["code"] == "REVIEW_SCOPE_DIGEST_MISMATCH" for error in summary["errors"])


def test_consent_must_predate_every_participant_measurement(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    pointer = manifest["consent_evidence"]
    consent = _load_pointed(tmp_path, pointer)
    consent["participants"][0]["consented_at"] = "2026-07-04T08:00:00+07:00"
    consent["participants"][0]["attestation"] = _attest(
        _attestable_consent(consent),
        _engineer_identity(),
        EvidenceRole.PILOT_ENGINEER,
        _ENGINEER_PRIVATE_KEY,
    )
    _rewrite_evidence(tmp_path, pointer, consent)
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path)

    errors = [
        error for error in summary["errors"] if error["code"] == "CONSENT_AFTER_MEASUREMENT_START"
    ]
    assert len(errors) == 30
    assert {error["field"] for error in errors} == {
        "baseline_evidence.measurement_started_at",
        "harness_evidence.measurement_started_at",
    }


def test_api_can_resolve_separate_trust_policy_path_from_environment(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    policy_path = tmp_path / "trust-policy.json"
    environment = {
        "CAD_HARNESS_EVIDENCE_TRUST_POLICY": str(policy_path),
        "CAD_HARNESS_EVIDENCE_TRUST_POLICY_SHA256": _policy_pin(policy_path),
    }

    summary = verify_production_pilot_acceptance(
        manifest_path,
        environment=environment,
        now=_VERIFICATION_TIME,
    )

    assert summary["passed"] is True


def test_missing_trust_policy_or_policy_pin_fails_closed_without_private_key_leakage(
    tmp_path: Path,
) -> None:
    manifest_path = _build_bundle(tmp_path / "private-pilot")
    without_policy = verify_production_pilot_acceptance(
        manifest_path,
        environment={},
        now=_VERIFICATION_TIME,
    )
    assert {"code": "TRUST_POLICY_REQUIRED", "field": "trust_policy"} in without_policy["errors"]

    without_policy_pin = verify_production_pilot_acceptance(
        manifest_path,
        trust_policy_path=manifest_path.parent / "trust-policy.json",
        environment={},
        now=_VERIFICATION_TIME,
    )
    rendered = canonical_json(without_policy_pin)
    assert {
        "code": "EVIDENCE_ATTESTATION_POLICY_DIGEST_MISSING",
        "field": "trust_policy_sha256",
    } in without_policy_pin["errors"]
    assert _ENGINEER_PRIVATE_KEY not in rendered
    assert _REVIEWER_PRIVATE_KEY not in rendered
    assert "private-pilot" not in rendered


def test_wrong_trust_policy_pin_fails_closed(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)

    summary = verify_production_pilot_acceptance(
        manifest_path,
        trust_policy_path=tmp_path / "trust-policy.json",
        expected_trust_policy_sha256=f"sha256:{'0' * 64}",
        environment={},
        now=_VERIFICATION_TIME,
    )

    assert any(
        error["code"] == "EVIDENCE_ATTESTATION_POLICY_DIGEST_MISMATCH"
        for error in summary["errors"]
    )
    assert summary["production_evidence_verified"] is False


def test_self_asserted_consent_and_review_booleans_do_not_replace_attestations(
    tmp_path: Path,
) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    consent_pointer = manifest["consent_evidence"]
    consent = _load_pointed(tmp_path, consent_pointer)
    del consent["participants"][0]["attestation"]
    _rewrite_evidence(tmp_path, consent_pointer, consent)
    review_pointer = manifest["independent_review"]["evidence"]
    review = _load_pointed(tmp_path, review_pointer)
    del review["attestation"]
    _rewrite_evidence(tmp_path, review_pointer, review)
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path)

    attestation_errors = [
        error
        for error in summary["errors"]
        if error["code"] == "EVIDENCE_ATTESTATION_INVALID_ATTESTATION"
    ]
    assert len(attestation_errors) == 2
    assert summary["production_evidence_verified"] is False


def test_wrong_attestation_role_and_exact_claim_tamper_fail_closed(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    role_pointer = manifest["cases"][0]["baseline_evidence"]
    role_evidence = _load_pointed(tmp_path, role_pointer)
    role_evidence["attestation"]["role"] = EvidenceRole.PILOT_REVIEWER.value
    _rewrite_evidence(tmp_path, role_pointer, role_evidence)

    tamper_pointer = manifest["cases"][1]["harness_evidence"]
    tampered_evidence = _load_pointed(tmp_path, tamper_pointer)
    tampered_evidence["record"]["harness_minutes"] = 11.0
    _rewrite_evidence(tmp_path, tamper_pointer, tampered_evidence)
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path)
    codes = {error["code"] for error in summary["errors"]}

    assert "EVIDENCE_ATTESTATION_ROLE_MISMATCH" in codes
    assert "EVIDENCE_ATTESTATION_CLAIMS_MISMATCH" in codes


def test_expired_measurement_attestation_is_rejected(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    pointer = manifest["cases"][0]["harness_evidence"]
    evidence = _load_pointed(tmp_path, pointer)
    evidence["attestation"] = _attest(
        _attestable_evidence(evidence),
        _engineer_identity(),
        EvidenceRole.PILOT_ENGINEER,
        _ENGINEER_PRIVATE_KEY,
        expires_at=_ATTESTED_AT + timedelta(days=1),
    )
    _rewrite_evidence(tmp_path, pointer, evidence)
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path, now=_ATTESTED_AT + timedelta(days=1))

    assert any(error["code"] == "EVIDENCE_ATTESTATION_EXPIRED" for error in summary["errors"])


@pytest.mark.parametrize("flag", ["synthetic", "simulated", "generated", "development"])
def test_manifest_rejects_every_nonproduction_origin(tmp_path: Path, flag: str) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    manifest[flag] = True
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path)

    assert summary["passed"] is False
    assert summary["production_evidence_verified"] is False
    assert {"code": "DISALLOWED_EVIDENCE_ORIGIN", "field": f"manifest.{flag}"} in summary["errors"]


def test_hash_tampering_fails_without_exposing_artifact_or_hash(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path / "customer-secret-project")
    manifest = _load_manifest(manifest_path)
    pointer = manifest["cases"][0]["baseline_evidence"]
    artifact = manifest_path.parent / pointer["artifact_ref"]
    artifact.write_bytes(artifact.read_bytes() + b" ")

    summary = _verify(manifest_path)
    rendered = canonical_json(summary)

    assert summary["passed"] is False
    assert any(error["code"] == "EVIDENCE_HASH_MISMATCH" for error in summary["errors"])
    assert "customer-secret-project" not in rendered
    assert pointer["artifact_ref"] not in rendered
    assert pointer["sha256"] not in rendered


def test_actual_controlled_source_bytes_are_checked_against_locked_hash(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    case = manifest["cases"][0]
    source = tmp_path / case["drawing_source_artifact_ref"]
    source.write_bytes(source.read_bytes() + b"tampered")

    summary = _verify(manifest_path)

    assert {
        "code": "SOURCE_HASH_MISMATCH",
        "field": "drawing_source_sha256",
        "case_id": "pilot-case-01",
    } in summary["errors"]


@pytest.mark.parametrize("invalid_kind", ["suffix", "header"])
def test_pilot_source_requires_real_dxf_or_dwg_signature(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    case = manifest["cases"][0]
    source = tmp_path / case["drawing_source_artifact_ref"]
    if invalid_kind == "suffix":
        renamed = source.with_suffix(".bin")
        source.rename(renamed)
        case["drawing_source_artifact_ref"] = renamed.relative_to(tmp_path).as_posix()
    else:
        source.write_bytes(b"not-an-ascii-dxf-section")
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path)

    assert {
        "code": "SOURCE_FORMAT_INVALID",
        "field": "drawing_source_artifact_ref",
        "case_id": "pilot-case-01",
    } in summary["errors"]


def test_source_snapshot_reads_once_and_rejects_swap_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    source = (tmp_path / manifest["cases"][0]["drawing_source_artifact_ref"]).resolve()
    real_open = pilot_verifier.os.open
    open_count = 0

    def counting_open(path: str | bytes | Path, flags: int, *args: int) -> int:
        nonlocal open_count
        if Path(path).resolve() == source:
            open_count += 1
        return real_open(path, flags, *args)

    monkeypatch.setattr(pilot_verifier.os, "open", counting_open)
    assert _verify(manifest_path)["passed"] is True
    assert open_count == 1

    replacement = source.with_suffix(".replacement")
    replacement.write_bytes(b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\nreplacement")
    swapped = False

    def swapping_open(path: str | bytes | Path, flags: int, *args: int) -> int:
        nonlocal swapped
        if Path(path).resolve() == source and not swapped:
            swapped = True
            replacement.replace(source)
        return real_open(path, flags, *args)

    monkeypatch.setattr(pilot_verifier.os, "open", swapping_open)
    summary = _verify(manifest_path)

    assert swapped is True
    assert {
        "code": "SOURCE_ARTIFACT_UNSTABLE",
        "field": "drawing_source_artifact_ref",
        "case_id": "pilot-case-01",
    } in summary["errors"]


def test_evidence_snapshot_rejects_swap_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    pointer = manifest["cases"][0]["baseline_evidence"]
    evidence = (tmp_path / pointer["artifact_ref"]).resolve()
    replacement = evidence.with_suffix(".replacement")
    replacement.write_bytes(evidence.read_bytes() + b" ")
    real_open = pilot_verifier.os.open
    swapped = False

    def swapping_open(path: str | bytes | Path, flags: int, *args: int) -> int:
        nonlocal swapped
        if Path(path).resolve() == evidence and not swapped:
            swapped = True
            replacement.replace(evidence)
        return real_open(path, flags, *args)

    monkeypatch.setattr(pilot_verifier.os, "open", swapping_open)
    summary = _verify(manifest_path)

    assert swapped is True
    assert {
        "code": "EVIDENCE_ARTIFACT_UNSTABLE",
        "field": "baseline_evidence.artifact_ref",
        "case_id": "pilot-case-01",
    } in summary["errors"]


def test_hash_locked_evidence_still_rejects_development_origin(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    pointer = manifest["cases"][0]["harness_evidence"]
    evidence = _load_pointed(tmp_path, pointer)
    evidence["development"] = True
    _rewrite_evidence(tmp_path, pointer, evidence)
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path)

    assert {
        "code": "DISALLOWED_EVIDENCE_ORIGIN",
        "field": "harness_evidence.development",
        "case_id": "pilot-case-01",
    } in summary["errors"]


def test_source_hash_binding_and_duplicate_cases_are_independently_checked(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    first = manifest["cases"][0]
    second = manifest["cases"][1]
    second["case_id"] = first["case_id"]
    second["drawing_source_sha256"] = first["drawing_source_sha256"]
    pointer = first["baseline_evidence"]
    evidence = _load_pointed(tmp_path, pointer)
    evidence["drawing_source_sha256"] = "f" * 64
    _rewrite_evidence(tmp_path, pointer, evidence)
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path)
    codes = {error["code"] for error in summary["errors"]}

    assert {"CASE_ID_DUPLICATE", "SOURCE_CASE_DUPLICATE", "SOURCE_HASH_BINDING_MISMATCH"} <= codes


def test_reviewer_must_be_separate_from_every_engineer_participant(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    trust_policy_path = tmp_path / "trust-policy.json"
    trust_policy = _load_manifest(trust_policy_path)
    trust_policy["identities"][0]["allowed_roles"].append(EvidenceRole.PILOT_REVIEWER.value)
    _write_json(trust_policy_path, trust_policy)
    manifest["independent_review"]["reviewer_id"] = _ENGINEER_ID
    pointer = manifest["independent_review"]["evidence"]
    review = _load_pointed(tmp_path, pointer)
    review["reviewer_id"] = _ENGINEER_ID
    dual_role_identity = TrustPolicyIdentity(
        identity_id=_ENGINEER_ID,
        allowed_roles=(EvidenceRole.PILOT_ENGINEER, EvidenceRole.PILOT_REVIEWER),
        public_key=_ENGINEER_PUBLIC_KEY,
    )
    review["attestation"] = _attest(
        _attestable_evidence(review),
        dual_role_identity,
        EvidenceRole.PILOT_REVIEWER,
        _ENGINEER_PRIVATE_KEY,
    )
    _rewrite_evidence(tmp_path, pointer, review)
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path)

    assert {
        "code": "REVIEWER_NOT_INDEPENDENT",
        "field": "independent_review.reviewer_id",
    } in summary["errors"]


def test_consent_must_be_affirmative_hash_locked_and_timezone_stamped(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    pointer = manifest["consent_evidence"]
    consent = _load_pointed(tmp_path, pointer)
    consent["participants"][0]["consent_given"] = False
    consent["participants"][0]["consented_at"] = "2026-07-01"
    _rewrite_evidence(tmp_path, pointer, consent)
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path)

    assert any(error["code"] == "PARTICIPANT_CONSENT_MISSING" for error in summary["errors"])


def test_manual_baseline_interval_must_precede_harness_and_match_duration(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    pointer = manifest["cases"][0]["baseline_evidence"]
    baseline = _load_pointed(tmp_path, pointer)
    baseline["measurement_started_at"] = "2026-07-03T09:30:00+07:00"
    baseline["measurement_ended_at"] = "2026-07-03T11:00:00+07:00"
    _rewrite_evidence(tmp_path, pointer, baseline)
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path)
    codes = {error["code"] for error in summary["errors"]}

    assert "BASELINE_NOT_PRIOR_TO_HARNESS" in codes
    assert "BASELINE_DURATION_EVIDENCE_MISMATCH" in codes


def test_configured_threshold_is_loaded_instead_of_hard_coded(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    policy = yaml.safe_load(Path("config/pilot.yaml").read_text(encoding="utf-8"))
    policy["overall_median_saving"] = 0.95
    policy_path = tmp_path / "stricter-pilot.yaml"
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    _renew_reviewer_attestation(manifest_path, policy_path)

    summary = _verify(manifest_path, policy_path)

    assert summary["production_evidence_verified"] is True
    assert summary["metrics"]["overall_median_saving"] == 0.9
    assert summary["metrics"]["goal_met"] is False
    assert any(
        error["code"] == "PILOT_EFFECTIVENESS_THRESHOLDS_NOT_MET" for error in summary["errors"]
    )


def test_each_case_saving_floor_is_enforced_beyond_collector_medians(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    pointer = manifest["cases"][0]["harness_evidence"]
    effort = _load_pointed(tmp_path, pointer)
    effort["record"]["harness_minutes"] = 60.0
    effort["measurement_ended_at"] = "2026-07-03T09:00:00+07:00"
    _resign_engineer_evidence(effort)
    _rewrite_evidence(tmp_path, pointer, effort)
    _save_manifest(manifest_path, manifest)
    _renew_reviewer_attestation(manifest_path)

    summary = _verify(manifest_path)

    assert summary["production_evidence_verified"] is True
    assert summary["metrics"]["goal_met"] is True
    assert summary["metrics"]["all_cases_meet_saving_floor"] is False
    assert {
        "code": "CASE_SAVING_BELOW_MINIMUM",
        "field": "metrics.saving",
        "case_id": "pilot-case-01",
    } in summary["errors"]


def test_failed_harness_case_remains_in_denominator_with_finite_reason(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    pointer = manifest["cases"][0]["harness_evidence"]
    effort = _load_pointed(tmp_path, pointer)
    effort["record"].update(
        {
            "completed": False,
            "failure_reason": "unsupported_feature",
            "entities_created": 0,
            "first_preview_clean": False,
        }
    )
    _resign_engineer_evidence(effort)
    _rewrite_evidence(tmp_path, pointer, effort)
    _save_manifest(manifest_path, manifest)
    _renew_reviewer_attestation(manifest_path)

    summary = _verify(manifest_path)

    assert summary["production_evidence_verified"] is True
    assert summary["metrics"] is not None
    assert summary["metrics"]["committed_job_rate"] == pytest.approx(14 / 15)
    assert {
        "code": "CASE_SAVING_BELOW_MINIMUM",
        "field": "metrics.saving",
        "case_id": "pilot-case-01",
    } in summary["errors"]


def test_minimum_metric_samples_apply_to_each_required_group(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    case = manifest["cases"][2]
    case["capability_group"] = "B"
    pointer = case["baseline_evidence"]
    baseline = _load_pointed(tmp_path, pointer)
    baseline["record"]["capability_group"] = "B"
    _resign_engineer_evidence(baseline)
    _rewrite_evidence(tmp_path, pointer, baseline)
    _save_manifest(manifest_path, manifest)
    _renew_reviewer_attestation(manifest_path)

    summary = _verify(manifest_path)

    assert summary["group_case_counts"] == {"B": 6, "D": 5, "E": 4}
    assert summary["production_evidence_verified"] is True
    assert summary["metrics"]["goal_met"] is False
    assert any(
        error["code"] == "PILOT_EFFECTIVENESS_THRESHOLDS_NOT_MET" for error in summary["errors"]
    )


def test_duplicate_effort_job_is_rejected_before_aggregation(tmp_path: Path) -> None:
    manifest_path = _build_bundle(tmp_path)
    manifest = _load_manifest(manifest_path)
    pointer = manifest["cases"][1]["harness_evidence"]
    effort = _load_pointed(tmp_path, pointer)
    effort["record"]["job_id"] = "pilot-job-01"
    _rewrite_evidence(tmp_path, pointer, effort)
    _save_manifest(manifest_path, manifest)

    summary = _verify(manifest_path)

    assert any(error["code"] == "EFFORT_JOB_DUPLICATE" for error in summary["errors"])
    assert summary["metrics"] is None


def test_path_traversal_and_missing_manifest_fail_with_canonical_redacted_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = _build_bundle(tmp_path / "private-pilot")
    trust_policy_path = manifest_path.parent / "trust-policy.json"
    policy_pin = _policy_pin(trust_policy_path)
    manifest = _load_manifest(manifest_path)
    manifest["cases"][0]["baseline_evidence"]["artifact_ref"] = "../customer-name.json"
    _save_manifest(manifest_path, manifest)

    cli_args = [
        str(manifest_path),
        "--trust-policy",
        str(trust_policy_path),
        "--trust-policy-sha256",
        policy_pin,
    ]
    assert main(cli_args) == 1
    output = capsys.readouterr().out.strip()
    parsed = json.loads(output)
    assert output == canonical_json(parsed)
    assert "private-pilot" not in output
    assert "customer-name" not in output
    assert any(error["code"] == "EVIDENCE_ARTIFACT_INVALID" for error in parsed["errors"])

    missing = tmp_path / "another-customer" / "missing.json"
    assert (
        main(
            [
                str(missing),
                "--trust-policy",
                str(trust_policy_path),
                "--trust-policy-sha256",
                policy_pin,
            ]
        )
        == 1
    )
    missing_output = capsys.readouterr().out.strip()
    assert str(missing) not in missing_output
    assert json.loads(missing_output)["errors"] == [
        {"code": "MANIFEST_UNREADABLE", "field": "manifest"}
    ]
