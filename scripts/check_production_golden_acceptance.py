"""Fail-closed verifier for the engineer-reviewed production golden corpus.

Artifacts are immutable byte snapshots. The verifier recompiles design evidence
and recomputes DXF takeoff evidence with production code; human trust evidence is
checked separately and is never manufactured by the verifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, cast

import ezdxf
import yaml
from pydantic import ValidationError

from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
from cad_harness.adapters.fake import FakeAutoCADAdapter, FakeDocument
from cad_harness.application.services.plan_compiler import PlanCompilerService
from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.comprehension.takeoff import compute_takeoff
from cad_harness.domain.canonical import canonical_json, sha256_of
from cad_harness.domain.errors import HarnessError
from cad_harness.domain.models.drawing_model import DrawingModel, ReadScope
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.result import CommitResult
from cad_harness.domain.models.takeoff import MaterialTable, TakeoffReport, TakeoffRequest
from cad_harness.domain.models.validation import ValidationReport, ValidationStage
from cad_harness.domain.ports.autocad_adapter import CommitRequest
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef
from cad_harness.security.evidence_attestation import (
    EvidenceAttestationError,
    EvidenceRole,
    EvidenceTrustPolicy,
    JsonValue,
    evidence_attestation_from_mapping,
    trust_policy_from_mapping,
    verify_attestation,
    verify_trust_policy_digest,
)
from cad_harness.validation.engine import RuleContext, default_engine

type JsonMapping = Mapping[str, Any]
type Error = dict[str, str]

MIN_CASES: Final = 30
MAX_CASES: Final = 50
MIN_TAKEOFF_CASES: Final = 5
MAX_MANIFEST_BYTES: Final = 2 * 1024 * 1024
MAX_STRUCTURED_ARTIFACT_BYTES: Final = 16 * 1024 * 1024
MAX_PREVIEW_BYTES: Final = 64 * 1024 * 1024
MAX_DRAWING_BYTES: Final = 512 * 1024 * 1024
MAX_TRUST_POLICY_BYTES: Final = 1024 * 1024
MANIFEST_KIND: Final = "reviewed_production_golden_corpus"
APPROVED_STATUS: Final = "approved"
TRUST_POLICY_ENV: Final = "CAD_HARNESS_EVIDENCE_TRUST_POLICY"
TRUST_POLICY_SHA256_ENV: Final = "CAD_HARNESS_EVIDENCE_TRUST_POLICY_SHA256"
_PRODUCTION_SOURCE_CLASSES: Final = frozenset({"customer_local_reviewed", "licensed_production"})
_SAFE_CASE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_SVG_GEOMETRY: Final = re.compile(
    rb"<(?:[A-Za-z_][\w.-]*:)?(?:path|line|polyline|polygon|circle|ellipse|rect)\b",
    re.IGNORECASE,
)
_PLACEHOLDER_VALUES: Final = frozenset(
    {"dummy", "example", "lorem ipsum", "n/a", "placeholder", "sample", "tbd", "todo"}
)
_BASE_ARTIFACTS: Final = {
    "input_spec": "json",
    "company_profile": "yaml",
    "expected_plan": "json",
    "expected_semantic_entities": "json",
    "expected_validation": "json",
    "preview_reference": "preview",
}
_TAKEOFF_ARTIFACTS: Final = {
    "input_drawing": "takeoff_drawing",
    "takeoff_request": "json",
    "expected_takeoff": "json",
}
_SUFFIXES: Final = {
    "json": frozenset({".json"}),
    "yaml": frozenset({".yaml", ".yml"}),
    "preview": frozenset({".jpeg", ".jpg", ".png", ".svg"}),
    "drawing": frozenset({".dwg", ".dxf"}),
    "takeoff_drawing": frozenset({".dxf"}),
}


class _DuplicateJsonKeyError(ValueError):
    """Raised before a duplicate JSON member can replace evidence silently."""


@dataclass(frozen=True, slots=True)
class _Artifact:
    reference: str
    path_key: str
    suffix: str
    data: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class _SourceBinding:
    artifact: _Artifact
    provenance_sha256: str | None
    source_class: str | None
    synthetic: bool | None
    development_fixture: bool | None
    semantic_evidence_sha256: str | None
    drawing_model_sha256: str | None


@dataclass(slots=True)
class _ArtifactLocker:
    """Resolve and read every unique corpus file at most once."""

    root: Path
    _cache: dict[str, _Artifact | None] = dataclass_field(default_factory=dict)

    def lock(self, reference: object, *, max_bytes: int) -> _Artifact | None:
        resolved = _resolve_artifact_path(self.root, reference)
        if resolved is None:
            return None
        path_key = str(resolved).casefold()
        if path_key in self._cache:
            artifact = self._cache[path_key]
            return artifact if artifact is not None and len(artifact.data) <= max_bytes else None
        data = _read_file_once(resolved, max_bytes=max_bytes)
        if data is None:
            self._cache[path_key] = None
            return None
        artifact = _Artifact(
            reference=str(reference),
            path_key=path_key,
            suffix=resolved.suffix.casefold(),
            data=data,
            sha256=hashlib.sha256(data).hexdigest(),
        )
        self._cache[path_key] = artifact
        return artifact


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
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


def _public_case_id(index: int) -> str:
    return f"case-{index + 1:03d}"


def _non_empty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _identity(value: object) -> str | None:
    return str(value).strip().casefold() if _non_empty(value) else None


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & flag)


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_mode),
    )


def _read_file_once(path: Path, *, max_bytes: int) -> bytes | None:
    """Read one stable regular-file snapshot and detect swaps during the read."""
    try:
        path_before = os.lstat(path)
        if (
            not stat.S_ISREG(path_before.st_mode)
            or _is_reparse(path_before)
            or not 0 < path_before.st_size <= max_bytes
        ):
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        handle_before = os.fstat(descriptor)
        if _file_signature(handle_before) != _file_signature(path_before):
            return None
        remaining = int(handle_before.st_size)
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            return None
        handle_after = os.fstat(descriptor)
        path_after = os.lstat(path)
        if (
            _file_signature(handle_before) != _file_signature(handle_after)
            or _file_signature(handle_after) != _file_signature(path_after)
            or _is_reparse(path_after)
        ):
            return None
        data = b"".join(chunks)
        return data if len(data) == handle_after.st_size else None
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _load_trust_policy(path: Path) -> EvidenceTrustPolicy | None:
    data = _read_file_once(path, max_bytes=MAX_TRUST_POLICY_BYTES)
    if data is None or path.suffix.casefold() != ".json":
        return None
    payload = _parse_mapping(data, suffix=".json")
    if payload is None:
        return None
    try:
        return trust_policy_from_mapping(dict(payload))
    except EvidenceAttestationError:
        return None


def _trusted_attestation(
    *,
    raw: object,
    expected_role: EvidenceRole,
    exact_claims: JsonValue,
    policy: EvidenceTrustPolicy,
    expected_policy_sha256: str,
    field: str,
    case_id: str,
    claimed_identity: object = None,
    now: datetime | None = None,
) -> tuple[str | None, list[Error]]:
    try:
        attestation = evidence_attestation_from_mapping(raw)
    except EvidenceAttestationError:
        return None, [_error("EVIDENCE_ATTESTATION_INVALID", field, case_id)]
    if attestation.role is not expected_role:
        return None, [_error("EVIDENCE_ATTESTATION_ROLE_MISMATCH", field, case_id)]
    try:
        identity = verify_attestation(
            policy,
            attestation,
            exact_claims,
            expected_policy_sha256=expected_policy_sha256,
            now=now,
        )
    except EvidenceAttestationError as exc:
        return None, [_error(exc.code.value, field, case_id)]
    if claimed_identity is not None and (
        not _non_empty(claimed_identity) or claimed_identity != identity.identity_id
    ):
        return None, [_error("EVIDENCE_ATTESTATION_IDENTITY_MISMATCH", field, case_id)]
    return identity.identity_id, []


def _parse_mapping(data: bytes, *, suffix: str) -> JsonMapping | None:
    try:
        text = data.decode("utf-8")
        if suffix in {".yaml", ".yml"}:
            value = yaml.safe_load(text)
        else:
            value = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeError, ValueError, TypeError, yaml.YAMLError):
        return None
    return value if isinstance(value, Mapping) and value else None


def _read_manifest(path: Path) -> JsonMapping | None:
    data = _read_file_once(path, max_bytes=MAX_MANIFEST_BYTES)
    if data is None:
        return None
    payload = _parse_mapping(data, suffix=path.suffix.casefold())
    return payload if payload is not None and not _contains_placeholder(payload) else None


def _resolve_artifact_path(root: Path, reference: object) -> Path | None:
    if not isinstance(reference, str) or not reference or "\\" in reference:
        return None
    relative = Path(reference)
    if (
        relative.is_absolute()
        or relative.drive
        or not relative.parts
        or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
    ):
        return None
    try:
        resolved_root = root.resolve(strict=True)
        current = resolved_root
        for part in relative.parts:
            current /= part
            metadata = os.lstat(current)
            if _is_reparse(metadata):
                return None
        resolved = current.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_relative_to(resolved_root) else None


def _contains_placeholder(value: object, *, top_level: bool = True) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in _PLACEHOLDER_VALUES
    if isinstance(value, Mapping):
        if top_level and not value:
            return True
        if "placeholder" in {str(key).casefold() for key in value}:
            return True
        return any(_contains_placeholder(item, top_level=False) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_placeholder(item, top_level=False) for item in value)
    return False


def _dxf_has_geometry(data: bytes) -> bool:
    normalized = b"\n" + data.replace(b"\r\n", b"\n").upper()
    entity_markers = tuple(
        f"\n0\n{entity}\n".encode()
        for entity in ("ARC", "CIRCLE", "ELLIPSE", "LINE", "LWPOLYLINE", "POLYLINE", "SPLINE")
    )
    return (
        b"\n0\nSECTION\n" in normalized
        and b"\n2\nENTITIES\n" in normalized
        and any(marker in normalized for marker in entity_markers)
        and b"\n0\nEOF" in normalized
    )


def _content_is_valid(artifact: _Artifact, kind: str) -> bool:
    data = artifact.data
    if kind in {"json", "yaml"}:
        payload = _parse_mapping(data, suffix=artifact.suffix)
        return payload is not None and not _contains_placeholder(payload)
    if kind in {"drawing", "takeoff_drawing"}:
        if artifact.suffix == ".dxf":
            return _dxf_has_geometry(data)
        return kind == "drawing" and len(data) >= 64 and re.match(rb"^AC10\d{2}", data) is not None
    if artifact.suffix == ".svg":
        stripped = data.lstrip()
        return b"<svg" in stripped[:512].lower() and _SVG_GEOMETRY.search(data) is not None
    if artifact.suffix == ".png":
        return len(data) >= 33 and data.startswith(b"\x89PNG\r\n\x1a\n")
    if artifact.suffix in {".jpg", ".jpeg"}:
        return len(data) >= 128 and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    return False


def _max_bytes(kind: str) -> int:
    if kind in {"drawing", "takeoff_drawing"}:
        return MAX_DRAWING_BYTES
    if kind == "preview":
        return MAX_PREVIEW_BYTES
    return MAX_STRUCTURED_ARTIFACT_BYTES


def _locked_artifact(
    locker: _ArtifactLocker,
    pointer: object,
    *,
    field: str,
    kind: str,
    case_id: str,
) -> tuple[_Artifact | None, list[Error]]:
    if not isinstance(pointer, Mapping):
        return None, [_error("ARTIFACT_BINDING_MISSING", field, case_id)]
    errors: list[Error] = []
    expected = pointer.get("sha256")
    if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
        errors.append(_error("ARTIFACT_HASH_INVALID", f"{field}.sha256", case_id))
        expected = None
    artifact = locker.lock(pointer.get("artifact_ref"), max_bytes=_max_bytes(kind))
    if artifact is None:
        errors.append(_error("ARTIFACT_INVALID", f"{field}.artifact_ref", case_id))
        return None, errors
    if artifact.suffix not in _SUFFIXES[kind]:
        errors.append(_error("ARTIFACT_TYPE_UNSUPPORTED", f"{field}.artifact_ref", case_id))
    if not _content_is_valid(artifact, kind):
        errors.append(_error("ARTIFACT_CONTENT_INVALID", f"{field}.artifact_ref", case_id))
    if expected is not None and artifact.sha256 != expected:
        errors.append(_error("ARTIFACT_HASH_MISMATCH", f"{field}.sha256", case_id))
    return artifact, errors


def _artifact_mapping(artifact: _Artifact | None) -> JsonMapping | None:
    return _parse_mapping(artifact.data, suffix=artifact.suffix) if artifact is not None else None


def _canonical_equal(left: object, right: object) -> bool:
    try:
        return canonical_json(left) == canonical_json(right)
    except (TypeError, ValueError):
        return False


def _validate_profile(
    artifact: _Artifact | None, case_id: str
) -> tuple[CompanyProfile | None, list[Error]]:
    try:
        profile = CompanyProfile.model_validate(_artifact_mapping(artifact))
    except (ValidationError, TypeError, ValueError):
        return None, [_error("COMPANY_PROFILE_INVALID", "artifacts.company_profile", case_id)]
    errors: list[Error] = []
    if not _non_empty(profile.profile_id) or not _non_empty(profile.version):
        errors.append(_error("COMPANY_PROFILE_INVALID", "artifacts.company_profile", case_id))
    if not profile.company_approved:
        errors.append(
            _error("COMPANY_PROFILE_UNAPPROVED", "company_profile.company_approved", case_id)
        )
    return profile, errors


def _source_semantic_projection(model: DrawingModel, source_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source_sha256": source_sha256,
        "revision": model.revision,
        "source_unit_code": model.source_unit_code,
        "to_mm_factor": model.to_mm_factor,
        "geometry_normalized": model.geometry_normalized,
        "scope": model.scope.model_dump(mode="json", exclude_none=True),
        "entities": [
            entity.model_dump(mode="json", exclude_none=True) for entity in model.entities
        ],
        "layers": [layer.model_dump(mode="json", exclude_none=True) for layer in model.layers],
        "unsupported": [
            item.model_dump(mode="json", exclude_none=True) for item in model.unsupported
        ],
        "coverage_complete": model.coverage_complete,
    }


def _read_dxf_snapshot(artifact: _Artifact, profile: CompanyProfile | None) -> DrawingModel | None:
    if profile is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="cad-harness-source-") as temp_dir:
            snapshot = Path(temp_dir) / "source.dxf"
            snapshot.write_bytes(artifact.data)
            return DxfDrawingReader(profile.tolerance()).read(
                DrawingReadRequest(
                    source=DrawingSourceRef(kind="file", format="dxf", ref=str(snapshot)),
                    scope=ReadScope(kind="model_space"),
                    max_entities=20_000,
                    max_block_nesting_depth=10,
                )
            )
    except (
        HarnessError,
        OSError,
        ValidationError,
        ezdxf.DXFError,
        KeyError,
        TypeError,
        ValueError,
        ArithmeticError,
    ):
        return None


def _entity_measurements(entity: object) -> dict[str, Any]:
    if not hasattr(entity, "geometry") or not hasattr(entity, "bounding_box_mm"):
        return {}
    geometry = entity.geometry
    x_min, y_min, x_max, y_max = entity.bounding_box_mm
    result: dict[str, Any] = {
        "width_mm": x_max - x_min,
        "height_mm": y_max - y_min,
    }
    if geometry.kind == "polyline":
        points = [vertex.point_mm for vertex in geometry.vertices]
        result["closed"] = geometry.closed
        result["vertex_count"] = len(points)
        if points and not any(vertex.bulge for vertex in geometry.vertices):
            segments = list(pairwise(points))
            if geometry.closed and len(points) > 1:
                segments.append((points[-1], points[0]))
            result["perimeter_mm"] = sum(
                math.hypot(end[0] - start[0], end[1] - start[1]) for start, end in segments
            )
            if geometry.closed and len(points) >= 3:
                result["area_mm2"] = abs(
                    sum(
                        start[0] * end[1] - end[0] * start[1]
                        for start, end in zip(points, [*points[1:], points[0]], strict=True)
                    )
                    / 2.0
                )
    elif geometry.kind == "circle":
        result.update(
            {
                "radius_mm": geometry.radius_mm,
                "diameter_mm": geometry.radius_mm * 2.0,
                "area_mm2": math.pi * geometry.radius_mm**2,
                "perimeter_mm": 2.0 * math.pi * geometry.radius_mm,
            }
        )
    elif geometry.kind == "line":
        result["length_mm"] = math.hypot(
            geometry.end_mm[0] - geometry.start_mm[0],
            geometry.end_mm[1] - geometry.start_mm[1],
        )
    return result


def _measurement_equal(left: object, right: object) -> bool:
    if type(left) in {int, float} and type(right) in {int, float}:
        return math.isclose(cast(float, left), cast(float, right), rel_tol=1e-9, abs_tol=1e-6)
    return left == right


def _drawing_model_matches_expected(model: DrawingModel, expected: JsonMapping | None) -> bool:
    if expected is None or expected.get("entity_count") != len(model.entities):
        return False
    raw_entities = expected.get("entities")
    if not isinstance(raw_entities, list) or len(raw_entities) != len(model.entities):
        return False
    remaining = list(model.entities)
    for raw_expected in raw_entities:
        if not isinstance(raw_expected, Mapping):
            return False
        expected_measurements = raw_expected.get("measurements")
        if not isinstance(expected_measurements, Mapping):
            return False

        match_index = None
        for index, entity in enumerate(remaining):
            if entity.entity_type != raw_expected.get(
                "entity_type"
            ) or entity.layer != raw_expected.get("layer"):
                continue
            actual = _entity_measurements(entity)
            if all(
                key in actual and _measurement_equal(actual[key], expected_value)
                for key, expected_value in expected_measurements.items()
            ):
                match_index = index
                break
        if match_index is None:
            return False
        remaining.pop(match_index)
    return not remaining


def _validate_provenance(
    source: Mapping[str, object], locker: _ArtifactLocker, case_id: str, source_hash: str | None
) -> tuple[_Artifact | None, list[Error]]:
    artifact, errors = _locked_artifact(
        locker,
        source.get("provenance"),
        field="source_drawing.provenance",
        kind="json",
        case_id=case_id,
    )
    payload = _artifact_mapping(artifact)
    required = {
        "schema_version",
        "evidence_kind",
        "source_sha256",
        "source_class",
        "synthetic",
        "development_fixture",
        "provenance_type",
        "provenance",
    }
    if payload is None or set(payload) != required:
        errors.append(_error("SOURCE_PROVENANCE_INVALID", "source_drawing.provenance", case_id))
        return artifact, errors
    source_class = payload.get("source_class")
    provenance_type = payload.get("provenance_type")
    details = payload.get("provenance")
    valid_details = False
    provenance_ref = None
    if provenance_type == "customer" and isinstance(details, Mapping):
        valid_details = set(details) == {"customer_record_ref", "custodian_ref"} and all(
            _non_empty(details.get(field)) for field in ("customer_record_ref", "custodian_ref")
        )
        provenance_ref = details.get("customer_record_ref")
    elif provenance_type == "licensed" and isinstance(details, Mapping):
        valid_details = set(details) == {"license_id", "source_ref", "attribution_ref"} and all(
            _non_empty(details.get(field))
            for field in ("license_id", "source_ref", "attribution_ref")
        )
        provenance_ref = details.get("source_ref")
    if (
        payload.get("schema_version") != "1.0"
        or payload.get("evidence_kind") != "production_source_provenance"
        or payload.get("source_sha256") != source_hash
        or source_class not in _PRODUCTION_SOURCE_CLASSES
        or payload.get("synthetic") is not False
        or payload.get("development_fixture") is not False
        or not valid_details
        or source.get("provenance_ref") != provenance_ref
        or any(
            source.get(field) != payload.get(field)
            for field in ("source_class", "synthetic", "development_fixture")
        )
    ):
        errors.append(_error("SOURCE_PROVENANCE_INVALID", "source_drawing.provenance", case_id))
    return artifact, errors


def _validate_source(
    case: JsonMapping,
    locker: _ArtifactLocker,
    case_id: str,
    *,
    profile: CompanyProfile | None,
    expected_semantic_artifact: _Artifact | None,
) -> tuple[_SourceBinding | None, list[Error]]:
    source = case.get("source_drawing")
    if not isinstance(source, Mapping):
        return None, [_error("SOURCE_PROVENANCE_MISSING", "source_drawing", case_id)]
    artifact, errors = _locked_artifact(
        locker, source, field="source_drawing", kind="drawing", case_id=case_id
    )
    provenance, provenance_errors = _validate_provenance(
        source, locker, case_id, artifact.sha256 if artifact else None
    )
    errors.extend(provenance_errors)
    source_class = source.get("source_class")
    synthetic = source.get("synthetic")
    development = source.get("development_fixture")
    if (
        source_class not in _PRODUCTION_SOURCE_CLASSES
        or synthetic is not False
        or development is not False
    ):
        errors.append(_error("SOURCE_NOT_PRODUCTION", "source_drawing.source_class", case_id))
    if artifact is None:
        return None, errors
    semantic_evidence: _Artifact | None = None
    model_artifact: _Artifact | None = None
    if artifact.suffix == ".dxf":
        model = _read_dxf_snapshot(artifact, profile)
        semantic_evidence, snapshot_errors = _locked_artifact(
            locker,
            source.get("semantic_snapshot"),
            field="source_drawing.semantic_snapshot",
            kind="json",
            case_id=case_id,
        )
        errors.extend(snapshot_errors)
        expected_snapshot = _artifact_mapping(semantic_evidence)
        if model is None:
            errors.append(_error("DXF_SOURCE_READ_FAILED", "source_drawing", case_id))
        elif not model.coverage_complete or model.unsupported:
            errors.append(_error("SOURCE_COVERAGE_INCOMPLETE", "source_drawing", case_id))
        elif not _canonical_equal(
            expected_snapshot, _source_semantic_projection(model, artifact.sha256)
        ):
            errors.append(
                _error("SOURCE_SEMANTIC_MISMATCH", "source_drawing.semantic_snapshot", case_id)
            )
    else:
        model_artifact, model_errors = _locked_artifact(
            locker,
            source.get("drawing_model"),
            field="source_drawing.drawing_model",
            kind="json",
            case_id=case_id,
        )
        errors.extend(model_errors)
        try:
            model = DrawingModel.model_validate(_artifact_mapping(model_artifact))
        except (ValidationError, TypeError, ValueError):
            model = None
            errors.append(
                _error("DWG_DRAWING_MODEL_INVALID", "source_drawing.drawing_model", case_id)
            )
        semantic_evidence, bridge_errors = _locked_artifact(
            locker,
            source.get("bridge_evidence"),
            field="source_drawing.bridge_evidence",
            kind="json",
            case_id=case_id,
        )
        errors.extend(bridge_errors)
        bridge = _artifact_mapping(semantic_evidence)
        projection = (
            _source_semantic_projection(model, artifact.sha256) if model is not None else None
        )
        expected_bridge = {
            "schema_version": "1.0",
            "evidence_kind": "dotnet_bridge_live_dwg_read",
            "source_sha256": artifact.sha256,
            "drawing_model_sha256": model_artifact.sha256 if model_artifact else None,
            "document_revision": model.revision if model else None,
            "adapter_type": "dotnet_bridge",
            "cad_version": bridge.get("cad_version") if isinstance(bridge, Mapping) else None,
            "coverage_complete": True,
            "semantic_projection_sha256": sha256_of(projection) if projection is not None else None,
            "expected_semantic_sha256": (
                expected_semantic_artifact.sha256 if expected_semantic_artifact else None
            ),
        }
        if (
            bridge is None
            or set(bridge) != set(expected_bridge)
            or not _non_empty(bridge.get("cad_version"))
            or not _canonical_equal(bridge, expected_bridge)
            or model is None
            or not model.coverage_complete
            or bool(model.unsupported)
        ):
            errors.append(
                _error("DWG_BRIDGE_EVIDENCE_INVALID", "source_drawing.bridge_evidence", case_id)
            )
        if model is not None and not _drawing_model_matches_expected(
            model, _artifact_mapping(expected_semantic_artifact)
        ):
            errors.append(
                _error("SOURCE_SEMANTIC_MISMATCH", "source_drawing.drawing_model", case_id)
            )
    return (
        _SourceBinding(
            artifact=artifact,
            provenance_sha256=provenance.sha256 if provenance else None,
            source_class=str(source_class) if isinstance(source_class, str) else None,
            synthetic=synthetic if isinstance(synthetic, bool) else None,
            development_fixture=development if isinstance(development, bool) else None,
            semantic_evidence_sha256=(
                semantic_evidence.sha256 if semantic_evidence is not None else None
            ),
            drawing_model_sha256=model_artifact.sha256 if model_artifact else None,
        ),
        errors,
    )


def _plan_projection(plan: OperationPlan) -> dict[str, Any]:
    return {
        "canonical_units": plan.canonical_units.value,
        "profile_ref": plan.profile_ref,
        "operations": [
            operation.model_dump(mode="json", exclude_none=True) for operation in plan.operations
        ],
    }


def _validation_projection(report: ValidationReport) -> dict[str, Any]:
    return {
        "stage": report.stage.value,
        "blocking_count": report.blocking_count,
        "error_count": report.error_count,
        "warning_count": report.warning_count,
        "commit_allowed": report.gate_allows_commit(),
        "findings": sorted(
            (
                {"rule_id": finding.rule_id, "severity": finding.severity.value}
                for finding in report.findings
            ),
            key=lambda item: (item["rule_id"], item["severity"]),
        ),
    }


def _semantic_projection(plan: OperationPlan, result: CommitResult) -> dict[str, Any]:
    operations = {operation.operation_id: operation for operation in plan.operations}
    entities: list[dict[str, Any]] = []
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
        entities.append(payload)
    return {"entity_count": len(entities), "entities": entities}


def _validate_engineering_outputs(
    artifacts: Mapping[str, _Artifact], profile: CompanyProfile | None, case_id: str
) -> list[Error]:
    errors: list[Error] = []
    if profile is None:
        return errors
    try:
        spec = DrawingSpec.model_validate(_artifact_mapping(artifacts.get("input_spec")))
    except (ValidationError, TypeError, ValueError):
        return [_error("INPUT_SPEC_INVALID", "artifacts.input_spec", case_id)]
    if not spec.features:
        errors.append(_error("INPUT_SPEC_EMPTY", "artifacts.input_spec", case_id))
        return errors
    if spec.standard_profile.as_ref() != profile.as_ref():
        errors.append(_error("INPUT_SPEC_PROFILE_MISMATCH", "input_spec.standard_profile", case_id))
        return errors

    adapter = FakeAutoCADAdapter(FakeDocument(document_id=spec.document_id))
    expected_revision = adapter.current_revision()
    compiler = PlanCompilerService(profile, profile.tolerance(), adapter)
    try:
        first = compiler.compile(
            spec, job_id="job-production-golden-verifier", expected_revision=expected_revision
        )
        second = compiler.compile(
            spec, job_id="job-production-golden-verifier", expected_revision=expected_revision
        )
    except (HarnessError, KeyError, TypeError, ValueError, ArithmeticError):
        return [_error("PLAN_COMPILATION_FAILED", "artifacts.input_spec", case_id)]
    if first.plan is None or first.missing_inputs:
        return [_error("INPUT_SPEC_INCOMPLETE", "artifacts.input_spec", case_id)]
    if second.plan is None or second.missing_inputs:
        return [_error("PLAN_NONDETERMINISTIC", "artifacts.input_spec", case_id)]
    plan = first.plan
    actual_plan = _plan_projection(plan)
    if not _canonical_equal(actual_plan, _plan_projection(second.plan)):
        errors.append(_error("PLAN_NONDETERMINISTIC", "artifacts.input_spec", case_id))
    expected_plan = _artifact_mapping(artifacts.get("expected_plan"))
    if expected_plan is None:
        errors.append(_error("EXPECTED_PLAN_INVALID", "artifacts.expected_plan", case_id))
    elif not _canonical_equal(expected_plan, actual_plan):
        errors.append(_error("EXPECTED_PLAN_MISMATCH", "artifacts.expected_plan", case_id))

    try:
        report = default_engine().run(
            ValidationStage.PRE_COMMIT,
            RuleContext(plan=plan, profile=profile, tolerance=profile.tolerance()),
            job_id="job-production-golden-verifier",
        )
    except (HarnessError, KeyError, TypeError, ValueError, ArithmeticError):
        errors.append(_error("VALIDATION_DERIVATION_FAILED", "artifacts.input_spec", case_id))
        return errors
    expected_validation = _artifact_mapping(artifacts.get("expected_validation"))
    if expected_validation is None:
        errors.append(
            _error("EXPECTED_VALIDATION_INVALID", "artifacts.expected_validation", case_id)
        )
    else:
        normalized_expected = dict(expected_validation)
        findings = normalized_expected.get("findings")
        if isinstance(findings, list) and all(isinstance(item, Mapping) for item in findings):
            normalized_expected["findings"] = sorted(
                findings,
                key=lambda item: (str(item.get("rule_id")), str(item.get("severity"))),
            )
        if not _canonical_equal(normalized_expected, _validation_projection(report)):
            errors.append(
                _error("EXPECTED_VALIDATION_MISMATCH", "artifacts.expected_validation", case_id)
            )

    try:
        commit_result = adapter.commit(
            CommitRequest(
                plan=plan,
                idempotency_key="production-golden-verifier",
                expected_revision=expected_revision,
                approval_token="human-evidence-verified-separately",
                create_checkpoint=False,
            )
        )
    except (HarnessError, KeyError, TypeError, ValueError, ArithmeticError):
        errors.append(_error("SEMANTIC_DERIVATION_FAILED", "artifacts.input_spec", case_id))
        return errors
    expected_semantic = _artifact_mapping(artifacts.get("expected_semantic_entities"))
    if expected_semantic is None:
        errors.append(
            _error("EXPECTED_SEMANTIC_INVALID", "artifacts.expected_semantic_entities", case_id)
        )
    elif not _canonical_equal(expected_semantic, _semantic_projection(plan, commit_result)):
        errors.append(
            _error("EXPECTED_SEMANTIC_MISMATCH", "artifacts.expected_semantic_entities", case_id)
        )
    return errors


def _validate_review(
    case: JsonMapping,
    locker: _ArtifactLocker,
    case_id: str,
    *,
    artifact_hashes: Mapping[str, str],
    source_hash: str | None,
) -> tuple[tuple[str, str, str] | None, _Artifact | None, list[Error]]:
    review = case.get("review")
    if not isinstance(review, Mapping):
        return None, None, [_error("REVIEW_MISSING", "review", case_id)]
    errors: list[Error] = []
    reviewer = review.get("reviewer_identity")
    selector = case.get("selector_identity")
    if not _non_empty(reviewer) or not _non_empty(review.get("evidence_ref")):
        errors.append(_error("REVIEW_EVIDENCE_MISSING", "review", case_id))
    if not _non_empty(selector):
        errors.append(_error("SELECTOR_IDENTITY_MISSING", "selector_identity", case_id))
    elif _identity(selector) == _identity(reviewer):
        errors.append(_error("SELECTION_NOT_INDEPENDENT", "selector_identity", case_id))
    artifact, artifact_errors = _locked_artifact(
        locker, review, field="review", kind="json", case_id=case_id
    )
    errors.extend(artifact_errors)
    payload = _artifact_mapping(artifact)
    expected_claims = {
        "evidence_kind": "golden_case_review",
        "case_id": case.get("case_id"),
        "reviewer_identity": reviewer,
        "evidence_ref": review.get("evidence_ref"),
        "accepted": True,
        "source_sha256": source_hash,
        "artifact_sha256": dict(artifact_hashes),
    }
    if payload is None or any(payload.get(key) != value for key, value in expected_claims.items()):
        errors.append(_error("REVIEW_EVIDENCE_INVALID", "review.artifact_ref", case_id))
    unique_key = None
    if artifact is not None and _non_empty(review.get("evidence_ref")):
        unique_key = (str(review["evidence_ref"]), artifact.path_key, artifact.sha256)
    return unique_key, artifact, errors


def _recompute_takeoff(
    *,
    source: _Artifact | None,
    request_artifact: _Artifact | None,
    expected_artifact: _Artifact | None,
    table: MaterialTable | None,
    profile: CompanyProfile | None,
    case_id: str,
) -> tuple[str | None, list[Error]]:
    if source is None or request_artifact is None or expected_artifact is None or table is None:
        return None, []
    try:
        request = TakeoffRequest.model_validate(_artifact_mapping(request_artifact))
        expected = TakeoffReport.model_validate(_artifact_mapping(expected_artifact))
    except (ValidationError, TypeError, ValueError):
        return None, [_error("TAKEOFF_CONTRACT_INVALID", "artifacts.expected_takeoff", case_id)]
    errors: list[Error] = []
    expected_ref = f"{table.profile_id}@{table.version}"
    if request.material_profile_ref != expected_ref:
        errors.append(
            _error(
                "TAKEOFF_REQUEST_PROFILE_MISMATCH", "takeoff_request.material_profile_ref", case_id
            )
        )
        return None, errors
    if profile is None or profile.material_profile_ref != expected_ref:
        errors.append(
            _error(
                "COMPANY_MATERIAL_PROFILE_MISMATCH",
                "company_profile.material_profile_ref",
                case_id,
            )
        )
        return None, errors
    if not request.parts:
        errors.append(_error("TAKEOFF_REQUEST_EMPTY", "artifacts.takeoff_request", case_id))
        return None, errors
    try:
        with tempfile.TemporaryDirectory(prefix="cad-harness-golden-") as temp_dir:
            snapshot = Path(temp_dir) / "input.dxf"
            snapshot.write_bytes(source.data)
            model = DxfDrawingReader(profile.tolerance()).read(
                DrawingReadRequest(
                    source=DrawingSourceRef(kind="file", format="dxf", ref=str(snapshot)),
                    scope=ReadScope(kind="model_space"),
                    max_entities=20_000,
                    max_block_nesting_depth=10,
                )
            )
        model = model.model_copy(update={"document_id": request.document_id})
        actual = compute_takeoff(
            model,
            request,
            materials=table,
            tolerance=profile.tolerance(),
        )
    except (
        HarnessError,
        OSError,
        ValidationError,
        ezdxf.DXFError,
        KeyError,
        TypeError,
        ValueError,
        ArithmeticError,
    ):
        return None, [_error("TAKEOFF_RECOMPUTATION_FAILED", "artifacts.takeoff_request", case_id)]
    if expected.revision != f"sha256:{source.sha256}":
        errors.append(
            _error("EXPECTED_TAKEOFF_REVISION_MISMATCH", "expected_takeoff.revision", case_id)
        )
    expected_payload = expected.model_dump(mode="json", exclude_none=True)
    actual_payload = actual.model_dump(mode="json", exclude_none=True)
    if not _canonical_equal(expected_payload, actual_payload):
        errors.append(_error("EXPECTED_TAKEOFF_MISMATCH", "artifacts.expected_takeoff", case_id))
    return sha256_of(actual_payload), errors


def _validate_takeoff(
    case: JsonMapping,
    locker: _ArtifactLocker,
    case_id: str,
    *,
    artifacts: Mapping[str, object],
    locked_artifacts: Mapping[str, _Artifact],
    artifact_hashes: Mapping[str, str],
    profile: CompanyProfile | None,
    source: _Artifact | None,
    policy: EvidenceTrustPolicy,
    expected_policy_sha256: str,
    now: datetime | None,
) -> list[Error]:
    takeoff = case.get("takeoff")
    if not isinstance(takeoff, Mapping):
        return [_error("TAKEOFF_EVIDENCE_MISSING", "takeoff", case_id)]
    errors: list[Error] = []
    calculated_by = takeoff.get("calculated_by")
    reviewer = takeoff.get("reviewer_identity")
    if not _non_empty(calculated_by) or not _non_empty(reviewer):
        errors.append(_error("TAKEOFF_EVIDENCE_MISSING", "takeoff.identity", case_id))
    elif _identity(calculated_by) == _identity(reviewer):
        errors.append(_error("TAKEOFF_NOT_INDEPENDENT", "takeoff.reviewer_identity", case_id))

    calculation = takeoff.get("calculation_source")
    if not isinstance(calculation, Mapping) or not _non_empty(calculation.get("evidence_ref")):
        errors.append(_error("TAKEOFF_EVIDENCE_MISSING", "takeoff.calculation_source", case_id))
        calculation_artifact = None
    else:
        calculation_artifact, calculation_errors = _locked_artifact(
            locker,
            calculation,
            field="takeoff.calculation_source",
            kind="json",
            case_id=case_id,
        )
        errors.extend(calculation_errors)
    calculation_payload = _artifact_mapping(calculation_artifact)
    expected_calculation = {
        "evidence_kind": "independent_takeoff_calculation",
        "case_id": case.get("case_id"),
        "evidence_ref": calculation.get("evidence_ref")
        if isinstance(calculation, Mapping)
        else None,
        "calculated_by": calculated_by,
        "source_sha256": source.sha256 if source else None,
        "expected_takeoff_sha256": artifact_hashes.get("expected_takeoff"),
    }
    if calculation_payload is None or any(
        calculation_payload.get(key) != value for key, value in expected_calculation.items()
    ):
        errors.append(_error("CALCULATION_EVIDENCE_INVALID", "takeoff.calculation_source", case_id))

    material = takeoff.get("material_table")
    table: MaterialTable | None = None
    table_artifact: _Artifact | None = None
    approval_artifact: _Artifact | None = None
    if not isinstance(material, Mapping):
        errors.append(_error("MATERIAL_TABLE_APPROVAL_MISSING", "takeoff.material_table", case_id))
    else:
        table_artifact, table_errors = _locked_artifact(
            locker,
            material.get("table"),
            field="takeoff.material_table.table",
            kind="yaml",
            case_id=case_id,
        )
        errors.extend(table_errors)
        try:
            table = MaterialTable.model_validate(_artifact_mapping(table_artifact))
        except (ValidationError, TypeError, ValueError):
            errors.append(_error("MATERIAL_TABLE_INVALID", "takeoff.material_table.table", case_id))
            table = None
        if table is not None:
            expected_ref = f"{table.profile_id}@{table.version}"
            if not table.entries:
                errors.append(
                    _error("MATERIAL_TABLE_EMPTY", "takeoff.material_table.table", case_id)
                )
            if not table.company_approved or material.get("company_approved") is not True:
                errors.append(
                    _error(
                        "MATERIAL_TABLE_UNAPPROVED",
                        "takeoff.material_table.company_approved",
                        case_id,
                    )
                )
            if material.get("ref") != expected_ref:
                errors.append(
                    _error("MATERIAL_TABLE_REF_MISMATCH", "takeoff.material_table.ref", case_id)
                )
        approval = material.get("approval")
        if not isinstance(approval, Mapping) or not _non_empty(approval.get("evidence_ref")):
            errors.append(
                _error(
                    "MATERIAL_TABLE_APPROVAL_MISSING", "takeoff.material_table.approval", case_id
                )
            )
            approval_artifact = None
        else:
            approval_artifact, approval_errors = _locked_artifact(
                locker,
                approval,
                field="takeoff.material_table.approval",
                kind="json",
                case_id=case_id,
            )
            errors.extend(approval_errors)
        approval_payload = _artifact_mapping(approval_artifact)
        expected_approval = {
            "evidence_kind": "company_material_table_approval",
            "evidence_ref": approval.get("evidence_ref") if isinstance(approval, Mapping) else None,
            "material_profile_ref": material.get("ref"),
            "material_table_sha256": table_artifact.sha256 if table_artifact else None,
            "approved": True,
        }
        if approval_payload is None or any(
            approval_payload.get(key) != value for key, value in expected_approval.items()
        ):
            errors.append(
                _error("MATERIAL_APPROVAL_INVALID", "takeoff.material_table.approval", case_id)
            )

    input_pointer = artifacts.get("input_drawing")
    source_pointer = case.get("source_drawing")
    if (
        not isinstance(input_pointer, Mapping)
        or not isinstance(source_pointer, Mapping)
        or any(
            input_pointer.get(key) != source_pointer.get(key) for key in ("artifact_ref", "sha256")
        )
    ):
        errors.append(_error("SOURCE_REFERENCE_MISMATCH", "artifacts.input_drawing", case_id))
    recomputed_hash, recompute_errors = _recompute_takeoff(
        source=source,
        request_artifact=locked_artifacts.get("takeoff_request"),
        expected_artifact=locked_artifacts.get("expected_takeoff"),
        table=table,
        profile=profile,
        case_id=case_id,
    )
    errors.extend(recompute_errors)

    calculation_claims: JsonValue = {
        "evidence_kind": "takeoff_calculation_attestation",
        "case_id": cast(str | None, case.get("case_id")),
        "source_sha256": source.sha256 if source else None,
        "takeoff_request_sha256": artifact_hashes.get("takeoff_request"),
        "expected_takeoff_sha256": artifact_hashes.get("expected_takeoff"),
        "recomputed_takeoff_sha256": recomputed_hash,
        "calculation_evidence_sha256": (
            calculation_artifact.sha256 if calculation_artifact else None
        ),
    }
    calculator_identity, attestation_errors = _trusted_attestation(
        raw=takeoff.get("calculator_attestation"),
        expected_role=EvidenceRole.TAKEOFF_CALCULATOR,
        exact_claims=calculation_claims,
        policy=policy,
        expected_policy_sha256=expected_policy_sha256,
        field="takeoff.calculator_attestation",
        case_id=case_id,
        claimed_identity=calculated_by,
        now=now,
    )
    errors.extend(attestation_errors)
    review_claims: JsonValue = {
        "evidence_kind": "takeoff_review_attestation",
        "case_id": cast(str | None, case.get("case_id")),
        "source_sha256": source.sha256 if source else None,
        "takeoff_request_sha256": artifact_hashes.get("takeoff_request"),
        "expected_takeoff_sha256": artifact_hashes.get("expected_takeoff"),
        "recomputed_takeoff_sha256": recomputed_hash,
        "calculation_evidence_sha256": (
            calculation_artifact.sha256 if calculation_artifact else None
        ),
        "calculator_identity": calculator_identity,
        "accepted": True,
    }
    reviewer_identity, attestation_errors = _trusted_attestation(
        raw=takeoff.get("reviewer_attestation"),
        expected_role=EvidenceRole.TAKEOFF_REVIEWER,
        exact_claims=review_claims,
        policy=policy,
        expected_policy_sha256=expected_policy_sha256,
        field="takeoff.reviewer_attestation",
        case_id=case_id,
        claimed_identity=reviewer,
        now=now,
    )
    errors.extend(attestation_errors)
    if (
        calculator_identity is not None
        and reviewer_identity is not None
        and _identity(calculator_identity) == _identity(reviewer_identity)
    ):
        errors.append(_error("TAKEOFF_NOT_INDEPENDENT", "takeoff.reviewer_attestation", case_id))

    material_claims: JsonValue = {
        "evidence_kind": "material_table_approval_attestation",
        "case_id": cast(str | None, case.get("case_id")),
        "material_profile_ref": material.get("ref") if isinstance(material, Mapping) else None,
        "material_table_sha256": table_artifact.sha256 if table_artifact else None,
        "approval_evidence_sha256": approval_artifact.sha256 if approval_artifact else None,
        "approved": True,
    }
    material_attestation = material.get("attestation") if isinstance(material, Mapping) else None
    _, attestation_errors = _trusted_attestation(
        raw=material_attestation,
        expected_role=EvidenceRole.MATERIAL_TABLE_APPROVER,
        exact_claims=material_claims,
        policy=policy,
        expected_policy_sha256=expected_policy_sha256,
        field="takeoff.material_table.attestation",
        case_id=case_id,
        now=now,
    )
    errors.extend(attestation_errors)
    return errors


def verify_production_golden_acceptance(
    manifest_path: Path,
    *,
    trust_policy_path: Path | None = None,
    trust_policy_sha256: str | None = None,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a deterministic summary containing no paths, names, or identities."""
    manifest = _read_manifest(manifest_path)
    if manifest is None:
        return {
            "passed": False,
            "case_count": 0,
            "takeoff_case_count": 0,
            "errors": [_error("MANIFEST_UNREADABLE", "manifest")],
        }

    raw_cases = manifest.get("cases")
    cases = raw_cases if isinstance(raw_cases, list) else []
    errors: list[Error] = []
    if manifest.get("schema_version") != "1.0":
        errors.append(_error("MANIFEST_SCHEMA_UNSUPPORTED", "schema_version"))
    if manifest.get("manifest_kind") != MANIFEST_KIND:
        errors.append(_error("MANIFEST_KIND_INVALID", "manifest_kind"))
    if manifest.get("production_evidence") is not True:
        errors.append(_error("NOT_PRODUCTION_EVIDENCE", "production_evidence"))
    if manifest.get("review_status") != APPROVED_STATUS:
        errors.append(_error("REVIEW_STATUS_INVALID", "review_status"))
    if not isinstance(raw_cases, list):
        errors.append(_error("CASES_MISSING", "cases"))
    if not MIN_CASES <= len(cases) <= MAX_CASES:
        errors.append(_error("CASE_COUNT_OUT_OF_RANGE", "cases"))

    environment = os.environ if env is None else env
    if trust_policy_path is None:
        try:
            configured_policy = environment.get(TRUST_POLICY_ENV)
        except Exception:
            configured_policy = None
        trust_policy_path = (
            Path(cast(str, configured_policy)) if _non_empty(configured_policy) else None
        )
    policy = _load_trust_policy(trust_policy_path) if trust_policy_path is not None else None
    if trust_policy_path is None:
        errors.append(_error("TRUST_POLICY_MISSING", "trust_policy"))
    elif policy is None:
        errors.append(_error("TRUST_POLICY_INVALID", "trust_policy"))
    if trust_policy_sha256 is None:
        try:
            configured_digest = environment.get(TRUST_POLICY_SHA256_ENV)
        except Exception:
            configured_digest = None
        trust_policy_sha256 = (
            cast(str, configured_digest) if _non_empty(configured_digest) else None
        )
    if policy is not None:
        try:
            verify_trust_policy_digest(policy, trust_policy_sha256)
        except EvidenceAttestationError as exc:
            errors.append(_error(exc.code.value, "trust_policy_sha256"))
            policy = None
    if policy is None:
        ordered_errors = sorted(
            errors,
            key=lambda item: (item.get("case_id", ""), item["code"], item["field"]),
        )
        return {
            "passed": False,
            "case_count": len(cases),
            "takeoff_case_count": sum(
                isinstance(case, Mapping) and case.get("case_type") == "takeoff" for case in cases
            ),
            "errors": ordered_errors,
        }

    locker = _ArtifactLocker(manifest_path.parent)
    takeoff_count = 0
    seen_ids: set[str] = set()
    seen_sources: set[str] = set()
    seen_review_refs: set[str] = set()
    seen_review_paths: set[str] = set()
    seen_review_hashes: set[str] = set()
    for index, raw_case in enumerate(cases):
        case_id = _public_case_id(index)
        if not isinstance(raw_case, Mapping):
            errors.append(_error("CASE_INVALID", "case", case_id))
            continue
        case: JsonMapping = raw_case
        raw_case_id = case.get("case_id")
        if not isinstance(raw_case_id, str) or _SAFE_CASE_ID.fullmatch(raw_case_id) is None:
            errors.append(_error("CASE_ID_INVALID", "case_id", case_id))
        elif raw_case_id in seen_ids:
            errors.append(_error("CASE_ID_DUPLICATE", "case_id", case_id))
        else:
            seen_ids.add(raw_case_id)
        if case.get("production_evidence") is not True:
            errors.append(_error("NOT_PRODUCTION_EVIDENCE", "production_evidence", case_id))
        if case.get("review_status") != APPROVED_STATUS:
            errors.append(_error("REVIEW_STATUS_INVALID", "review_status", case_id))
        if case.get("engineer_selected") is not True:
            errors.append(_error("ENGINEER_SELECTION_MISSING", "engineer_selected", case_id))

        artifacts = case.get("artifacts")
        if not isinstance(artifacts, Mapping):
            errors.append(_error("ARTIFACTS_MISSING", "artifacts", case_id))
            artifacts = {}
        is_takeoff = case.get("case_type") == "takeoff"
        if is_takeoff:
            takeoff_count += 1
            required_artifacts = _BASE_ARTIFACTS | _TAKEOFF_ARTIFACTS
        else:
            required_artifacts = _BASE_ARTIFACTS
            if case.get("case_type") != "design":
                errors.append(_error("CASE_TYPE_INVALID", "case_type", case_id))

        locked_artifacts: dict[str, _Artifact] = {}
        artifact_hashes: dict[str, str] = {}
        for artifact_field, kind in required_artifacts.items():
            artifact, artifact_errors = _locked_artifact(
                locker,
                artifacts.get(artifact_field),
                field=f"artifacts.{artifact_field}",
                kind=kind,
                case_id=case_id,
            )
            errors.extend(artifact_errors)
            if artifact is not None:
                locked_artifacts[artifact_field] = artifact
                artifact_hashes[artifact_field] = artifact.sha256

        profile, profile_errors = _validate_profile(
            locked_artifacts.get("company_profile"), case_id
        )
        errors.extend(profile_errors)
        profile_claims: JsonValue = {
            "evidence_kind": "company_profile_approval_attestation",
            "company_profile_ref": profile.as_ref() if profile else None,
            "company_profile_sha256": artifact_hashes.get("company_profile"),
            "approved": True,
        }
        _, attestation_errors = _trusted_attestation(
            raw=case.get("company_profile_attestation"),
            expected_role=EvidenceRole.COMPANY_PROFILE_APPROVER,
            exact_claims=profile_claims,
            policy=policy,
            expected_policy_sha256=cast(str, trust_policy_sha256),
            field="company_profile_attestation",
            case_id=case_id,
            now=now,
        )
        errors.extend(attestation_errors)
        errors.extend(_validate_engineering_outputs(locked_artifacts, profile, case_id))
        source, source_errors = _validate_source(
            case,
            locker,
            case_id,
            profile=profile,
            expected_semantic_artifact=locked_artifacts.get("expected_semantic_entities"),
        )
        errors.extend(source_errors)
        if source is not None:
            if source.artifact.sha256 in seen_sources:
                errors.append(_error("SOURCE_HASH_DUPLICATE", "source_drawing.sha256", case_id))
            else:
                seen_sources.add(source.artifact.sha256)

        if is_takeoff:
            errors.extend(
                _validate_takeoff(
                    case,
                    locker,
                    case_id,
                    artifacts=artifacts,
                    locked_artifacts=locked_artifacts,
                    artifact_hashes=artifact_hashes,
                    profile=profile,
                    source=source.artifact if source else None,
                    policy=policy,
                    expected_policy_sha256=cast(str, trust_policy_sha256),
                    now=now,
                )
            )

        review_key, review_artifact, review_errors = _validate_review(
            case,
            locker,
            case_id,
            artifact_hashes=artifact_hashes,
            source_hash=source.artifact.sha256 if source else None,
        )
        errors.extend(review_errors)
        selector_claims: JsonValue = {
            "evidence_kind": "production_golden_selection_attestation",
            "manifest_kind": MANIFEST_KIND,
            "case_id": cast(str | None, case.get("case_id")),
            "source_sha256": source.artifact.sha256 if source else None,
            "provenance_sha256": source.provenance_sha256 if source else None,
            "source_class": source.source_class if source else None,
            "synthetic": source.synthetic if source else None,
            "development_fixture": source.development_fixture if source else None,
            "source_semantic_evidence_sha256": (
                source.semantic_evidence_sha256 if source else None
            ),
            "source_drawing_model_sha256": source.drawing_model_sha256 if source else None,
            "artifact_sha256": dict(artifact_hashes),
            "selected": True,
        }
        selector_identity, attestation_errors = _trusted_attestation(
            raw=case.get("selector_attestation"),
            expected_role=EvidenceRole.ENGINEER_SELECTOR,
            exact_claims=selector_claims,
            policy=policy,
            expected_policy_sha256=cast(str, trust_policy_sha256),
            field="selector_attestation",
            case_id=case_id,
            claimed_identity=case.get("selector_identity"),
            now=now,
        )
        errors.extend(attestation_errors)
        review = case.get("review")
        review_claims: JsonValue = {
            "evidence_kind": "production_golden_review_attestation",
            "manifest_kind": MANIFEST_KIND,
            "case_id": cast(str | None, case.get("case_id")),
            "source_sha256": source.artifact.sha256 if source else None,
            "provenance_sha256": source.provenance_sha256 if source else None,
            "source_class": source.source_class if source else None,
            "synthetic": source.synthetic if source else None,
            "development_fixture": source.development_fixture if source else None,
            "source_semantic_evidence_sha256": (
                source.semantic_evidence_sha256 if source else None
            ),
            "source_drawing_model_sha256": source.drawing_model_sha256 if source else None,
            "artifact_sha256": dict(artifact_hashes),
            "review_evidence_sha256": review_artifact.sha256 if review_artifact else None,
            "accepted": True,
        }
        reviewer_identity, attestation_errors = _trusted_attestation(
            raw=review.get("attestation") if isinstance(review, Mapping) else None,
            expected_role=EvidenceRole.GOLDEN_REVIEWER,
            exact_claims=review_claims,
            policy=policy,
            expected_policy_sha256=cast(str, trust_policy_sha256),
            field="review.attestation",
            case_id=case_id,
            claimed_identity=(
                review.get("reviewer_identity") if isinstance(review, Mapping) else None
            ),
            now=now,
        )
        errors.extend(attestation_errors)
        if (
            selector_identity is not None
            and reviewer_identity is not None
            and _identity(selector_identity) == _identity(reviewer_identity)
        ):
            errors.append(_error("SELECTION_NOT_INDEPENDENT", "review.attestation", case_id))
        if review_key is not None:
            review_ref, review_path, review_hash = review_key
            if (
                review_ref in seen_review_refs
                or review_path in seen_review_paths
                or review_hash in seen_review_hashes
            ):
                errors.append(_error("REVIEW_EVIDENCE_REUSED", "review", case_id))
            seen_review_refs.add(review_ref)
            seen_review_paths.add(review_path)
            seen_review_hashes.add(review_hash)

    if takeoff_count < MIN_TAKEOFF_CASES:
        errors.append(_error("TAKEOFF_CASE_COUNT_TOO_LOW", "cases"))
    unique_errors = {(item["code"], item["field"], item.get("case_id")): item for item in errors}
    ordered_errors = sorted(
        unique_errors.values(),
        key=lambda item: (item.get("case_id", ""), item["code"], item["field"]),
    )
    return {
        "passed": not ordered_errors,
        "case_count": len(cases),
        "takeoff_case_count": takeoff_count,
        "errors": ordered_errors,
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
    parser.add_argument(
        "--trust-policy",
        type=Path,
        help=(
            "separate production evidence trust policy; defaults to "
            f"{TRUST_POLICY_ENV} (path is never emitted)"
        ),
    )
    parser.add_argument(
        "--trust-policy-sha256",
        help=(f"pinned canonical trust-policy digest; defaults to {TRUST_POLICY_SHA256_ENV}"),
    )
    args = parser.parse_args(argv)
    summary = verify_production_golden_acceptance(
        args.manifest,
        trust_policy_path=args.trust_policy,
        trust_policy_sha256=args.trust_policy_sha256,
    )
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
