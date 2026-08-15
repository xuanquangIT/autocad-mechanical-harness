"""Fail-closed verifier for real, independently attested raster evidence."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cad_harness.domain.canonical import canonical_json
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

MIN_CASES = 5
MAX_CASES = 50
MIN_ACCURACY_SAMPLES = 4
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
TRUST_POLICY_ENV = "CAD_HARNESS_EVIDENCE_TRUST_POLICY"
TRUST_POLICY_SHA256_ENV = "CAD_HARNESS_EVIDENCE_TRUST_POLICY_SHA256"
EXECUTION_PUBLIC_KEY_ENV = "CAD_HARNESS_RASTER_EXECUTION_PUBLIC_KEY"
EXECUTION_KEY_SHA256_ENV = "CAD_HARNESS_RASTER_EXECUTION_KEY_SHA256"
REQUIRED_PRIMITIVES = frozenset({"line", "circle", "arc", "closed_polyline"})
SUPPORTED_MEDIA = frozenset({"image/png", "image/jpeg"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_METRIC_EPSILON = 1e-12
_GEOMETRY_KINDS = frozenset({"line", "circle", "arc", "closed_polyline"})
_PRODUCTION_FLAGS = {
    "production_evidence": True,
    "development_evidence": False,
    "synthetic_evidence": False,
    "generated_evidence": False,
    "simulated_evidence": False,
}
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_START = b"\xff\xd8"
_JPEG_END = b"\xff\xd9"
_MIN_IMAGE_WIDTH = 16
_MIN_IMAGE_HEIGHT = 16
_EXECUTION_DOMAIN = b"cad-harness/raster-execution-receipt/v1\x00"
_EXECUTION_CLAIM_KEYS = frozenset(
    {
        "adapter_type",
        "process_id",
        "document_id",
        "pre_revision",
        "post_revision",
        "plan_hash",
        "job_id",
        "validation_report_sha256",
        "result_sha256",
    }
)


def _error(code: str, field: str, case_id: str | None = None) -> dict[str, str]:
    value = {"code": code, "field": field}
    if case_id is not None:
        value["case_id"] = case_id
    return value


def _non_empty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_case_id(value: object, index: int) -> str:
    return (
        str(value)
        if isinstance(value, str) and _SAFE_ID.fullmatch(value)
        else f"case-{index + 1:03d}"
    )


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & flag)


def _stable_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_stable_file(path: Path) -> bytes | None:
    """Read one bounded snapshot and prove the open handle did not change."""
    if _is_reparse(path):
        return None
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_ARTIFACT_BYTES:
                return None
            payload = stream.read(MAX_ARTIFACT_BYTES + 1)
            after = os.fstat(stream.fileno())
    except OSError:
        return None
    if (
        len(payload) != before.st_size
        or len(payload) > MAX_ARTIFACT_BYTES
        or _stable_identity(before) != _stable_identity(after)
    ):
        return None
    return payload


def _artifact(root: Path, reference: object) -> Path | None:
    if not _non_empty(reference):
        return None
    raw = str(reference).replace("\\", "/")
    relative = Path(raw)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        return None
    try:
        resolved_root = root.resolve(strict=True)
        current = resolved_root
        for part in relative.parts:
            current /= part
            if _is_reparse(current):
                return None
        resolved = current.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    return resolved


def _expected_digest(value: object) -> str | None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        return None
    return value.removeprefix("sha256:").casefold()


def _bound_digest(value: object) -> str:
    digest = _expected_digest(value)
    return f"sha256:{digest}" if digest is not None else "sha256:invalid"


def _verified_artifact(
    root: Path,
    container: Mapping[str, Any],
    field: str,
    case_id: str,
) -> tuple[bytes | None, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    path = _artifact(root, container.get("artifact_ref"))
    if path is None:
        errors.append(_error("ARTIFACT_MISSING", f"{field}.artifact_ref", case_id))
        return None, errors
    payload = _read_stable_file(path)
    if payload is None:
        errors.append(_error("ARTIFACT_UNSTABLE", f"{field}.artifact_ref", case_id))
        return None, errors
    expected = _expected_digest(container.get("sha256"))
    if expected is None:
        errors.append(_error("ARTIFACT_HASH_INVALID", f"{field}.sha256", case_id))
    elif hashlib.sha256(payload).hexdigest() != expected:
        errors.append(_error("ARTIFACT_HASH_MISMATCH", f"{field}.sha256", case_id))
    return payload, errors


def _json_mapping(payload: bytes | None) -> Mapping[str, Any] | None:
    if payload is None:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _claims_match(
    payload: Mapping[str, Any] | None,
    claims: Mapping[str, Any],
    fields: Sequence[str],
) -> bool:
    return payload is not None and all(payload.get(field) == claims.get(field) for field in fields)


def _positive_number(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _finite_number(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        return None
    return float(value)


def _locked_section(section: object, fields: Sequence[str]) -> dict[str, JsonValue]:
    if not isinstance(section, Mapping):
        return {"artifact_sha256": "sha256:invalid"}
    locked: dict[str, JsonValue] = {"artifact_sha256": _bound_digest(section.get("sha256"))}
    for field in fields:
        value = section.get(field)
        if isinstance(value, (str, int, float, bool, list)) or value is None:
            locked[field] = value
        else:
            locked[field] = None
    return locked


def raster_engineer_attestation_claims(case: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Build the exact path-free claim set reviewed by the raster engineer."""
    return {
        "case_id": str(case.get("case_id", "")),
        "source": _locked_section(
            case.get("source"),
            (
                "media_type",
                "synthetic",
                "generated",
                "simulated",
                "shop_scan",
                "deidentified",
                "scan_quality",
                "provenance_ref",
                "prepared_by",
            ),
        ),
        "calibration": _locked_section(
            case.get("calibration"),
            (
                "source_sha256",
                "pixel_distance",
                "real_distance_mm",
                "engineer_id",
                "evidence_ref",
            ),
        ),
        "trace": _locked_section(
            case.get("trace"), ("source_sha256", "deterministic_runs", "detected_types")
        ),
        "candidate_geometry": _locked_section(case.get("candidate_geometry"), ()),
        "engineer_acceptance": _locked_section(
            case.get("engineer_acceptance"),
            (
                "source_sha256",
                "engineer_id",
                "evidence_ref",
                "candidate_set_sha256",
                "accepted_candidate_count",
                "accepted_candidate_refs",
            ),
        ),
        "live_readback": _locked_section(
            case.get("live_readback"),
            (
                "source_sha256",
                "acceptance_sha256",
                "trace_sha256",
                "candidate_set_sha256",
                "accepted_candidate_refs",
                "job_id",
                "plan_hash",
                "adapter_type",
                "process_id",
                "document_id",
                "autocad_version",
                "pre_revision",
                "post_revision",
                "measured_geometry",
                "validation_report_sha256",
                "validation_passed",
            ),
        ),
        "execution_receipt": _locked_section(case.get("execution_receipt"), ()),
    }


