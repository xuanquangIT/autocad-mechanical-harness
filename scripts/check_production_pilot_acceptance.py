"""Fail-closed verifier for a measured, human-run production pilot bundle.

The metrics collector proves the arithmetic.  This verifier proves that the inputs
used for a production claim are hash-locked, human measured, consented, independently
reviewed, unique, and explicitly not development or generated evidence.  It only
emits aggregate numbers, opaque case IDs, and stable error codes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from pydantic import ValidationError

from cad_harness.domain.canonical import canonical_json
from cad_harness.domain.models.metrics import (
    BaselineCase,
    EffortRecord,
    FailureReason,
    PilotReport,
    round_minutes,
)
from cad_harness.metrics.collector import (
    CAPABILITY_GROUPS,
    MetricsCollector,
    PilotThresholds,
    load_pilot_thresholds,
)
from cad_harness.security.evidence_attestation import (
    EvidenceAttestation,
    EvidenceAttestationError,
    EvidenceRole,
    EvidenceTrustPolicy,
    JsonValue,
    evidence_attestation_from_mapping,
    trust_policy_from_mapping,
    verify_attestation,
)

type JsonMapping = Mapping[str, Any]
type Error = dict[str, str]

MANIFEST_SCHEMA_VERSION: Final = "1.0"
MAX_MANIFEST_BYTES: Final = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES: Final = 1024 * 1024
MAX_SOURCE_BYTES: Final = 512 * 1024 * 1024
MAX_TRUST_POLICY_BYTES: Final = 256 * 1024
TRUST_POLICY_PATH_ENV: Final = "CAD_HARNESS_EVIDENCE_TRUST_POLICY"
TRUST_POLICY_SHA256_ENV: Final = "CAD_HARNESS_EVIDENCE_TRUST_POLICY_SHA256"
_OPAQUE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_DWG_MAGIC: Final = re.compile(rb"AC10[0-9]{2}")
_DXF_SECTION_SIGNATURE: Final = re.compile(
    rb"\A[ \t\r\n]*0[ \t]*\r?\nSECTION[ \t]*\r?\n2[ \t]*\r?\n(?:HEADER|ENTITIES)[ \t]*\r?\n"
)
_ORIGIN_FLAGS: Final = ("synthetic", "simulated", "generated", "development")
_MANIFEST_KEYS: Final = frozenset(
    {
        "schema_version",
        "evidence_kind",
        "production_evidence",
        *_ORIGIN_FLAGS,
        "pilot_run_id",
        "human_engineer_participants",
        "consent_evidence",
        "independent_review",
        "cases",
    }
)
_PARTICIPANT_KEYS: Final = frozenset({"participant_id", "role", "human"})
_POINTER_KEYS: Final = frozenset({"evidence_ref", "artifact_ref", "sha256"})
_REVIEW_POINTER_KEYS: Final = frozenset({"reviewer_id", "evidence"})
_CASE_KEYS: Final = frozenset(
    {
        "case_id",
        "capability_group",
        "work_label",
        "engineer_selected",
        "engineer_participant_id",
        "manual_measured_by",
        "harness_operated_by",
        "drawing_source_artifact_ref",
        "drawing_source_sha256",
        "baseline_evidence",
        "harness_evidence",
    }
)
_COMMON_EVIDENCE_KEYS: Final = frozenset(
    {
        "schema_version",
        "evidence_kind",
        "production_evidence",
        *_ORIGIN_FLAGS,
        "pilot_run_id",
        "evidence_ref",
    }
)
_CONSENT_KEYS: Final = _COMMON_EVIDENCE_KEYS | {"participants"}
_CONSENT_PARTICIPANT_KEYS: Final = frozenset(
    {"participant_id", "consent_given", "consent_record_ref", "consented_at", "attestation"}
)
_REVIEW_KEYS: Final = _COMMON_EVIDENCE_KEYS | {
    "reviewer_id",
    "reviewer_is_human",
    "independent",
    "attestation_given",
    "attested_at",
    "review_record_ref",
    "reviewed_case_ids",
    "review_scope_sha256",
    "attestation",
}
_CASE_EVIDENCE_KEYS: Final = _COMMON_EVIDENCE_KEYS | {
    "case_id",
    "drawing_source_sha256",
    "recorded_by",
    "measurement_started_at",
    "measurement_ended_at",
    "record",
    "attestation",
}
_BASELINE_RECORD_KEYS: Final = frozenset(
    {
        "pilot_run_id",
        "case_id",
        "capability_group",
        "work_label",
        "manual_minutes",
        "manual_measured_by",
        "manual_measurement_biased",
        "manual_measured_in_single_session",
    }
)
_EFFORT_RECORD_KEYS: Final = frozenset(
    {
        "pilot_run_id",
        "record_id",
        "case_id",
        "job_id",
        "harness_minutes",
        "idle_minutes_excluded",
        "manual_fixup_minutes",
        "spec_change_count",
        "entities_created",
        "entities_manually_edited",
        "first_preview_clean",
        "completed",
        "failure_reason",
    }
)


class _DuplicateJsonKeyError(ValueError):
    """Raised before a duplicate JSON member can silently replace evidence."""


@dataclass(slots=True)
class _ArtifactSnapshot:
    """One descriptor-stable read; consumers never reopen the same artifact."""

    content: bytes
    sha256: str
    metadata: tuple[int, int, int, int, int]
    json_checked: bool = False
    json_mapping: JsonMapping | None = None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _error(code: str, field: str, case_id: str | None = None) -> Error:
    result = {"code": code, "field": field}
    if case_id is not None:
        result["case_id"] = case_id
    return result


def _opaque(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_ID.fullmatch(value) is not None


def _sha(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _safe_case_id(value: object, index: int) -> str:
    return str(value) if _opaque(value) else f"case-{index + 1:03d}"


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & flag)


def _metadata_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _stable_regular_file(metadata: os.stat_result, *, max_bytes: int) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and not _is_reparse(metadata)
        and 0 <= metadata.st_size <= max_bytes
    )


def _snapshot_file(
    path: Path,
    *,
    max_bytes: int,
    cache: dict[str, _ArtifactSnapshot],
) -> _ArtifactSnapshot | None:
    path_key = str(path).casefold()
    cached = cache.get(path_key)
    if cached is not None:
        try:
            current = os.lstat(path)
        except OSError:
            return None
        if (
            not _stable_regular_file(current, max_bytes=max_bytes)
            or _metadata_signature(current) != cached.metadata
        ):
            return None
        return cached

    descriptor = -1
    try:
        before = os.lstat(path)
        if not _stable_regular_file(before, max_bytes=max_bytes):
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        signature = _metadata_signature(before)
        if (
            not _stable_regular_file(opened, max_bytes=max_bytes)
            or _metadata_signature(opened) != signature
        ):
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        after_handle = os.fstat(descriptor)
        after_path = os.lstat(path)
        if (
            not _stable_regular_file(after_handle, max_bytes=max_bytes)
            or not _stable_regular_file(after_path, max_bytes=max_bytes)
            or _metadata_signature(after_handle) != signature
            or _metadata_signature(after_path) != signature
            or total != opened.st_size
        ):
            return None
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
    content = b"".join(chunks)
    snapshot = _ArtifactSnapshot(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        metadata=signature,
    )
    cache[path_key] = snapshot
    return snapshot


def _snapshot_json_mapping(snapshot: _ArtifactSnapshot) -> JsonMapping | None:
    if snapshot.json_checked:
        return snapshot.json_mapping
    try:
        payload = snapshot.content.decode("utf-8")
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        value = None
    snapshot.json_mapping = value if isinstance(value, Mapping) else None
    snapshot.json_checked = True
    return snapshot.json_mapping


def _read_json_mapping(path: Path, *, max_bytes: int) -> JsonMapping | None:
    snapshot = _snapshot_file(path, max_bytes=max_bytes, cache={})
    return None if snapshot is None else _snapshot_json_mapping(snapshot)


def _artifact_path(root: Path, reference: object, *, max_bytes: int) -> Path | None:
    if not isinstance(reference, str) or not reference or "\\" in reference:
        return None
    relative = Path(reference)
    if (
        relative.is_absolute()
        or relative.drive
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return None
    try:
        resolved_root = root.resolve(strict=True)
        current = resolved_root
        for part in relative.parts:
            current /= part
            metadata = os.lstat(current)
            if _is_reparse(metadata) or stat.S_ISLNK(metadata.st_mode):
                return None
        resolved = current.resolve(strict=True)
        metadata = os.lstat(resolved)
    except OSError:
        return None
    if not resolved.is_relative_to(resolved_root) or not stat.S_ISREG(metadata.st_mode):
        return None
    if metadata.st_size > max_bytes:
        return None
    return resolved


def _valid_drawing_source(path: Path, content: bytes) -> bool:
    suffix = path.suffix.casefold()
    if suffix == ".dwg":
        return _DWG_MAGIC.fullmatch(content[:6]) is not None
    if suffix != ".dxf":
        return False
    header = content[:8192]
    if b"\x00" in header:
        return False
    return _DXF_SECTION_SIGNATURE.match(header) is not None


def _exact_keys(
    value: JsonMapping,
    allowed: frozenset[str],
    *,
    field: str,
    errors: list[Error],
    case_id: str | None = None,
) -> None:
    for missing in sorted(allowed - value.keys()):
        errors.append(_error("REQUIRED_FIELD_MISSING", f"{field}.{missing}", case_id))
    if value.keys() - allowed:
        # Unknown member names can themselves contain customer data, so never echo them.
        errors.append(_error("UNEXPECTED_FIELD", field, case_id))


def _production_flags(
    value: JsonMapping, *, field: str, errors: list[Error], case_id: str | None = None
) -> None:
    if value.get("production_evidence") is not True:
        errors.append(_error("NOT_PRODUCTION_EVIDENCE", f"{field}.production_evidence", case_id))
    for flag in _ORIGIN_FLAGS:
        if value.get(flag) is not False:
            errors.append(_error("DISALLOWED_EVIDENCE_ORIGIN", f"{field}.{flag}", case_id))


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _load_trust_policy(
    trust_policy_path: Path | None,
    expected_policy_sha256: str | None,
    environment: Mapping[str, str],
    errors: list[Error],
) -> tuple[EvidenceTrustPolicy | None, str | None]:
    policy_path = trust_policy_path
    if policy_path is None:
        configured_path = environment.get(TRUST_POLICY_PATH_ENV)
        if not configured_path:
            errors.append(_error("TRUST_POLICY_REQUIRED", "trust_policy"))
            return None, None
        policy_path = Path(configured_path)
    raw_policy = _read_json_mapping(policy_path, max_bytes=MAX_TRUST_POLICY_BYTES)
    if raw_policy is None:
        errors.append(_error("TRUST_POLICY_UNREADABLE", "trust_policy"))
        return None, None
    try:
        policy = trust_policy_from_mapping(dict(raw_policy))
    except EvidenceAttestationError as exc:
        errors.append(_error(exc.code.value, "trust_policy"))
        return None, None
    policy_sha256 = expected_policy_sha256 or environment.get(TRUST_POLICY_SHA256_ENV)
    if not policy_sha256:
        errors.append(_error("EVIDENCE_ATTESTATION_POLICY_DIGEST_MISSING", "trust_policy_sha256"))
        return policy, None
    return policy, policy_sha256


def _claims_without_attestation(evidence: JsonMapping) -> JsonValue:
    return cast(JsonValue, {key: value for key, value in evidence.items() if key != "attestation"})


def _consent_attestation_claims(evidence: JsonMapping) -> JsonValue:
    claims = dict(evidence)
    participants = evidence.get("participants")
    if isinstance(participants, list):
        claims["participants"] = [
            (
                {key: value for key, value in entry.items() if key != "attestation"}
                if isinstance(entry, Mapping)
                else entry
            )
            for entry in participants
        ]
    return cast(JsonValue, claims)


def _verify_evidence_attestation(
    raw_attestation: object,
    *,
    claims: JsonValue,
    expected_identity_id: object,
    expected_role: EvidenceRole,
    trust_policy: EvidenceTrustPolicy | None,
    expected_policy_sha256: str | None,
    now: datetime,
    field: str,
    errors: list[Error],
    case_id: str | None = None,
) -> EvidenceAttestation | None:
    if trust_policy is None or expected_policy_sha256 is None:
        return None
    try:
        attestation = evidence_attestation_from_mapping(raw_attestation)
    except EvidenceAttestationError as exc:
        errors.append(_error(exc.code.value, field, case_id))
        return None
    if attestation.identity_id != expected_identity_id:
        errors.append(_error("EVIDENCE_ATTESTATION_IDENTITY_MISMATCH", field, case_id))
        return None
    if attestation.role is not expected_role:
        errors.append(_error("EVIDENCE_ATTESTATION_ROLE_MISMATCH", field, case_id))
        return None
    try:
        verify_attestation(
            trust_policy,
            attestation,
            claims,
            expected_policy_sha256=expected_policy_sha256,
            now=now,
        )
    except EvidenceAttestationError as exc:
        errors.append(_error(exc.code.value, field, case_id))
        return None
    return attestation


def _locked_evidence(
    root: Path,
    pointer_value: object,
    *,
    field: str,
    errors: list[Error],
    seen_refs: set[str],
    seen_paths: set[str],
    snapshots: dict[str, _ArtifactSnapshot],
    case_id: str | None = None,
) -> JsonMapping | None:
    if not isinstance(pointer_value, Mapping):
        errors.append(_error("EVIDENCE_POINTER_MISSING", field, case_id))
        return None
    pointer: JsonMapping = pointer_value
    _exact_keys(pointer, _POINTER_KEYS, field=field, errors=errors, case_id=case_id)
    evidence_ref = pointer.get("evidence_ref")
    if not _opaque(evidence_ref):
        errors.append(_error("EVIDENCE_REF_INVALID", f"{field}.evidence_ref", case_id))
    elif evidence_ref in seen_refs:
        errors.append(_error("EVIDENCE_REF_DUPLICATE", f"{field}.evidence_ref", case_id))
    else:
        seen_refs.add(str(evidence_ref))
    expected_hash = pointer.get("sha256")
    if not _sha(expected_hash):
        errors.append(_error("EVIDENCE_HASH_INVALID", f"{field}.sha256", case_id))
    path = _artifact_path(root, pointer.get("artifact_ref"), max_bytes=MAX_EVIDENCE_BYTES)
    if path is None:
        errors.append(_error("EVIDENCE_ARTIFACT_INVALID", f"{field}.artifact_ref", case_id))
        return None
    path_key = str(path).casefold()
    if path_key in seen_paths:
        errors.append(_error("EVIDENCE_ARTIFACT_REUSED", f"{field}.artifact_ref", case_id))
    else:
        seen_paths.add(path_key)
    snapshot = _snapshot_file(path, max_bytes=MAX_EVIDENCE_BYTES, cache=snapshots)
    if snapshot is None:
        errors.append(_error("EVIDENCE_ARTIFACT_UNSTABLE", f"{field}.artifact_ref", case_id))
        return None
    if _sha(expected_hash) and snapshot.sha256 != expected_hash:
        errors.append(_error("EVIDENCE_HASH_MISMATCH", f"{field}.sha256", case_id))
        return None
    evidence = _snapshot_json_mapping(snapshot)
    if evidence is None:
        errors.append(_error("EVIDENCE_UNREADABLE", field, case_id))
    return evidence


def _evidence_header(
    evidence: JsonMapping,
    *,
    allowed_keys: frozenset[str],
    expected_kind: str,
    expected_ref: object,
    pilot_run_id: object,
    field: str,
    errors: list[Error],
    case_id: str | None = None,
) -> None:
    _exact_keys(evidence, allowed_keys, field=field, errors=errors, case_id=case_id)
    if evidence.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(_error("EVIDENCE_SCHEMA_UNSUPPORTED", f"{field}.schema_version", case_id))
    if evidence.get("evidence_kind") != expected_kind:
        errors.append(_error("EVIDENCE_KIND_INVALID", f"{field}.evidence_kind", case_id))
    if evidence.get("evidence_ref") != expected_ref:
        errors.append(_error("EVIDENCE_REF_MISMATCH", f"{field}.evidence_ref", case_id))
    if evidence.get("pilot_run_id") != pilot_run_id:
        errors.append(_error("PILOT_RUN_MISMATCH", f"{field}.pilot_run_id", case_id))
    _production_flags(evidence, field=field, errors=errors, case_id=case_id)


def _validate_participants(manifest: JsonMapping, errors: list[Error]) -> set[str]:
    raw_participants = manifest.get("human_engineer_participants")
    if not isinstance(raw_participants, list) or not raw_participants:
        errors.append(_error("HUMAN_PARTICIPANTS_MISSING", "human_engineer_participants"))
        return set()
    participant_ids: set[str] = set()
    for index, raw_participant in enumerate(raw_participants):
        field = f"human_engineer_participants[{index}]"
        if not isinstance(raw_participant, Mapping):
            errors.append(_error("PARTICIPANT_INVALID", field))
            continue
        participant: JsonMapping = raw_participant
        _exact_keys(participant, _PARTICIPANT_KEYS, field=field, errors=errors)
        participant_id = participant.get("participant_id")
        if not _opaque(participant_id):
            errors.append(_error("PARTICIPANT_ID_INVALID", f"{field}.participant_id"))
        elif participant_id in participant_ids:
            errors.append(_error("PARTICIPANT_ID_DUPLICATE", f"{field}.participant_id"))
        else:
            participant_ids.add(str(participant_id))
        if participant.get("role") != "mechanical_engineer" or participant.get("human") is not True:
            errors.append(_error("HUMAN_ENGINEER_EVIDENCE_MISSING", field))
    return participant_ids


def _validate_consent(
    evidence: JsonMapping | None,
    *,
    pointer: object,
    pilot_run_id: object,
    participant_ids: set[str],
    trust_policy: EvidenceTrustPolicy | None,
    expected_policy_sha256: str | None,
    now: datetime,
    errors: list[Error],
) -> dict[str, datetime]:
    consented_at_by_participant: dict[str, datetime] = {}
    if evidence is None:
        return consented_at_by_participant
    expected_ref = pointer.get("evidence_ref") if isinstance(pointer, Mapping) else None
    _evidence_header(
        evidence,
        allowed_keys=_CONSENT_KEYS,
        expected_kind="human_participant_consent",
        expected_ref=expected_ref,
        pilot_run_id=pilot_run_id,
        field="consent_evidence",
        errors=errors,
    )
    raw_entries = evidence.get("participants")
    if not isinstance(raw_entries, list):
        errors.append(_error("CONSENT_PARTICIPANTS_MISSING", "consent_evidence.participants"))
        return consented_at_by_participant
    consent_ids: set[str] = set()
    for index, raw_entry in enumerate(raw_entries):
        field = f"consent_evidence.participants[{index}]"
        if not isinstance(raw_entry, Mapping):
            errors.append(_error("CONSENT_RECORD_INVALID", field))
            continue
        entry: JsonMapping = raw_entry
        _exact_keys(entry, _CONSENT_PARTICIPANT_KEYS, field=field, errors=errors)
        participant_id = entry.get("participant_id")
        if not _opaque(participant_id):
            errors.append(_error("PARTICIPANT_ID_INVALID", f"{field}.participant_id"))
        elif participant_id in consent_ids:
            errors.append(_error("CONSENT_RECORD_DUPLICATE", f"{field}.participant_id"))
        else:
            consent_ids.add(str(participant_id))
        consented_at = _timestamp(entry.get("consented_at"))
        if (
            entry.get("consent_given") is not True
            or not _opaque(entry.get("consent_record_ref"))
            or consented_at is None
        ):
            errors.append(_error("PARTICIPANT_CONSENT_MISSING", field))
        elif _opaque(participant_id):
            consented_at_by_participant[str(participant_id)] = consented_at
        attestation = _verify_evidence_attestation(
            entry.get("attestation"),
            claims=_consent_attestation_claims(evidence),
            expected_identity_id=participant_id,
            expected_role=EvidenceRole.PILOT_ENGINEER,
            trust_policy=trust_policy,
            expected_policy_sha256=expected_policy_sha256,
            now=now,
            field=f"{field}.attestation",
            errors=errors,
        )
        if (
            consented_at is not None
            and attestation is not None
            and attestation.issued_at < consented_at
        ):
            errors.append(_error("CONSENT_ATTESTATION_CHRONOLOGY_INVALID", field))
    if consent_ids != participant_ids:
        errors.append(_error("CONSENT_PARTICIPANT_SET_MISMATCH", "consent_evidence.participants"))
    return consented_at_by_participant


def _case_record(
    evidence: JsonMapping | None,
    *,
    pointer: object,
    evidence_kind: str,
    pilot_run_id: object,
    case_id: str,
    drawing_source_sha256: object,
    recorded_by: object,
    trust_policy: EvidenceTrustPolicy | None,
    expected_policy_sha256: str | None,
    now: datetime,
    errors: list[Error],
) -> JsonMapping | None:
    if evidence is None:
        return None
    expected_ref = pointer.get("evidence_ref") if isinstance(pointer, Mapping) else None
    field = "baseline_evidence" if evidence_kind == "human_manual_baseline" else "harness_evidence"
    _evidence_header(
        evidence,
        allowed_keys=_CASE_EVIDENCE_KEYS,
        expected_kind=evidence_kind,
        expected_ref=expected_ref,
        pilot_run_id=pilot_run_id,
        field=field,
        errors=errors,
        case_id=case_id,
    )
    for key, expected in (
        ("case_id", case_id),
        ("drawing_source_sha256", drawing_source_sha256),
        ("recorded_by", recorded_by),
    ):
        if evidence.get(key) != expected:
            code = (
                "SOURCE_HASH_BINDING_MISMATCH"
                if key == "drawing_source_sha256"
                else "EVIDENCE_RECORD_MISMATCH"
            )
            errors.append(_error(code, f"{field}.{key}", case_id))
    _verify_evidence_attestation(
        evidence.get("attestation"),
        claims=_claims_without_attestation(evidence),
        expected_identity_id=recorded_by,
        expected_role=EvidenceRole.PILOT_ENGINEER,
        trust_policy=trust_policy,
        expected_policy_sha256=expected_policy_sha256,
        now=now,
        field=f"{field}.attestation",
        errors=errors,
        case_id=case_id,
    )
    record = evidence.get("record")
    if not isinstance(record, Mapping):
        errors.append(_error("EVIDENCE_RECORD_MISSING", f"{field}.record", case_id))
        return None
    return record


def _measurement_interval(
    evidence: JsonMapping | None,
    *,
    field: str,
    errors: list[Error],
    case_id: str,
) -> tuple[datetime, datetime] | None:
    if evidence is None:
        return None
    started_at = _timestamp(evidence.get("measurement_started_at"))
    ended_at = _timestamp(evidence.get("measurement_ended_at"))
    if started_at is None or ended_at is None or ended_at < started_at:
        errors.append(_error("MEASUREMENT_INTERVAL_INVALID", field, case_id))
        return None
    return started_at, ended_at


def _tenth_precision(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and round_minutes(float(value)) == float(value)
    )


def _validate_consent_chronology(
    consented_at_by_participant: Mapping[str, datetime],
    *,
    participant_id: object,
    measurement_interval: tuple[datetime, datetime] | None,
    field: str,
    errors: list[Error],
    case_id: str,
) -> None:
    if measurement_interval is None or not isinstance(participant_id, str):
        return
    consented_at = consented_at_by_participant.get(participant_id)
    if consented_at is None or consented_at > measurement_interval[0]:
        errors.append(_error("CONSENT_AFTER_MEASUREMENT_START", field, case_id))


def _validate_harness_duration(
    effort: EffortRecord | None,
    measurement_interval: tuple[datetime, datetime] | None,
    *,
    errors: list[Error],
    case_id: str,
) -> None:
    if effort is None or measurement_interval is None:
        return
    field = "harness_evidence.measurement_interval"
    elapsed_minutes = (measurement_interval[1] - measurement_interval[0]).total_seconds() / 60.0
    if not _tenth_precision(elapsed_minutes):
        errors.append(_error("HARNESS_DURATION_PRECISION_INVALID", field, case_id))
        return
    active_event_minutes = effort.harness_minutes - effort.manual_fixup_minutes
    if active_event_minutes < 0.0:
        errors.append(_error("HARNESS_ACTIVE_DURATION_NEGATIVE", field, case_id))
        return
    expected_elapsed = round_minutes(active_event_minutes + effort.idle_minutes_excluded)
    if elapsed_minutes != expected_elapsed:
        errors.append(_error("HARNESS_DURATION_EVIDENCE_MISMATCH", field, case_id))


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _review_scope_sha256(
    *,
    pilot_run_id: str,
    case_artifact_bindings: Sequence[JsonMapping],
    metrics: Mapping[str, Any],
    thresholds: PilotThresholds,
) -> str:
    return _canonical_sha256(
        {
            "aggregate_metrics": dict(metrics),
            "case_artifact_bindings": [dict(binding) for binding in case_artifact_bindings],
            "pilot_run_id": pilot_run_id,
            "threshold_policy_sha256": _canonical_sha256(thresholds.model_dump(mode="json")),
        }
    )


def _validate_review(
    evidence: JsonMapping | None,
    *,
    pointer: object,
    pilot_run_id: object,
    reviewer_id: object,
    participant_ids: set[str],
    case_ids: set[str],
    expected_review_scope_sha256: str | None,
    latest_measurement_end: datetime | None,
    trust_policy: EvidenceTrustPolicy | None,
    expected_policy_sha256: str | None,
    now: datetime,
    errors: list[Error],
) -> None:
    if not _opaque(reviewer_id):
        errors.append(_error("REVIEWER_ID_INVALID", "independent_review.reviewer_id"))
    elif reviewer_id in participant_ids:
        errors.append(_error("REVIEWER_NOT_INDEPENDENT", "independent_review.reviewer_id"))
    if evidence is None:
        return
    expected_ref = pointer.get("evidence_ref") if isinstance(pointer, Mapping) else None
    _evidence_header(
        evidence,
        allowed_keys=_REVIEW_KEYS,
        expected_kind="independent_human_review",
        expected_ref=expected_ref,
        pilot_run_id=pilot_run_id,
        field="independent_review.evidence",
        errors=errors,
    )
    if evidence.get("reviewer_id") != reviewer_id:
        errors.append(_error("REVIEWER_ID_MISMATCH", "independent_review.evidence.reviewer_id"))
    attested_at = _timestamp(evidence.get("attested_at"))
    if (
        evidence.get("reviewer_is_human") is not True
        or evidence.get("independent") is not True
        or evidence.get("attestation_given") is not True
        or not _opaque(evidence.get("review_record_ref"))
        or attested_at is None
    ):
        errors.append(_error("REVIEW_ATTESTATION_MISSING", "independent_review.evidence"))
    if latest_measurement_end is not None and (
        attested_at is None or attested_at < latest_measurement_end
    ):
        errors.append(_error("REVIEW_CHRONOLOGY_INVALID", "independent_review.evidence"))
    review_scope = evidence.get("review_scope_sha256")
    if not _sha(review_scope):
        errors.append(
            _error("REVIEW_SCOPE_DIGEST_INVALID", "independent_review.evidence.review_scope_sha256")
        )
    elif expected_review_scope_sha256 is None or review_scope != expected_review_scope_sha256:
        errors.append(
            _error(
                "REVIEW_SCOPE_DIGEST_MISMATCH",
                "independent_review.evidence.review_scope_sha256",
            )
        )
    attestation = _verify_evidence_attestation(
        evidence.get("attestation"),
        claims=_claims_without_attestation(evidence),
        expected_identity_id=reviewer_id,
        expected_role=EvidenceRole.PILOT_REVIEWER,
        trust_policy=trust_policy,
        expected_policy_sha256=expected_policy_sha256,
        now=now,
        field="independent_review.evidence.attestation",
        errors=errors,
    )
    if attested_at is not None and attestation is not None and attestation.issued_at < attested_at:
        errors.append(
            _error("REVIEW_ATTESTATION_CHRONOLOGY_INVALID", "independent_review.evidence")
        )
    reviewed = evidence.get("reviewed_case_ids")
    if not isinstance(reviewed, list) or any(not _opaque(item) for item in reviewed):
        errors.append(
            _error("REVIEWED_CASE_SET_INVALID", "independent_review.evidence.reviewed_case_ids")
        )
        return
    reviewed_ids = {str(item) for item in reviewed}
    if len(reviewed_ids) != len(reviewed) or reviewed_ids != case_ids:
        errors.append(
            _error("REVIEWED_CASE_SET_MISMATCH", "independent_review.evidence.reviewed_case_ids")
        )


def _sorted_errors(errors: Sequence[Error]) -> list[Error]:
    unique = {(item["code"], item["field"], item.get("case_id")): item for item in errors}
    return sorted(
        unique.values(),
        key=lambda item: (item.get("case_id", ""), item["code"], item["field"]),
    )


def _summary(
    *,
    passed: bool,
    evidence_verified: bool,
    case_count: int,
    participant_count: int,
    group_counts: Mapping[str, int],
    metrics: Mapping[str, Any] | None,
    errors: Sequence[Error],
) -> dict[str, Any]:
    return {
        "passed": passed,
        "production_evidence_verified": evidence_verified,
        "case_count": case_count,
        "participant_count": participant_count,
        "group_case_counts": {group: group_counts.get(group, 0) for group in CAPABILITY_GROUPS},
        "metrics": metrics,
        "errors": _sorted_errors(errors),
    }


def _report_metrics(
    report: PilotReport, thresholds: PilotThresholds
) -> tuple[dict[str, Any], list[Error]]:
    errors: list[Error] = []
    all_cases_meet_floor = True
    for case in report.cases:
        if case.saving < thresholds.minimum_case_saving:
            all_cases_meet_floor = False
            errors.append(_error("CASE_SAVING_BELOW_MINIMUM", "metrics.saving", case.case_id))
    if not report.goal_met:
        errors.append(_error("PILOT_EFFECTIVENESS_THRESHOLDS_NOT_MET", "metrics.goal_met"))
    if not report.quality_gates_met:
        errors.append(_error("PILOT_QUALITY_THRESHOLDS_NOT_MET", "metrics.quality_gates_met"))
    acceptance_met = report.pilot_acceptance_met and all_cases_meet_floor
    group_values = {
        metric.name.removeprefix("median_saving_"): metric.value for metric in report.group_savings
    }
    metrics = {
        "overall_median_saving": report.overall_saving.value,
        "group_median_saving": group_values,
        "first_preview_clean_rate": report.first_preview_clean_rate.value,
        "median_spec_changes": report.median_spec_changes.value,
        "manual_entity_edit_rate": report.manual_entity_edit_rate.value,
        "committed_job_rate": report.committed_job_rate.value,
        "goal_met": report.goal_met,
        "quality_gates_met": report.quality_gates_met,
        "all_cases_meet_saving_floor": all_cases_meet_floor,
        "pilot_acceptance_met": acceptance_met,
    }
    return metrics, errors


def verify_production_pilot_acceptance(
    manifest_path: Path,
    thresholds_path: Path = Path("config/pilot.yaml"),
    trust_policy_path: Path | None = None,
    *,
    expected_trust_policy_sha256: str | None = None,
    environment: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify one explicit local evidence bundle without exposing paths or identities."""
    errors: list[Error] = []
    try:
        thresholds = load_pilot_thresholds(thresholds_path)
    except (OSError, UnicodeError, ValueError, TypeError):
        thresholds = None
        errors.append(_error("PILOT_POLICY_UNREADABLE", "thresholds"))
    manifest = _read_json_mapping(manifest_path, max_bytes=MAX_MANIFEST_BYTES)
    if manifest is None:
        errors.append(_error("MANIFEST_UNREADABLE", "manifest"))
        return _summary(
            passed=False,
            evidence_verified=False,
            case_count=0,
            participant_count=0,
            group_counts={},
            metrics=None,
            errors=errors,
        )

    active_environment = environment if environment is not None else os.environ
    verification_time = now or datetime.now(UTC)
    trust_policy, pinned_trust_policy_sha256 = _load_trust_policy(
        trust_policy_path,
        expected_trust_policy_sha256,
        active_environment,
        errors,
    )

    _exact_keys(manifest, _MANIFEST_KEYS, field="manifest", errors=errors)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(_error("MANIFEST_SCHEMA_UNSUPPORTED", "schema_version"))
    if manifest.get("evidence_kind") != "production_pilot":
        errors.append(_error("MANIFEST_KIND_INVALID", "evidence_kind"))
    _production_flags(manifest, field="manifest", errors=errors)
    pilot_run_id = manifest.get("pilot_run_id")
    if not _opaque(pilot_run_id):
        errors.append(_error("PILOT_RUN_ID_INVALID", "pilot_run_id"))

    participant_ids = _validate_participants(manifest, errors)
    root = manifest_path.parent
    seen_evidence_refs: set[str] = set()
    seen_evidence_paths: set[str] = set()
    snapshots: dict[str, _ArtifactSnapshot] = {}
    consent_pointer = manifest.get("consent_evidence")
    consent = _locked_evidence(
        root,
        consent_pointer,
        field="consent_evidence",
        errors=errors,
        seen_refs=seen_evidence_refs,
        seen_paths=seen_evidence_paths,
        snapshots=snapshots,
    )
    consented_at_by_participant = _validate_consent(
        consent,
        pointer=consent_pointer,
        pilot_run_id=pilot_run_id,
        participant_ids=participant_ids,
        trust_policy=trust_policy,
        expected_policy_sha256=pinned_trust_policy_sha256,
        now=verification_time,
        errors=errors,
    )

    raw_cases = manifest.get("cases")
    cases = raw_cases if isinstance(raw_cases, list) else []
    if not isinstance(raw_cases, list):
        errors.append(_error("CASES_MISSING", "cases"))
    if thresholds is not None and len(cases) < thresholds.minimum_baseline_cases:
        errors.append(_error("BASELINE_CASE_COUNT_TOO_LOW", "cases"))

    baselines: list[BaselineCase] = []
    efforts: list[EffortRecord] = []
    case_ids: set[str] = set()
    source_hashes: set[str] = set()
    source_paths: set[str] = set()
    job_ids: set[str] = set()
    record_ids: set[str] = set()
    group_counts: Counter[str] = Counter()
    case_artifact_bindings: list[JsonMapping] = []
    latest_measurement_end: datetime | None = None
    for index, raw_case in enumerate(cases):
        case_id = _safe_case_id(
            raw_case.get("case_id") if isinstance(raw_case, Mapping) else None, index
        )
        if not isinstance(raw_case, Mapping):
            errors.append(_error("CASE_INVALID", "case", case_id))
            continue
        case: JsonMapping = raw_case
        _exact_keys(case, _CASE_KEYS, field="case", errors=errors, case_id=case_id)
        raw_case_id = case.get("case_id")
        duplicate_case = False
        if not _opaque(raw_case_id):
            errors.append(_error("CASE_ID_INVALID", "case_id", case_id))
        elif raw_case_id in case_ids:
            duplicate_case = True
            errors.append(_error("CASE_ID_DUPLICATE", "case_id", case_id))
        else:
            case_ids.add(str(raw_case_id))
        if case.get("engineer_selected") is not True:
            errors.append(_error("ENGINEER_SELECTION_MISSING", "engineer_selected", case_id))
        group = case.get("capability_group")
        if group not in CAPABILITY_GROUPS:
            errors.append(_error("CAPABILITY_GROUP_INVALID", "capability_group", case_id))
        else:
            group_counts[str(group)] += 1
        if case.get("work_label") not in {"ve_moi", "sua_ban_co_san"}:
            errors.append(_error("WORK_LABEL_INVALID", "work_label", case_id))
        for field in (
            "engineer_participant_id",
            "manual_measured_by",
            "harness_operated_by",
        ):
            identity = case.get(field)
            if not _opaque(identity) or identity not in participant_ids:
                errors.append(_error("HUMAN_PARTICIPANT_MISMATCH", field, case_id))

        drawing_source_sha256 = case.get("drawing_source_sha256")
        source_digest: str | None = None
        if not _sha(drawing_source_sha256):
            errors.append(_error("SOURCE_HASH_INVALID", "drawing_source_sha256", case_id))
        elif drawing_source_sha256 in source_hashes:
            errors.append(_error("SOURCE_CASE_DUPLICATE", "drawing_source_sha256", case_id))
        else:
            source_hashes.add(str(drawing_source_sha256))
        source_path = _artifact_path(
            root,
            case.get("drawing_source_artifact_ref"),
            max_bytes=MAX_SOURCE_BYTES,
        )
        if source_path is None:
            errors.append(_error("SOURCE_ARTIFACT_INVALID", "drawing_source_artifact_ref", case_id))
        else:
            source_path_key = str(source_path).casefold()
            manifest_path_key = str(manifest_path.resolve()).casefold()
            if (
                source_path_key in source_paths
                or source_path_key in seen_evidence_paths
                or source_path_key == manifest_path_key
            ):
                errors.append(
                    _error("SOURCE_ARTIFACT_REUSED", "drawing_source_artifact_ref", case_id)
                )
            else:
                source_paths.add(source_path_key)
                seen_evidence_paths.add(source_path_key)
            source_snapshot = _snapshot_file(
                source_path,
                max_bytes=MAX_SOURCE_BYTES,
                cache=snapshots,
            )
            if source_snapshot is None:
                errors.append(
                    _error("SOURCE_ARTIFACT_UNSTABLE", "drawing_source_artifact_ref", case_id)
                )
            else:
                source_digest = source_snapshot.sha256
                if _sha(drawing_source_sha256) and source_snapshot.sha256 != drawing_source_sha256:
                    errors.append(_error("SOURCE_HASH_MISMATCH", "drawing_source_sha256", case_id))
                if not _valid_drawing_source(source_path, source_snapshot.content):
                    errors.append(
                        _error("SOURCE_FORMAT_INVALID", "drawing_source_artifact_ref", case_id)
                    )

        baseline_pointer = case.get("baseline_evidence")
        baseline_evidence = _locked_evidence(
            root,
            baseline_pointer,
            field="baseline_evidence",
            errors=errors,
            seen_refs=seen_evidence_refs,
            seen_paths=seen_evidence_paths,
            snapshots=snapshots,
            case_id=case_id,
        )
        baseline_record = _case_record(
            baseline_evidence,
            pointer=baseline_pointer,
            evidence_kind="human_manual_baseline",
            pilot_run_id=pilot_run_id,
            case_id=case_id,
            drawing_source_sha256=drawing_source_sha256,
            recorded_by=case.get("manual_measured_by"),
            trust_policy=trust_policy,
            expected_policy_sha256=pinned_trust_policy_sha256,
            now=verification_time,
            errors=errors,
        )
        baseline_interval = _measurement_interval(
            baseline_evidence,
            field="baseline_evidence.measurement_interval",
            errors=errors,
            case_id=case_id,
        )
        baseline: BaselineCase | None = None
        if baseline_record is not None:
            _exact_keys(
                baseline_record,
                _BASELINE_RECORD_KEYS,
                field="baseline_evidence.record",
                errors=errors,
                case_id=case_id,
            )
            try:
                baseline = BaselineCase.model_validate(baseline_record, strict=True)
            except ValidationError:
                errors.append(
                    _error("BASELINE_RECORD_INVALID", "baseline_evidence.record", case_id)
                )
            if baseline is not None:
                if (
                    baseline.pilot_run_id != pilot_run_id
                    or baseline.case_id != case_id
                    or baseline.capability_group != group
                    or baseline.work_label != case.get("work_label")
                    or baseline.manual_measured_by != case.get("manual_measured_by")
                ):
                    errors.append(
                        _error("EVIDENCE_RECORD_MISMATCH", "baseline_evidence.record", case_id)
                    )
                if baseline.manual_measurement_biased:
                    errors.append(
                        _error("BASELINE_MEASUREMENT_BIASED", "baseline_evidence.record", case_id)
                    )
                if not baseline.manual_measured_in_single_session:
                    errors.append(
                        _error("BASELINE_SESSION_INVALID", "baseline_evidence.record", case_id)
                    )
                if (
                    thresholds is not None
                    and baseline.manual_minutes < thresholds.minimum_manual_minutes
                ):
                    errors.append(
                        _error("BASELINE_DURATION_TOO_LOW", "baseline_evidence.record", case_id)
                    )

        effort_pointer = case.get("harness_evidence")
        effort_evidence = _locked_evidence(
            root,
            effort_pointer,
            field="harness_evidence",
            errors=errors,
            seen_refs=seen_evidence_refs,
            seen_paths=seen_evidence_paths,
            snapshots=snapshots,
            case_id=case_id,
        )
        effort_record = _case_record(
            effort_evidence,
            pointer=effort_pointer,
            evidence_kind="human_harness_effort",
            pilot_run_id=pilot_run_id,
            case_id=case_id,
            drawing_source_sha256=drawing_source_sha256,
            recorded_by=case.get("harness_operated_by"),
            trust_policy=trust_policy,
            expected_policy_sha256=pinned_trust_policy_sha256,
            now=verification_time,
            errors=errors,
        )
        effort_interval = _measurement_interval(
            effort_evidence,
            field="harness_evidence.measurement_interval",
            errors=errors,
            case_id=case_id,
        )
        effort: EffortRecord | None = None
        if effort_record is not None:
            _exact_keys(
                effort_record,
                _EFFORT_RECORD_KEYS,
                field="harness_evidence.record",
                errors=errors,
                case_id=case_id,
            )
            if any(
                not _tenth_precision(effort_record.get(field))
                for field in (
                    "harness_minutes",
                    "idle_minutes_excluded",
                    "manual_fixup_minutes",
                )
            ):
                errors.append(
                    _error("EFFORT_PRECISION_INVALID", "harness_evidence.record", case_id)
                )
            strict_effort_record = dict(effort_record)
            failure_reason = strict_effort_record.get("failure_reason")
            if isinstance(failure_reason, str):
                with suppress(ValueError):
                    strict_effort_record["failure_reason"] = FailureReason(failure_reason)
            try:
                effort = EffortRecord.model_validate(strict_effort_record, strict=True)
            except ValidationError:
                errors.append(_error("EFFORT_RECORD_INVALID", "harness_evidence.record", case_id))
            if effort is not None:
                if effort.pilot_run_id != pilot_run_id or effort.case_id != case_id:
                    errors.append(
                        _error("EVIDENCE_RECORD_MISMATCH", "harness_evidence.record", case_id)
                    )
                if effort.record_id is None:
                    errors.append(
                        _error("EFFORT_RECORD_ID_MISSING", "harness_evidence.record_id", case_id)
                    )
                elif effort.record_id in record_ids:
                    errors.append(
                        _error("EFFORT_RECORD_ID_DUPLICATE", "harness_evidence.record_id", case_id)
                    )
                else:
                    record_ids.add(effort.record_id)
                if effort.job_id in job_ids:
                    errors.append(
                        _error("EFFORT_JOB_DUPLICATE", "harness_evidence.job_id", case_id)
                    )
                else:
                    job_ids.add(effort.job_id)

        _validate_consent_chronology(
            consented_at_by_participant,
            participant_id=case.get("manual_measured_by"),
            measurement_interval=baseline_interval,
            field="baseline_evidence.measurement_started_at",
            errors=errors,
            case_id=case_id,
        )
        _validate_consent_chronology(
            consented_at_by_participant,
            participant_id=case.get("harness_operated_by"),
            measurement_interval=effort_interval,
            field="harness_evidence.measurement_started_at",
            errors=errors,
            case_id=case_id,
        )
        _validate_harness_duration(
            effort,
            effort_interval,
            errors=errors,
            case_id=case_id,
        )
        for interval in (baseline_interval, effort_interval):
            if interval is not None and (
                latest_measurement_end is None or interval[1] > latest_measurement_end
            ):
                latest_measurement_end = interval[1]

        if baseline_interval is not None and effort_interval is not None:
            if baseline_interval[1] > effort_interval[0]:
                errors.append(
                    _error(
                        "BASELINE_NOT_PRIOR_TO_HARNESS",
                        "baseline_evidence.measurement_interval",
                        case_id,
                    )
                )
            if baseline is not None:
                observed_manual_minutes = round_minutes(
                    (baseline_interval[1] - baseline_interval[0]).total_seconds() / 60.0
                )
                if observed_manual_minutes != baseline.manual_minutes:
                    errors.append(
                        _error(
                            "BASELINE_DURATION_EVIDENCE_MISMATCH",
                            "baseline_evidence.measurement_interval",
                            case_id,
                        )
                    )

        if not duplicate_case and baseline is not None and effort is not None:
            baselines.append(baseline)
            efforts.append(effort)
        case_artifact_bindings.append(
            {
                "baseline_evidence_sha256": (
                    baseline_pointer.get("sha256")
                    if isinstance(baseline_pointer, Mapping)
                    else None
                ),
                "case_id": raw_case_id,
                "drawing_source_sha256": source_digest,
                "harness_evidence_sha256": (
                    effort_pointer.get("sha256") if isinstance(effort_pointer, Mapping) else None
                ),
            }
        )

    if thresholds is not None:
        for group in CAPABILITY_GROUPS:
            if group_counts[group] < thresholds.minimum_cases_per_group:
                errors.append(_error("GROUP_CASE_COUNT_TOO_LOW", f"cases.group.{group}"))

    metrics: Mapping[str, Any] | None = None
    metric_errors: list[Error] = []
    if (
        not errors
        and thresholds is not None
        and _opaque(pilot_run_id)
        and len(baselines) == len(cases)
        and len(efforts) == len(cases)
    ):
        try:
            report = MetricsCollector(thresholds).aggregate(
                report_id="production-pilot-acceptance",
                pilot_run_id=str(pilot_run_id),
                baseline=baselines,
                efforts=efforts,
            )
        except (ValueError, TypeError):
            errors.append(_error("METRICS_AGGREGATION_FAILED", "metrics"))
        else:
            metrics, metric_errors = _report_metrics(report, thresholds)

    expected_review_scope_sha256 = None
    if metrics is not None and thresholds is not None and _opaque(pilot_run_id):
        expected_review_scope_sha256 = _review_scope_sha256(
            pilot_run_id=str(pilot_run_id),
            case_artifact_bindings=case_artifact_bindings,
            metrics=metrics,
            thresholds=thresholds,
        )

    raw_review = manifest.get("independent_review")
    review_pointer: object = None
    reviewer_id: object = None
    if not isinstance(raw_review, Mapping):
        errors.append(_error("INDEPENDENT_REVIEW_MISSING", "independent_review"))
    else:
        review: JsonMapping = raw_review
        _exact_keys(review, _REVIEW_POINTER_KEYS, field="independent_review", errors=errors)
        reviewer_id = review.get("reviewer_id")
        review_pointer = review.get("evidence")
    review_evidence = _locked_evidence(
        root,
        review_pointer,
        field="independent_review.evidence",
        errors=errors,
        seen_refs=seen_evidence_refs,
        seen_paths=seen_evidence_paths,
        snapshots=snapshots,
    )
    _validate_review(
        review_evidence,
        pointer=review_pointer,
        pilot_run_id=pilot_run_id,
        reviewer_id=reviewer_id,
        participant_ids=participant_ids,
        case_ids=case_ids,
        expected_review_scope_sha256=expected_review_scope_sha256,
        latest_measurement_end=latest_measurement_end,
        trust_policy=trust_policy,
        expected_policy_sha256=pinned_trust_policy_sha256,
        now=verification_time,
        errors=errors,
    )

    evidence_verified = not errors
    errors.extend(metric_errors)

    return _summary(
        passed=not errors and evidence_verified and metrics is not None,
        evidence_verified=evidence_verified,
        case_count=len(cases),
        participant_count=len(participant_ids),
        group_counts=group_counts,
        metrics=metrics,
        errors=errors,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="local pilot manifest; path is never emitted")
    parser.add_argument(
        "--thresholds",
        type=Path,
        default=Path("config/pilot.yaml"),
        help="pilot threshold policy; path is never emitted",
    )
    parser.add_argument(
        "--trust-policy",
        type=Path,
        required=True,
        help="local production-evidence trust policy; path is never emitted",
    )
    parser.add_argument(
        "--trust-policy-sha256",
        help=(f"expected canonical trust-policy SHA-256; falls back to {TRUST_POLICY_SHA256_ENV}"),
    )
    args = parser.parse_args(argv)
    summary = verify_production_pilot_acceptance(
        args.manifest,
        args.thresholds,
        args.trust_policy,
        expected_trust_policy_sha256=args.trust_policy_sha256,
    )
    print(canonical_json(summary))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