def raster_accuracy_attestation_claims(case: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Build the exact path-free claim set reviewed by the accuracy reviewer."""
    return {
        "case_id": str(case.get("case_id", "")),
        "accuracy": _locked_section(
            case.get("accuracy"),
            (
                "source_sha256",
                "calculated_by",
                "reviewed_by",
                "samples",
                "sample_count",
                "tolerance_mm",
                "maximum_error_mm",
                "rmse_mm",
            ),
        ),
    }


def _declared_image_type(payload: bytes | None) -> str | None:
    if payload is None:
        return None
    if payload.startswith(_PNG_MAGIC):
        return "image/png"
    if payload.startswith(_JPEG_START) and payload.endswith(_JPEG_END):
        return "image/jpeg"
    return None


def _image_content_valid(decoded: np.ndarray | None) -> bool:
    if decoded is None or decoded.size == 0 or decoded.ndim not in {2, 3}:
        return False
    height, width = decoded.shape[:2]
    if height < _MIN_IMAGE_HEIGHT or width < _MIN_IMAGE_WIDTH:
        return False
    if decoded.ndim == 3:
        channels = decoded.shape[2]
        if channels not in {3, 4}:
            return False
        conversion = cv2.COLOR_BGR2GRAY if channels == 3 else cv2.COLOR_BGRA2GRAY
        gray = cv2.cvtColor(decoded, conversion)
    else:
        gray = decoded
    minimum = int(np.min(gray))
    maximum = int(np.max(gray))
    foreground = int(np.count_nonzero(gray != minimum))
    return maximum > minimum and 0 < foreground < gray.size


def _validate_source(
    source: object,
    root: Path,
    case_id: str,
) -> tuple[str | None, str | None, list[dict[str, str]]]:
    if not isinstance(source, Mapping):
        return None, None, [_error("SOURCE_EVIDENCE_MISSING", "source", case_id)]
    payload, errors = _verified_artifact(root, source, "source", case_id)
    digest = _expected_digest(source.get("sha256"))
    media_type = source.get("media_type")
    if media_type not in SUPPORTED_MEDIA:
        errors.append(_error("SOURCE_MEDIA_UNSUPPORTED", "source.media_type", case_id))
    detected_media_type = _declared_image_type(payload)
    if detected_media_type is None or detected_media_type != media_type:
        errors.append(_error("SOURCE_MEDIA_MAGIC_MISMATCH", "source.media_type", case_id))
    artifact_ref = source.get("artifact_ref")
    suffix = Path(str(artifact_ref)).suffix.casefold() if _non_empty(artifact_ref) else ""
    expected_suffixes = {"image/png": {".png"}, "image/jpeg": {".jpg", ".jpeg"}}
    if media_type in expected_suffixes and suffix not in expected_suffixes[media_type]:
        errors.append(_error("SOURCE_MEDIA_SUFFIX_MISMATCH", "source.artifact_ref", case_id))
    if source.get("synthetic") is not False or source.get("shop_scan") is not True:
        errors.append(_error("SOURCE_NOT_REAL_SHOP_SCAN", "source.shop_scan", case_id))
    if source.get("generated") is not False or source.get("simulated") is not False:
        errors.append(_error("SOURCE_NOT_REAL_SHOP_SCAN", "source.generated", case_id))
    if source.get("deidentified") is not True:
        errors.append(_error("SOURCE_NOT_DEIDENTIFIED", "source.deidentified", case_id))
    for field in ("provenance_ref", "prepared_by"):
        if not _non_empty(source.get(field)):
            errors.append(_error("SOURCE_PROVENANCE_MISSING", f"source.{field}", case_id))
    decoded = (
        cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if payload is not None
        else None
    )
    if decoded is None or decoded.size == 0 or decoded.ndim not in {2, 3}:
        errors.append(_error("SOURCE_IMAGE_INVALID", "source.artifact_ref", case_id))
    elif not _image_content_valid(decoded):
        errors.append(_error("SOURCE_IMAGE_DEGENERATE", "source.artifact_ref", case_id))
    quality = source.get("scan_quality")
    if quality not in {"clean", "noisy", "rotated", "cropped"}:
        errors.append(_error("SCAN_QUALITY_INVALID", "source.scan_quality", case_id))
        quality = None
    return digest, str(quality) if quality is not None else None, errors


def _point(value: object) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    x = _finite_number(value[0])
    y = _finite_number(value[1])
    return (x, y) if x is not None and y is not None else None


def _candidate_geometry_valid(candidate: object) -> bool:
    if not isinstance(candidate, Mapping) or set(candidate) != {
        "candidate_ref",
        "geometry_kind",
        "geometry",
    }:
        return False
    candidate_ref = candidate.get("candidate_ref")
    kind = candidate.get("geometry_kind")
    geometry = candidate.get("geometry")
    if (
        not isinstance(candidate_ref, str)
        or _SAFE_ID.fullmatch(candidate_ref) is None
        or kind not in _GEOMETRY_KINDS
        or not isinstance(geometry, Mapping)
    ):
        return False
    if kind == "line":
        if set(geometry) != {"start", "end"}:
            return False
        start = _point(geometry.get("start"))
        end = _point(geometry.get("end"))
        return start is not None and end is not None and start != end
    if kind in {"circle", "arc"}:
        expected = {"center", "radius_mm"}
        if kind == "arc":
            expected |= {"start_angle_deg", "end_angle_deg"}
        if set(geometry) != expected or _point(geometry.get("center")) is None:
            return False
        radius = _finite_number(geometry.get("radius_mm"))
        if radius is None or radius <= 0:
            return False
        if kind == "circle":
            return True
        start_angle = _finite_number(geometry.get("start_angle_deg"))
        end_angle = _finite_number(geometry.get("end_angle_deg"))
        return (
            start_angle is not None
            and end_angle is not None
            and not math.isclose(start_angle, end_angle, abs_tol=_METRIC_EPSILON)
        )
    if set(geometry) != {"vertices"}:
        return False
    raw_vertices = geometry.get("vertices")
    if not isinstance(raw_vertices, list) or not 3 <= len(raw_vertices) <= 10_000:
        return False
    vertices = [_point(vertex) for vertex in raw_vertices]
    if any(vertex is None for vertex in vertices):
        return False
    points = [vertex for vertex in vertices if vertex is not None]
    area_twice = abs(
        sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1], strict=True)
        )
    )
    return len(set(points)) >= 3 and area_twice > _METRIC_EPSILON


def _validate_candidate_geometry(
    section: object,
    root: Path,
    source_digest: str | None,
    acceptance: object,
    case_id: str,
) -> tuple[set[str], list[dict[str, str]]]:
    if not isinstance(section, Mapping):
        return set(), [_error("CANDIDATE_GEOMETRY_MISSING", "candidate_geometry", case_id)]
    raw, errors = _verified_artifact(root, section, "candidate_geometry", case_id)
    payload = _json_mapping(raw)
    if payload is None or set(payload) != {"schema_version", "source_sha256", "candidates"}:
        errors.append(
            _error(
                "CANDIDATE_GEOMETRY_ARTIFACT_INVALID", "candidate_geometry.artifact_ref", case_id
            )
        )
        return set(), errors
    candidates = payload.get("candidates")
    if (
        payload.get("schema_version") != "1.0"
        or payload.get("source_sha256") != f"sha256:{source_digest}"
        or not isinstance(candidates, list)
        or not candidates
        or len(candidates) > 100_000
        or any(not _candidate_geometry_valid(candidate) for candidate in candidates)
    ):
        errors.append(
            _error(
                "CANDIDATE_GEOMETRY_ARTIFACT_INVALID", "candidate_geometry.artifact_ref", case_id
            )
        )
        return set(), errors
    candidate_refs = {str(candidate["candidate_ref"]) for candidate in candidates}
    if len(candidate_refs) != len(candidates):
        errors.append(_error("CANDIDATE_REF_DUPLICATE", "candidate_geometry.candidates", case_id))
    recomputed = f"sha256:{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()}"
    accepted_refs = _accepted_refs(acceptance)
    declared = acceptance.get("candidate_set_sha256") if isinstance(acceptance, Mapping) else None
    if recomputed != declared:
        errors.append(
            _error(
                "CANDIDATE_SET_HASH_MISMATCH", "engineer_acceptance.candidate_set_sha256", case_id
            )
        )
    if candidate_refs != accepted_refs:
        errors.append(
            _error("CANDIDATE_GEOMETRY_BINDING_MISMATCH", "candidate_geometry.candidates", case_id)
        )
    return candidate_refs, errors


def _sample_error(
    sample: object,
    *,
    source_sha256: object,
    accepted_candidate_refs: set[str],
) -> tuple[float, tuple[str, str, str, str, str]] | None:
    expected_keys = {
        "sample_id",
        "candidate_ref",
        "entity_ref",
        "geometry_kind",
        "unit",
        "source_sha256",
        "kind",
        "measurement_key",
        "expected_mm",
        "observed_mm",
    }
    if not isinstance(sample, Mapping) or set(sample) != expected_keys:
        return None
    sample_id = sample.get("sample_id")
    candidate_ref = sample.get("candidate_ref")
    entity_ref = sample.get("entity_ref")
    measurement_key = sample.get("measurement_key")
    if (
        not isinstance(sample_id, str)
        or _SAFE_ID.fullmatch(sample_id) is None
        or not isinstance(candidate_ref, str)
        or candidate_ref not in accepted_candidate_refs
        or not _non_empty(entity_ref)
        or not isinstance(measurement_key, str)
        or _SAFE_ID.fullmatch(measurement_key) is None
        or sample.get("geometry_kind") not in _GEOMETRY_KINDS
        or sample.get("unit") != "mm"
        or sample.get("source_sha256") != source_sha256
    ):
        return None
    kind = sample.get("kind")
    expected = sample.get("expected_mm")
    observed = sample.get("observed_mm")
    if kind in {"scalar", "length"}:
        expected_number = _finite_number(expected)
        observed_number = _finite_number(observed)
        if (
            expected_number is None
            or observed_number is None
            or expected_number < 0
            or observed_number < 0
        ):
            return None
        return abs(observed_number - expected_number), (
            candidate_ref,
            str(entity_ref),
            str(sample.get("geometry_kind")),
            "mm",
            measurement_key,
        )
    if kind == "point":
        if (
            not isinstance(expected, list)
            or not isinstance(observed, list)
            or len(expected) != 2
            or len(observed) != 2
        ):
            return None
        expected_x = _finite_number(expected[0])
        expected_y = _finite_number(expected[1])
        observed_x = _finite_number(observed[0])
        observed_y = _finite_number(observed[1])
        if None in {expected_x, expected_y, observed_x, observed_y}:
            return None
        assert expected_x is not None and expected_y is not None
        assert observed_x is not None and observed_y is not None
        if min(expected_x, expected_y, observed_x, observed_y) < 0:
            return None
        return (
            math.hypot(observed_x - expected_x, observed_y - expected_y),
            (
                candidate_ref,
                str(entity_ref),
                str(sample.get("geometry_kind")),
                "mm",
                measurement_key,
            ),
        )
    return None


def _validate_accuracy(
    accuracy: Mapping[str, Any],
    payload: Mapping[str, Any] | None,
    source_digest: str | None,
    accepted_candidate_refs: set[str],
    case_id: str,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    fields = (
        "source_sha256",
        "calculated_by",
        "reviewed_by",
        "samples",
        "sample_count",
        "tolerance_mm",
        "maximum_error_mm",
        "rmse_mm",
    )
    if (
        not _claims_match(payload, accuracy, fields)
        or accuracy.get("source_sha256") != f"sha256:{source_digest}"
    ):
        errors.append(_error("ACCURACY_SOURCE_MISMATCH", "accuracy.source_sha256", case_id))
    samples = accuracy.get("samples")
    sample_results = (
        [
            _sample_error(
                sample,
                source_sha256=accuracy.get("source_sha256"),
                accepted_candidate_refs=accepted_candidate_refs,
            )
            for sample in samples
        ]
        if isinstance(samples, list)
        else []
    )
    if (
        not isinstance(samples, list)
        or len(samples) < MIN_ACCURACY_SAMPLES
        or any(result is None for result in sample_results)
    ):
        errors.append(_error("ACCURACY_SAMPLES_INVALID", "accuracy.samples", case_id))
        return errors
    valid_results = [result for result in sample_results if result is not None]
    errors_mm = [result[0] for result in valid_results]
    sample_ids = [str(sample["sample_id"]) for sample in samples]
    sampled_candidates = {result[1][0] for result in valid_results}
    sample_bindings = [result[1] for result in valid_results]
    if (
        len(set(sample_ids)) != len(sample_ids)
        or len(set(sample_bindings)) != len(sample_bindings)
        or sampled_candidates != accepted_candidate_refs
    ):
        errors.append(_error("ACCURACY_SAMPLE_BINDING_DUPLICATE", "accuracy.samples", case_id))
    recomputed_count = len(errors_mm)
    recomputed_maximum = max(errors_mm)
    recomputed_rmse = math.sqrt(sum(error * error for error in errors_mm) / recomputed_count)
    sample_count = accuracy.get("sample_count")
    maximum = _finite_number(accuracy.get("maximum_error_mm"))
    rmse = _finite_number(accuracy.get("rmse_mm"))
    tolerance = _finite_number(accuracy.get("tolerance_mm"))
    aggregate_matches = (
        sample_count == recomputed_count
        and maximum is not None
        and rmse is not None
        and math.isclose(maximum, recomputed_maximum, rel_tol=0.0, abs_tol=_METRIC_EPSILON)
        and math.isclose(rmse, recomputed_rmse, rel_tol=0.0, abs_tol=_METRIC_EPSILON)
    )
    if not aggregate_matches:
        errors.append(_error("ACCURACY_AGGREGATE_MISMATCH", "accuracy.samples", case_id))
    if tolerance is None or tolerance <= 0:
        errors.append(_error("ACCURACY_METRICS_INVALID", "accuracy.tolerance_mm", case_id))
    elif recomputed_maximum > tolerance or recomputed_rmse > tolerance:
        errors.append(_error("ACCURACY_THRESHOLD_FAILED", "accuracy.samples", case_id))
    return errors


def _accepted_refs(acceptance: object) -> set[str]:
    if not isinstance(acceptance, Mapping):
        return set()
    refs = acceptance.get("accepted_candidate_refs")
    if (
        not isinstance(refs, list)
        or not refs
        or any(not isinstance(ref, str) or not _non_empty(ref) for ref in refs)
        or len(set(refs)) != len(refs)
    ):
        return set()
    return set(refs)


def _decode_base64url(value: object, size: int) -> bytes | None:
    if not isinstance(value, str) or not value or "=" in value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None
    canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return raw if len(raw) == size and hmac.compare_digest(canonical, value) else None


def _utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _execution_receipt_unsigned(receipt: Mapping[str, Any]) -> dict[str, JsonValue]:
    return {
        "schema_version": str(receipt.get("schema_version", "")),
        "signer_id": str(receipt.get("signer_id", "")),
        "issued_at": str(receipt.get("issued_at", "")),
        "claims": dict(receipt.get("claims", {}))
        if isinstance(receipt.get("claims"), Mapping)
        else {},
    }


def _validate_execution_receipt(
    section: object,
    root: Path,
    readback: Mapping[str, Any],
    *,
    public_key_value: str,
    expected_key_sha256: str,
    policy: EvidenceTrustPolicy | None,
    now: datetime | None,
    case_id: str,
) -> list[dict[str, str]]:
    if not isinstance(section, Mapping) or set(section) != {"artifact_ref", "sha256"}:
        return [_error("EXECUTION_RECEIPT_MISSING", "execution_receipt", case_id)]
    raw, errors = _verified_artifact(root, section, "execution_receipt", case_id)
    receipt = _json_mapping(raw)
    if receipt is None or set(receipt) != {
        "schema_version",
        "signer_id",
        "issued_at",
        "claims",
        "signature",
    }:
        errors.append(_error("EXECUTION_RECEIPT_INVALID", "execution_receipt", case_id))
        return errors
    claims = receipt.get("claims")
    issued_at = _utc_timestamp(receipt.get("issued_at"))
    signature_value = receipt.get("signature")
    signature = (
        _decode_base64url(signature_value.removeprefix("ed25519:"), 64)
        if isinstance(signature_value, str) and signature_value.startswith("ed25519:")
        else None
    )
    public_key = _decode_base64url(public_key_value, 32)
    expected_key = _expected_digest(expected_key_sha256)
    if public_key is None or expected_key is None:
        errors.append(_error("EXECUTION_TRUST_CONFIG_INVALID", "execution_trust", case_id))
        return errors
    actual_key = hashlib.sha256(public_key).hexdigest()
    if not hmac.compare_digest(actual_key, expected_key):
        errors.append(_error("EXECUTION_TRUST_KEY_MISMATCH", "execution_trust", case_id))
        return errors
    if policy is not None and any(
        hmac.compare_digest(identity.public_key, public_key_value) for identity in policy.identities
    ):
        errors.append(_error("EXECUTION_SIGNER_NOT_INDEPENDENT", "execution_trust", case_id))
    expected_claims = {
        "adapter_type": readback.get("adapter_type"),
        "process_id": readback.get("process_id"),
        "document_id": readback.get("document_id"),
        "pre_revision": readback.get("pre_revision"),
        "post_revision": readback.get("post_revision"),
        "plan_hash": readback.get("plan_hash"),
        "job_id": readback.get("job_id"),
        "validation_report_sha256": readback.get("validation_report_sha256"),
        "result_sha256": _bound_digest(readback.get("sha256")),
    }
    if (
        receipt.get("schema_version") != "1.0"
        or not _non_empty(receipt.get("signer_id"))
        or not isinstance(claims, Mapping)
        or set(claims) != _EXECUTION_CLAIM_KEYS
        or dict(claims) != expected_claims
        or issued_at is None
        or issued_at > (now or datetime.now(UTC)).astimezone(UTC)
        or signature is None
    ):
        errors.append(_error("EXECUTION_RECEIPT_BINDING_INVALID", "execution_receipt", case_id))
        return errors
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            _EXECUTION_DOMAIN
            + canonical_json(_execution_receipt_unsigned(receipt)).encode("utf-8"),
        )
    except InvalidSignature:
        errors.append(_error("EXECUTION_RECEIPT_SIGNATURE_INVALID", "execution_receipt", case_id))
    return errors


def _validate_readback_bindings(
    readback: Mapping[str, Any],
    *,
    source_digest: str | None,
    trace: object,
    acceptance: object,
    accuracy: object,
    case_id: str,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    accepted_refs = _accepted_refs(acceptance)
    expected_acceptance_sha = (
        _bound_digest(acceptance.get("sha256"))
        if isinstance(acceptance, Mapping)
        else "sha256:invalid"
    )
    expected_trace_sha = (
        _bound_digest(trace.get("sha256")) if isinstance(trace, Mapping) else "sha256:invalid"
    )
    expected_candidate_set = (
        acceptance.get("candidate_set_sha256") if isinstance(acceptance, Mapping) else None
    )
    expected_readback_refs = (
        acceptance.get("accepted_candidate_refs") if isinstance(acceptance, Mapping) else None
    )
    readback_refs = readback.get("accepted_candidate_refs")
    readback_refs_valid = isinstance(readback_refs, list) and all(
        isinstance(ref, str) for ref in readback_refs
    )
    bindings_valid = (
        readback.get("source_sha256") == f"sha256:{source_digest}"
        and readback.get("acceptance_sha256") == expected_acceptance_sha
        and readback.get("trace_sha256") == expected_trace_sha
        and readback.get("candidate_set_sha256") == expected_candidate_set
        and readback_refs_valid
        and readback_refs == expected_readback_refs
    )
    if not bindings_valid:
        errors.append(_error("LIVE_READBACK_BINDING_MISMATCH", "live_readback", case_id))
    if (
        not _non_empty(readback.get("job_id"))
        or not isinstance(readback.get("plan_hash"), str)
        or _REVISION.fullmatch(str(readback.get("plan_hash"))) is None
        or readback.get("adapter_type") not in {"com", "dotnet_bridge"}
        or not isinstance(readback.get("process_id"), int)
        or isinstance(readback.get("process_id"), bool)
        or int(readback.get("process_id", 0)) <= 0
        or not isinstance(readback.get("document_id"), str)
        or _SAFE_ID.fullmatch(str(readback.get("document_id"))) is None
        or not isinstance(readback.get("validation_report_sha256"), str)
        or _REVISION.fullmatch(str(readback.get("validation_report_sha256"))) is None
    ):
        errors.append(_error("LIVE_READBACK_EXECUTION_INVALID", "live_readback.job_id", case_id))
    pre_revision = readback.get("pre_revision")
    post_revision = readback.get("post_revision")
    if (
        not isinstance(pre_revision, str)
        or _REVISION.fullmatch(pre_revision) is None
        or not isinstance(post_revision, str)
        or _REVISION.fullmatch(post_revision) is None
        or pre_revision == post_revision
    ):
        errors.append(
            _error("LIVE_READBACK_REVISION_INVALID", "live_readback.post_revision", case_id)
        )
    measured = readback.get("measured_geometry")
    measured_values: dict[tuple[str, str, str, str, str], float | list[float]] = {}
    measured_candidates: set[str] = set()
    measured_valid = isinstance(measured, list) and bool(measured)
    measured_count = len(measured) if isinstance(measured, list) else 0
    if isinstance(measured, list):
        for item in measured:
            if not isinstance(item, Mapping) or set(item) != {
                "candidate_ref",
                "entity_ref",
                "geometry_kind",
                "unit",
                "measurements",
            }:
                measured_valid = False
                continue
            candidate_ref = item.get("candidate_ref")
            entity_ref = item.get("entity_ref")
            measurements = item.get("measurements")
            if (
                not isinstance(candidate_ref, str)
                or candidate_ref not in accepted_refs
                or not _non_empty(entity_ref)
                or item.get("geometry_kind") not in _GEOMETRY_KINDS
                or item.get("unit") != "mm"
                or not isinstance(measurements, Mapping)
                or not measurements
                or any(
                    not isinstance(key, str)
                    or _SAFE_ID.fullmatch(key) is None
                    or (
                        _finite_number(value) is None
                        and not (
                            isinstance(value, list)
                            and len(value) == 2
                            and all(_finite_number(component) is not None for component in value)
                        )
                    )
                    for key, value in measurements.items()
                )
            ):
                measured_valid = False
                continue
            measured_candidates.add(candidate_ref)
            for measurement_key, measurement_value in measurements.items():
                binding = (
                    candidate_ref,
                    str(entity_ref),
                    str(item.get("geometry_kind")),
                    "mm",
                    str(measurement_key),
                )
                if binding in measured_values:
                    measured_valid = False
                elif isinstance(measurement_value, list):
                    measured_values[binding] = [float(value) for value in measurement_value]
                else:
                    measured_values[binding] = float(measurement_value)
    accuracy_samples = accuracy.get("samples") if isinstance(accuracy, Mapping) else None
    sample_bindings = (
        {
            (
                str(sample.get("candidate_ref")),
                str(sample.get("entity_ref")),
                str(sample.get("geometry_kind")),
                str(sample.get("unit")),
                str(sample.get("measurement_key")),
            ): sample.get("observed_mm")
            for sample in accuracy_samples
            if isinstance(sample, Mapping)
        }
        if isinstance(accuracy_samples, list)
        else {}
    )
    if (
        not measured_valid
        or measured_candidates != accepted_refs
        or not sample_bindings
        or set(sample_bindings) != set(measured_values)
        or len(measured_candidates) != measured_count
    ):
        errors.append(
            _error("LIVE_READBACK_MEASUREMENT_UNBOUND", "live_readback.measured_geometry", case_id)
        )
    tolerance = (
        _finite_number(accuracy.get("tolerance_mm")) if isinstance(accuracy, Mapping) else None
    )
    if tolerance is not None and tolerance >= 0 and set(sample_bindings) == set(measured_values):
        for binding, observed in sample_bindings.items():
            live = measured_values[binding]
            mismatch = False
            if (
                isinstance(observed, list)
                and isinstance(live, list)
                and len(observed) == len(live) == 2
            ):
                observed_values = [_finite_number(value) for value in observed]
                if any(value is None for value in observed_values):
                    mismatch = True
                else:
                    observed_x, observed_y = observed_values
                    assert observed_x is not None and observed_y is not None
                    mismatch = math.hypot(observed_x - live[0], observed_y - live[1]) > tolerance
            else:
                observed_number = _finite_number(observed)
                live_number = _finite_number(live)
                mismatch = (
                    observed_number is None
                    or live_number is None
                    or abs(observed_number - live_number) > tolerance
                )
            if mismatch:
                errors.append(
                    _error("ACCURACY_OBSERVED_LIVE_MISMATCH", "accuracy.samples", case_id)
                )
                break
    return errors


def _verify_case_attestation(
    value: object,
    *,
    expected_role: EvidenceRole,
    exact_claims: JsonValue,
    policy: EvidenceTrustPolicy | None,
    expected_policy_sha256: str,
    now: datetime | None,
    field: str,
    case_id: str,
) -> tuple[str | None, list[dict[str, str]]]:
    if policy is None:
        return None, [_error("ATTESTATION_UNVERIFIED", field, case_id)]
    try:
        attestation: EvidenceAttestation = evidence_attestation_from_mapping(value)
    except EvidenceAttestationError:
        return None, [_error("ATTESTATION_INVALID", field, case_id)]
    if attestation.role is not expected_role:
        return None, [_error("ATTESTATION_ROLE_INVALID", field, case_id)]
    try:
        identity = verify_attestation(
            policy,
            attestation,
            exact_claims,
            expected_policy_sha256=expected_policy_sha256,
            now=now,
        )
    except EvidenceAttestationError:
        return None, [_error("ATTESTATION_VERIFICATION_FAILED", field, case_id)]
    return identity.identity_id, []


def _validate_case(
    case: Mapping[str, Any],
    root: Path,
    case_id: str,
    policy: EvidenceTrustPolicy | None,
    expected_policy_sha256: str,
    execution_public_key: str,
    expected_execution_key_sha256: str,
    now: datetime | None,
) -> tuple[set[str], str | None, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    source_digest, quality, source_errors = _validate_source(case.get("source"), root, case_id)
    errors.extend(source_errors)

    calibration = case.get("calibration")
    if not isinstance(calibration, Mapping):
        errors.append(_error("CALIBRATION_MISSING", "calibration", case_id))
    else:
        raw, artifact_errors = _verified_artifact(root, calibration, "calibration", case_id)
        errors.extend(artifact_errors)
        payload = _json_mapping(raw)
        if not _positive_number(calibration.get("pixel_distance")) or not _positive_number(
            calibration.get("real_distance_mm")
        ):
            errors.append(_error("CALIBRATION_INVALID", "calibration.distance", case_id))
        if (
            not _claims_match(
                payload,
                calibration,
                (
                    "source_sha256",
                    "pixel_distance",
                    "real_distance_mm",
                    "engineer_id",
                    "evidence_ref",
                ),
            )
            or calibration.get("source_sha256") != f"sha256:{source_digest}"
        ):
            errors.append(
                _error("CALIBRATION_SOURCE_MISMATCH", "calibration.source_sha256", case_id)
            )

    trace = case.get("trace")
    detected: set[str] = set()
    if not isinstance(trace, Mapping):
        errors.append(_error("TRACE_EVIDENCE_MISSING", "trace", case_id))
    else:
        raw, artifact_errors = _verified_artifact(root, trace, "trace", case_id)
        errors.extend(artifact_errors)
        payload = _json_mapping(raw)
        if (
            not _claims_match(
                payload, trace, ("source_sha256", "deterministic_runs", "detected_types")
            )
            or trace.get("source_sha256") != f"sha256:{source_digest}"
        ):
            errors.append(_error("TRACE_SOURCE_MISMATCH", "trace.source_sha256", case_id))
        if (
            not isinstance(trace.get("deterministic_runs"), int)
            or trace.get("deterministic_runs", 0) < 2
        ):
            errors.append(_error("TRACE_DETERMINISM_MISSING", "trace.deterministic_runs", case_id))
        raw_types = trace.get("detected_types")
        if isinstance(raw_types, list) and all(isinstance(item, str) for item in raw_types):
            detected = set(raw_types)
        else:
            errors.append(_error("TRACE_TYPES_INVALID", "trace.detected_types", case_id))

    acceptance = case.get("engineer_acceptance")
    if not isinstance(acceptance, Mapping):
        errors.append(_error("ENGINEER_ACCEPTANCE_MISSING", "engineer_acceptance", case_id))
    else:
        raw, artifact_errors = _verified_artifact(root, acceptance, "engineer_acceptance", case_id)
        errors.extend(artifact_errors)
        payload = _json_mapping(raw)
        if acceptance.get("source_sha256") != f"sha256:{source_digest}":
            errors.append(
                _error("ACCEPTANCE_SOURCE_MISMATCH", "engineer_acceptance.source_sha256", case_id)
            )
        if (
            not isinstance(acceptance.get("accepted_candidate_count"), int)
            or acceptance.get("accepted_candidate_count", 0) < 1
        ):
            errors.append(
                _error(
                    "NO_ACCEPTED_CANDIDATES",
                    "engineer_acceptance.accepted_candidate_count",
                    case_id,
                )
            )
        accepted_refs = _accepted_refs(acceptance)
        if (
            len(accepted_refs) != acceptance.get("accepted_candidate_count")
            or _expected_digest(acceptance.get("candidate_set_sha256")) is None
        ):
            errors.append(
                _error(
                    "ACCEPTED_CANDIDATE_BINDING_INVALID",
                    "engineer_acceptance.accepted_candidate_refs",
                    case_id,
                )
            )
        if not _claims_match(
            payload,
            acceptance,
            (
                "source_sha256",
                "engineer_id",
                "evidence_ref",
                "candidate_set_sha256",
                "accepted_candidate_count",
                "accepted_candidate_refs",
            ),
        ):
            errors.append(
                _error(
                    "ENGINEER_ACCEPTANCE_ARTIFACT_MISMATCH",
                    "engineer_acceptance.artifact_ref",
                    case_id,
                )
            )

    _, candidate_errors = _validate_candidate_geometry(
        case.get("candidate_geometry"),
        root,
        source_digest,
        acceptance,
        case_id,
    )
    errors.extend(candidate_errors)

    accuracy = case.get("accuracy")
    if not isinstance(accuracy, Mapping):
        errors.append(_error("ACCURACY_EVIDENCE_MISSING", "accuracy", case_id))
    else:
        raw, artifact_errors = _verified_artifact(root, accuracy, "accuracy", case_id)
        errors.extend(artifact_errors)
        errors.extend(
            _validate_accuracy(
                accuracy,
                _json_mapping(raw),
                source_digest,
                _accepted_refs(acceptance),
                case_id,
            )
        )

    readback = case.get("live_readback")
    if not isinstance(readback, Mapping):
        errors.append(_error("LIVE_READBACK_MISSING", "live_readback", case_id))
    else:
        raw, artifact_errors = _verified_artifact(root, readback, "live_readback", case_id)
        errors.extend(artifact_errors)
        payload = _json_mapping(raw)
        if not _claims_match(
            payload,
            readback,
            (
                "source_sha256",
                "acceptance_sha256",
                "trace_sha256",
                "candidate_set_sha256",
                "accepted_candidate_refs",
                "job_id",
                "plan_hash",
                "adapter_type",
                "process_id",
                "document_id",
                "autocad_version",
                "pre_revision",
                "post_revision",
                "measured_geometry",
                "validation_report_sha256",
                "validation_passed",
            ),
        ):
            errors.append(
                _error("LIVE_READBACK_ARTIFACT_MISMATCH", "live_readback.artifact_ref", case_id)
            )
        errors.extend(
            _validate_readback_bindings(
                readback,
                source_digest=source_digest,
                trace=trace,
                acceptance=acceptance,
                accuracy=accuracy,
                case_id=case_id,
            )
        )
        if readback.get("validation_passed") is not True:
            errors.append(
                _error("LIVE_VALIDATION_FAILED", "live_readback.validation_passed", case_id)
            )
        if not _non_empty(readback.get("autocad_version")):
            errors.append(_error("LIVE_READBACK_INVALID", "live_readback.autocad_version", case_id))
        errors.extend(
            _validate_execution_receipt(
                case.get("execution_receipt"),
                root,
                readback,
                public_key_value=execution_public_key,
                expected_key_sha256=expected_execution_key_sha256,
                policy=policy,
                now=now,
                case_id=case_id,
            )
        )

    engineer_identity, attestation_errors = _verify_case_attestation(
        case.get("engineer_review_attestation"),
        expected_role=EvidenceRole.RASTER_ENGINEER_REVIEWER,
        exact_claims=raster_engineer_attestation_claims(case),
        policy=policy,
        expected_policy_sha256=expected_policy_sha256,
        now=now,
        field="engineer_review_attestation",
        case_id=case_id,
    )
    errors.extend(attestation_errors)
    accuracy_identity, attestation_errors = _verify_case_attestation(
        case.get("accuracy_review_attestation"),
        expected_role=EvidenceRole.RASTER_ACCURACY_REVIEWER,
        exact_claims=raster_accuracy_attestation_claims(case),
        policy=policy,
        expected_policy_sha256=expected_policy_sha256,
        now=now,
        field="accuracy_review_attestation",
        case_id=case_id,
    )
    errors.extend(attestation_errors)
    if engineer_identity is not None and engineer_identity == accuracy_identity:
        errors.append(
            _error("RASTER_REVIEWERS_NOT_INDEPENDENT", "accuracy_review_attestation", case_id)
        )
    return detected, quality, errors


def _validate_unique_evidence(cases: Sequence[object]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    seen_source_paths: set[str] = set()
    seen_source_hashes: set[str] = set()
    seen_artifact_paths: set[str] = set()
    seen_artifact_hashes: set[str] = set()
    seen_candidate_set_hashes: set[str] = set()
    seen_candidate_refs: set[str] = set()
    for index, raw_case in enumerate(cases):
        if not isinstance(raw_case, Mapping):
            continue
        case_id = _safe_case_id(raw_case.get("case_id"), index)
        source = raw_case.get("source")
        if isinstance(source, Mapping):
            source_path = source.get("artifact_ref")
            source_digest = _expected_digest(source.get("sha256"))
            normalized_path = (
                str(source_path).replace("\\", "/").casefold() if _non_empty(source_path) else None
            )
            if normalized_path is not None:
                if normalized_path in seen_source_paths:
                    errors.append(_error("SOURCE_PATH_REUSED", "source.artifact_ref", case_id))
                seen_source_paths.add(normalized_path)
            if source_digest is not None:
                if source_digest in seen_source_hashes:
                    errors.append(_error("SOURCE_HASH_REUSED", "source.sha256", case_id))
                seen_source_hashes.add(source_digest)
        for section_name in (
            "calibration",
            "trace",
            "candidate_geometry",
            "engineer_acceptance",
            "accuracy",
            "live_readback",
            "execution_receipt",
        ):
            section = raw_case.get(section_name)
            if not isinstance(section, Mapping):
                continue
            artifact_path = section.get("artifact_ref")
            artifact_digest = _expected_digest(section.get("sha256"))
            normalized_path = (
                str(artifact_path).replace("\\", "/").casefold()
                if _non_empty(artifact_path)
                else None
            )
            if normalized_path is not None:
                if normalized_path in seen_artifact_paths:
                    errors.append(
                        _error(
                            "EVIDENCE_ARTIFACT_PATH_REUSED", f"{section_name}.artifact_ref", case_id
                        )
                    )
                seen_artifact_paths.add(normalized_path)
            if artifact_digest is not None:
                if artifact_digest in seen_artifact_hashes:
                    errors.append(
                        _error("EVIDENCE_ARTIFACT_HASH_REUSED", f"{section_name}.sha256", case_id)
                    )
                seen_artifact_hashes.add(artifact_digest)
        acceptance = raw_case.get("engineer_acceptance")
        if isinstance(acceptance, Mapping):
            candidate_set_hash = _expected_digest(acceptance.get("candidate_set_sha256"))
            if candidate_set_hash is not None:
                if candidate_set_hash in seen_candidate_set_hashes:
                    errors.append(
                        _error(
                            "CANDIDATE_SET_HASH_REUSED",
                            "engineer_acceptance.candidate_set_sha256",
                            case_id,
                        )
                    )
                seen_candidate_set_hashes.add(candidate_set_hash)
            refs = acceptance.get("accepted_candidate_refs")
            if isinstance(refs, list):
                for ref in refs:
                    if not isinstance(ref, str):
                        continue
                    if ref in seen_candidate_refs:
                        errors.append(
                            _error(
                                "CANDIDATE_REF_REUSED",
                                "engineer_acceptance.accepted_candidate_refs",
                                case_id,
                            )
                        )
                    seen_candidate_refs.add(ref)
    return errors


def _load_trust_policy(
    path: Path | None,
) -> tuple[EvidenceTrustPolicy | None, dict[str, str] | None]:
    if path is None:
        return None, _error("TRUST_POLICY_MISSING", "trust_policy")
    payload = _read_stable_file(path)
    if payload is None:
        return None, _error("TRUST_POLICY_UNREADABLE", "trust_policy")
    try:
        raw = json.loads(payload.decode("utf-8"))
        policy = trust_policy_from_mapping(raw)
    except (UnicodeError, ValueError, EvidenceAttestationError):
        return None, _error("TRUST_POLICY_INVALID", "trust_policy")
    return policy, None


def verify_production_raster_acceptance(
    manifest_path: Path,
    trust_policy_path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    expected_policy_sha256: str | None = None,
    execution_public_key: str | None = None,
    expected_execution_key_sha256: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    environment = os.environ if env is None else env
    manifest_raw = _read_stable_file(manifest_path)
    try:
        manifest = json.loads(manifest_raw.decode("utf-8")) if manifest_raw is not None else None
    except (UnicodeError, ValueError):
        manifest = None
    if not isinstance(manifest, Mapping):
        return {
            "passed": False,
            "case_count": 0,
            "errors": [_error("MANIFEST_UNREADABLE", "manifest")],
        }
    if trust_policy_path is None:
        configured_policy = environment.get(TRUST_POLICY_ENV)
        trust_policy_path = (
            Path(configured_policy) if configured_policy and configured_policy.strip() else None
        )
    expected_policy_sha256 = expected_policy_sha256 or environment.get(TRUST_POLICY_SHA256_ENV, "")
    execution_public_key = execution_public_key or environment.get(EXECUTION_PUBLIC_KEY_ENV, "")
    expected_execution_key_sha256 = expected_execution_key_sha256 or environment.get(
        EXECUTION_KEY_SHA256_ENV, ""
    )
    errors: list[dict[str, str]] = []
    policy: EvidenceTrustPolicy | None
    policy_error: dict[str, str] | None
    try:
        same_file = trust_policy_path is not None and trust_policy_path.resolve(
            strict=True
        ) == manifest_path.resolve(strict=True)
    except OSError:
        same_file = False
    if same_file:
        policy = None
        policy_error = _error("TRUST_POLICY_NOT_SEPARATE", "trust_policy")
    else:
        policy, policy_error = _load_trust_policy(trust_policy_path)
    if policy_error is not None:
        errors.append(policy_error)
    if not expected_policy_sha256:
        errors.append(_error("TRUST_POLICY_DIGEST_MISSING", "trust_policy_sha256"))
    if not execution_public_key or not expected_execution_key_sha256:
        errors.append(_error("EXECUTION_TRUST_CONFIG_MISSING", "execution_trust"))

    raw_cases = manifest.get("cases")
    cases = raw_cases if isinstance(raw_cases, list) else []
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("evidence_kind") != "production_raster_acceptance"
    ):
        errors.append(_error("MANIFEST_SCHEMA_UNSUPPORTED", "schema_version"))
    for flag, expected in _PRODUCTION_FLAGS.items():
        if manifest.get(flag) is not expected:
            errors.append(_error("MANIFEST_PRODUCTION_FLAGS_INVALID", flag))
    if not MIN_CASES <= len(cases) <= MAX_CASES:
        errors.append(_error("CASE_COUNT_OUT_OF_RANGE", "cases"))
    errors.extend(_validate_unique_evidence(cases))
    seen: set[str] = set()
    detected: set[str] = set()
    qualities: set[str] = set()
    root = manifest_path.parent
    for index, raw_case in enumerate(cases):
        case_id = _safe_case_id(
            raw_case.get("case_id") if isinstance(raw_case, Mapping) else None, index
        )
        if not isinstance(raw_case, Mapping):
            errors.append(_error("CASE_INVALID", "case", case_id))
            continue
        if raw_case.get("case_id") != case_id:
            errors.append(_error("CASE_ID_INVALID", "case_id", case_id))
        elif case_id in seen:
            errors.append(_error("CASE_ID_DUPLICATE", "case_id", case_id))
        seen.add(case_id)
        case_types, quality, case_errors = _validate_case(
            raw_case,
            root,
            case_id,
            policy,
            expected_policy_sha256,
            execution_public_key,
            expected_execution_key_sha256,
            now,
        )
        detected.update(case_types)
        if quality is not None:
            qualities.add(quality)
        errors.extend(case_errors)
    if REQUIRED_PRIMITIVES - detected:
        errors.append(_error("PRIMITIVE_COVERAGE_INCOMPLETE", "cases"))
    if "noisy" not in qualities:
        errors.append(_error("NOISY_SCAN_COVERAGE_MISSING", "cases"))
    errors.sort(key=lambda item: (item.get("case_id", ""), item["code"], item["field"]))
    return {"passed": not errors, "case_count": len(cases), "errors": errors}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--trust-policy", type=Path)
    parser.add_argument("--trust-policy-sha256")
    parser.add_argument("--execution-public-key")
    parser.add_argument("--execution-key-sha256")
    args = parser.parse_args(argv)
    result = verify_production_raster_acceptance(
        args.manifest,
        args.trust_policy,
        expected_policy_sha256=args.trust_policy_sha256,
        execution_public_key=args.execution_public_key,
        expected_execution_key_sha256=args.execution_key_sha256,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
