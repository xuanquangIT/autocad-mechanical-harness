"""Evaluate hash-locked public raster fixtures without making production claims.

The evaluator is deliberately separate from engineer acceptance.  It verifies the
download lock and every referenced byte, requires explicit pixel-to-millimetre
calibration, runs the local tracer twice, and emits only aggregate/redacted metrics.
It never accepts candidates, creates operations, touches AutoCAD, or treats public
examples as company or production evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import os
import re
import stat
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, NoReturn, cast

import cv2
import ezdxf
import ezdxf.units
import numpy as np
from ezdxf.entities import LWPolyline
from numpy.typing import NDArray
from pydantic import ValidationError

from cad_harness.comprehension.raster_trace import LocalRasterTracer, RasterTraceLimits
from cad_harness.domain.canonical import canonical_json, sha256_of
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    CircleGeometry,
    LineGeometry,
    PolylineGeometry,
)
from cad_harness.domain.models.raster import (
    PixelPoint,
    RasterCalibration,
    RasterTraceReport,
    RasterVectorCandidate,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_ROOT: Final = REPOSITORY_ROOT / "data" / "development-corpus" / "public"
DEFAULT_DATA_ROOT: Final = REPOSITORY_ROOT / "data"
LOCK_FILENAME: Final = "development-corpus.lock.json"
DERIVATION_MANIFEST_FILENAME: Final = "derivation-manifest.json"
MANIFEST_SCHEMA_VERSION: Final = "1.0"
OUTPUT_SCHEMA_VERSION: Final = "1.0"
EVALUATOR_VERSION: Final = "development-raster-evaluator-v1"
MAX_MANIFEST_BYTES: Final = 1024 * 1024
MAX_LOCK_BYTES: Final = 4 * 1024 * 1024
MAX_CASES: Final = 50
MAX_VARIANTS_PER_CASE: Final = 8
MAX_SAFE_IMAGE_BYTES: Final = 16 * 1024 * 1024
MAX_SAFE_PIXELS: Final = 20_000_000
MAX_SAFE_DIMENSION_PX: Final = 20_000
MAX_SAFE_DXF_BYTES: Final = 16 * 1024 * 1024
MAX_SAFE_REFERENCE_ENTITIES: Final = 20_000
MAX_MATERIALIZED_VARIANTS: Final = 100
MAX_MATERIALIZED_BYTES: Final = 256 * 1024 * 1024
NOISE_CHUNK_VALUES: Final = 1_000_000
READ_CHUNK_BYTES: Final = 1024 * 1024

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOCK_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_CASE_ID = re.compile(r"^case-[0-9]{4}$")
_SAFE_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"})
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_kind",
        "production_evidence",
        "customer_inputs_allowed",
        "corpus_lock_sha256",
        "tracer",
        "cases",
    }
)
_CASE_FIELDS = frozenset(
    {"case_id", "image", "calibration", "calibration_evidence", "reference", "variants"}
)


class DevelopmentRasterEvaluationError(ValueError):
    """Fail-closed error carrying a stable, path-free code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise DevelopmentRasterEvaluationError(code)


@dataclass(frozen=True, slots=True)
class _LockedSource:
    source_id: str
    relative_path: PurePosixPath
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _StableBytes:
    payload: bytes
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _TracerConfiguration:
    confidence_threshold: float
    limits: RasterTraceLimits
    max_reference_dxf_bytes: int
    max_reference_entities: int


@dataclass(frozen=True, slots=True)
class _ImageInput:
    payload: bytes
    source_sha256: str
    derivation_status: str


@dataclass(frozen=True, slots=True)
class _ReferenceInput:
    payload: bytes
    source_sha256: str
    millimetres_per_unit: float
    maximum_size_error_mm: float


@dataclass(frozen=True, slots=True)
class _Variant:
    variant_id: str
    kind: str
    seed: int | None = None
    sigma: float | None = None
    kernel_size: int | None = None


@dataclass(frozen=True, slots=True)
class _ReferenceDescriptor:
    kind: str
    size_mm: float


@dataclass(frozen=True, slots=True)
class _MaterializationCase:
    case_id: str
    image: object
    variants: tuple[_Variant, ...]


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & flag)


def _safe_lstat(path: Path, error_code: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        _fail(error_code)
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        _fail("REPARSE_POINT_NOT_ALLOWED")
    return metadata


def _validated_root(root: Path) -> Path:
    metadata = _safe_lstat(root, "CORPUS_ROOT_UNREADABLE")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("CORPUS_ROOT_NOT_DIRECTORY")
    try:
        return root.resolve(strict=True)
    except OSError:
        _fail("CORPUS_ROOT_UNREADABLE")


def _state(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _read_stable_file(path: Path, *, max_bytes: int, error_code: str) -> _StableBytes:
    before = _safe_lstat(path, error_code)
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > max_bytes:
        _fail(error_code)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(READ_CHUNK_BYTES), b""):
                chunks.append(chunk)
                digest.update(chunk)
    except OSError:
        _fail(error_code)
    after = _safe_lstat(path, error_code)
    if _state(before) != _state(after):
        _fail("SOURCE_CHANGED_DURING_READ")
    return _StableBytes(b"".join(chunks), digest.hexdigest(), after.st_size)


def _read_json(path: Path, *, max_bytes: int, error_code: str) -> tuple[Mapping[str, Any], str]:
    stable = _read_stable_file(path, max_bytes=max_bytes, error_code=error_code)
    try:
        decoded = json.loads(stable.payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _fail(error_code)
    if not isinstance(decoded, Mapping) or not all(isinstance(key, str) for key in decoded):
        _fail(error_code)
    return cast(Mapping[str, Any], decoded), stable.sha256


def _safe_relative_path(value: object, *, required_parent: str | None = None) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        _fail("SOURCE_REFERENCE_INVALID")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or value != relative.as_posix()
        or len(relative.parts) < 2
        or any(
            part in {"", ".", ".."} or _SAFE_PATH_PART.fullmatch(part) is None
            for part in relative.parts
        )
        or (required_parent is not None and relative.parts[0] != required_parent)
    ):
        _fail("SOURCE_REFERENCE_INVALID")
    return relative


def _resolve_corpus_file(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current /= part
        metadata = _safe_lstat(current, "LOCKED_SOURCE_UNREADABLE")
        if part != relative.parts[-1] and not stat.S_ISDIR(metadata.st_mode):
            _fail("SOURCE_REFERENCE_INVALID")
    if not stat.S_ISREG(_safe_lstat(current, "LOCKED_SOURCE_UNREADABLE").st_mode):
        _fail("LOCKED_SOURCE_UNREADABLE")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        _fail("SOURCE_PATH_ESCAPE")
    return resolved


def _safe_id(value: object, code: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        _fail(code)
    return value


def _exact_fields(value: object, expected: frozenset[str], code: str) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or not all(isinstance(key, str) for key in value)
        or frozenset(value) != expected
    ):
        _fail(code)
    return cast(Mapping[str, Any], value)


def _finite_number(value: object, code: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _fail(code)
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        _fail(code)
    return result


def _bounded_integer(value: object, code: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail(code)
    return value


def _load_lock(root: Path, expected_digest: object) -> tuple[dict[str, _LockedSource], str]:
    if not isinstance(expected_digest, str) or _SHA256.fullmatch(expected_digest) is None:
        _fail("CORPUS_LOCK_HASH_REQUIRED")
    lock, actual_digest = _read_json(
        root / LOCK_FILENAME,
        max_bytes=MAX_LOCK_BYTES,
        error_code="CORPUS_LOCK_UNREADABLE",
    )
    if expected_digest != f"sha256:{actual_digest}":
        _fail("CORPUS_LOCK_HASH_MISMATCH")
    if (
        lock.get("schema_version") != "1.0"
        or not isinstance(lock.get("manifest_sha256"), str)
        or _LOCK_SHA256.fullmatch(str(lock["manifest_sha256"])) is None
    ):
        _fail("CORPUS_LOCK_INVALID")
    metadata = lock.get("manifest")
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("production_evidence") is not False
        or metadata.get("customer_inputs_allowed") is not False
    ):
        _fail("CORPUS_LOCK_NOT_DEVELOPMENT_ONLY")
    raw_sources = lock.get("sources")
    source_count = lock.get("source_count")
    if (
        not isinstance(raw_sources, list)
        or isinstance(source_count, bool)
        or not isinstance(source_count, int)
        or source_count != len(raw_sources)
        or not raw_sources
    ):
        _fail("CORPUS_LOCK_INVALID")
    sources: dict[str, _LockedSource] = {}
    folded_paths: set[str] = set()
    for raw_entry in raw_sources:
        if not isinstance(raw_entry, Mapping):
            _fail("CORPUS_LOCK_INVALID")
        raw_source = raw_entry.get("source")
        if not isinstance(raw_source, Mapping):
            _fail("CORPUS_LOCK_INVALID")
        source_id = _safe_id(raw_source.get("source_id"), "CORPUS_LOCK_INVALID")
        relative = _safe_relative_path(raw_source.get("output"))
        digest = raw_entry.get("sha256")
        size_bytes = raw_entry.get("size_bytes")
        max_bytes = raw_source.get("max_bytes")
        if (
            not isinstance(digest, str)
            or _LOCK_SHA256.fullmatch(digest) is None
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes <= 0
            or isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not size_bytes <= max_bytes
        ):
            _fail("CORPUS_LOCK_INVALID")
        if source_id in sources or relative.as_posix().casefold() in folded_paths:
            _fail("CORPUS_LOCK_INVALID")
        sources[source_id] = _LockedSource(source_id, relative, digest, size_bytes)
        folded_paths.add(relative.as_posix().casefold())
    return sources, actual_digest


def _verify_locked_source(
    root: Path,
    sources: Mapping[str, _LockedSource],
    source_id_value: object,
    *,
    max_bytes: int,
    expected_suffixes: frozenset[str],
) -> tuple[_LockedSource, _StableBytes]:
    source_id = _safe_id(source_id_value, "SOURCE_REFERENCE_INVALID")
    source = sources.get(source_id)
    if source is None or source.relative_path.suffix.casefold() not in expected_suffixes:
        _fail("SOURCE_REFERENCE_INVALID")
    if source.size_bytes > max_bytes:
        _fail("SOURCE_LIMIT_EXCEEDED")
    path = _resolve_corpus_file(root, source.relative_path)
    stable = _read_stable_file(path, max_bytes=max_bytes, error_code="LOCKED_SOURCE_UNREADABLE")
    if stable.sha256 != source.sha256 or stable.size_bytes != source.size_bytes:
        _fail("LOCKED_SOURCE_HASH_MISMATCH")
    return source, stable


def _load_tracer_configuration(value: object) -> _TracerConfiguration:
    tracer = _exact_fields(
        value,
        frozenset({"confidence_threshold", "limits"}),
        "TRACER_CONFIGURATION_INVALID",
    )
    threshold = _finite_number(tracer["confidence_threshold"], "TRACER_CONFIGURATION_INVALID")
    if not 0.0 <= threshold <= 1.0:
        _fail("TRACER_CONFIGURATION_INVALID")
    limits = _exact_fields(
        tracer["limits"],
        frozenset(
            {
                "max_bytes",
                "max_pixels",
                "max_dimension_px",
                "max_reference_dxf_bytes",
                "max_reference_entities",
            }
        ),
        "TRACER_CONFIGURATION_INVALID",
    )
    max_bytes = _bounded_integer(
        limits["max_bytes"], "TRACER_CONFIGURATION_INVALID", minimum=1, maximum=MAX_SAFE_IMAGE_BYTES
    )
    max_pixels = _bounded_integer(
        limits["max_pixels"], "TRACER_CONFIGURATION_INVALID", minimum=1, maximum=MAX_SAFE_PIXELS
    )
    max_dimension = _bounded_integer(
        limits["max_dimension_px"],
        "TRACER_CONFIGURATION_INVALID",
        minimum=1,
        maximum=MAX_SAFE_DIMENSION_PX,
    )
    max_dxf_bytes = _bounded_integer(
        limits["max_reference_dxf_bytes"],
        "TRACER_CONFIGURATION_INVALID",
        minimum=1,
        maximum=MAX_SAFE_DXF_BYTES,
    )
    max_entities = _bounded_integer(
        limits["max_reference_entities"],
        "TRACER_CONFIGURATION_INVALID",
        minimum=1,
        maximum=MAX_SAFE_REFERENCE_ENTITIES,
    )
    return _TracerConfiguration(
        confidence_threshold=threshold,
        limits=RasterTraceLimits(
            max_bytes=max_bytes,
            max_pixels=max_pixels,
            max_dimension_px=max_dimension,
        ),
        max_reference_dxf_bytes=max_dxf_bytes,
        max_reference_entities=max_entities,
    )


def _load_calibration(value: object, evidence_value: object) -> tuple[RasterCalibration, str]:
    evidence = _exact_fields(
        evidence_value,
        frozenset({"kind", "evidence_id"}),
        "CALIBRATION_EVIDENCE_REQUIRED",
    )
    if evidence.get("kind") != "explicit_control_points":
        _fail("CALIBRATION_EVIDENCE_REQUIRED")
    evidence_id = _safe_id(evidence.get("evidence_id"), "CALIBRATION_EVIDENCE_REQUIRED")
    calibration_raw = _exact_fields(
        value,
        frozenset({"pixel_a", "pixel_b", "reference_distance_mm", "origin_mm"}),
        "CALIBRATION_REQUIRED",
    )
    try:
        calibration = RasterCalibration(
            pixel_a=PixelPoint.model_validate(calibration_raw["pixel_a"]),
            pixel_b=PixelPoint.model_validate(calibration_raw["pixel_b"]),
            reference_distance_mm=calibration_raw["reference_distance_mm"],
            origin_mm=calibration_raw["origin_mm"],
        )
    except (TypeError, ValueError, ValidationError):
        _fail("CALIBRATION_INVALID")
    calibration_digest = sha256_of(
        {
            "calibration": calibration.model_dump(mode="json"),
            "evidence_kind": evidence["kind"],
            "evidence_id": evidence_id,
        }
    )
    return calibration, calibration_digest


def _load_image(
    root: Path,
    sources: Mapping[str, _LockedSource],
    value: object,
    *,
    max_bytes: int,
) -> _ImageInput:
    if not isinstance(value, Mapping):
        _fail("IMAGE_INPUT_INVALID")
    kind = value.get("kind")
    if kind == "locked":
        image = _exact_fields(value, frozenset({"kind", "source_id"}), "IMAGE_INPUT_INVALID")
        _, stable = _verify_locked_source(
            root,
            sources,
            image["source_id"],
            max_bytes=max_bytes,
            expected_suffixes=_IMAGE_SUFFIXES,
        )
        return _ImageInput(stable.payload, f"sha256:{stable.sha256}", "locked_source")
    if kind != "derived":
        _fail("IMAGE_INPUT_INVALID")
    image = _exact_fields(
        value,
        frozenset({"kind", "relative_path", "sha256", "derived_from_source_id", "derivation"}),
        "IMAGE_INPUT_INVALID",
    )
    digest = image["sha256"]
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        _fail("DERIVED_IMAGE_HASH_REQUIRED")
    relative = _safe_relative_path(image["relative_path"], required_parent="scans")
    if relative.suffix.casefold() not in _IMAGE_SUFFIXES:
        _fail("IMAGE_INPUT_INVALID")
    _verify_locked_source(
        root,
        sources,
        image["derived_from_source_id"],
        max_bytes=MAX_SAFE_DXF_BYTES * 4,
        expected_suffixes=frozenset({".pdf"}),
    )
    derivation = _exact_fields(
        image["derivation"],
        frozenset({"kind", "page_number", "renderer_id"}),
        "DERIVATION_EVIDENCE_INVALID",
    )
    if derivation.get("kind") != "pdf_page_raster":
        _fail("DERIVATION_EVIDENCE_INVALID")
    _bounded_integer(
        derivation.get("page_number"),
        "DERIVATION_EVIDENCE_INVALID",
        minimum=1,
        maximum=10_000,
    )
    _safe_id(derivation.get("renderer_id"), "DERIVATION_EVIDENCE_INVALID")
    path = _resolve_corpus_file(root, relative)
    stable = _read_stable_file(path, max_bytes=max_bytes, error_code="DERIVED_IMAGE_UNREADABLE")
    if digest != f"sha256:{stable.sha256}":
        _fail("DERIVED_IMAGE_HASH_MISMATCH")
    return _ImageInput(stable.payload, digest, "declared_derivative_not_recomputed")


def _load_reference(
    root: Path,
    sources: Mapping[str, _LockedSource],
    value: object,
    *,
    configuration: _TracerConfiguration,
) -> _ReferenceInput | None:
    if value is None:
        return None
    reference = _exact_fields(
        value,
        frozenset({"kind", "source_id", "millimetres_per_unit", "maximum_size_error_mm"}),
        "REFERENCE_INPUT_INVALID",
    )
    if reference.get("kind") != "locked_dxf":
        _fail("REFERENCE_INPUT_INVALID")
    units = _finite_number(
        reference["millimetres_per_unit"], "REFERENCE_INPUT_INVALID", positive=True
    )
    tolerance = _finite_number(
        reference["maximum_size_error_mm"], "REFERENCE_INPUT_INVALID", positive=True
    )
    _, stable = _verify_locked_source(
        root,
        sources,
        reference["source_id"],
        max_bytes=configuration.max_reference_dxf_bytes,
        expected_suffixes=frozenset({".dxf"}),
    )
    return _ReferenceInput(stable.payload, f"sha256:{stable.sha256}", units, tolerance)


def _load_variants(value: object) -> tuple[_Variant, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_VARIANTS_PER_CASE:
        _fail("VARIANT_CONFIGURATION_INVALID")
    result: list[_Variant] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            _fail("VARIANT_CONFIGURATION_INVALID")
        kind = raw.get("kind")
        variant_id = _safe_id(raw.get("variant_id"), "VARIANT_CONFIGURATION_INVALID")
        if kind == "original":
            _exact_fields(raw, frozenset({"variant_id", "kind"}), "VARIANT_CONFIGURATION_INVALID")
            result.append(_Variant(variant_id, kind))
        elif kind == "gaussian_noise":
            variant = _exact_fields(
                raw,
                frozenset({"variant_id", "kind", "seed", "sigma"}),
                "VARIANT_CONFIGURATION_INVALID",
            )
            seed = _bounded_integer(
                variant["seed"],
                "VARIANT_CONFIGURATION_INVALID",
                minimum=0,
                maximum=2**32 - 1,
            )
            sigma = _finite_number(variant["sigma"], "VARIANT_CONFIGURATION_INVALID", positive=True)
            if sigma > 64.0:
                _fail("VARIANT_CONFIGURATION_INVALID")
            result.append(_Variant(variant_id, kind, seed=seed, sigma=sigma))
        elif kind == "gaussian_blur":
            variant = _exact_fields(
                raw,
                frozenset({"variant_id", "kind", "kernel_size"}),
                "VARIANT_CONFIGURATION_INVALID",
            )
            kernel = _bounded_integer(
                variant["kernel_size"],
                "VARIANT_CONFIGURATION_INVALID",
                minimum=3,
                maximum=31,
            )
            if kernel % 2 == 0:
                _fail("VARIANT_CONFIGURATION_INVALID")
            result.append(_Variant(variant_id, kind, kernel_size=kernel))
        else:
            _fail("VARIANT_CONFIGURATION_INVALID")
    ids = [variant.variant_id for variant in result]
    if len(ids) != len(set(ids)) or sum(item.kind == "original" for item in result) != 1:
        _fail("VARIANT_CONFIGURATION_INVALID")
    return tuple(sorted(result, key=lambda item: (item.kind != "original", item.variant_id)))


def _variant_transform(variant: _Variant) -> dict[str, Any]:
    if variant.kind == "gaussian_noise":
        assert variant.seed is not None and variant.sigma is not None
        return {
            "kind": variant.kind,
            "seed": variant.seed,
            "sigma": variant.sigma,
        }
    if variant.kind == "gaussian_blur":
        assert variant.kernel_size is not None
        return {
            "kind": variant.kind,
            "kernel_size": variant.kernel_size,
        }
    _fail("ORIGINAL_VARIANT_CANNOT_BE_MATERIALIZED")


def _encode_png(image: NDArray[np.uint8]) -> bytes:
    success, encoded = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    if not success:
        _fail("VARIANT_ENCODING_FAILED")
    return encoded.tobytes()


def _variant_payload(source: bytes, variant: _Variant) -> bytes:
    if variant.kind == "original":
        return source
    decoded = cv2.imdecode(np.frombuffer(source, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if decoded is None or decoded.ndim != 2:
        _fail("IMAGE_DECODE_FAILED")
    cv2.setNumThreads(1)
    cv2.setRNGSeed(0)
    cv2.ocl.setUseOpenCL(False)
    if variant.kind == "gaussian_noise":
        assert variant.seed is not None and variant.sigma is not None
        generator = np.random.Generator(np.random.PCG64(variant.seed))
        noisy = np.empty_like(decoded)
        rows_per_chunk = max(1, NOISE_CHUNK_VALUES // decoded.shape[1])
        for start in range(0, decoded.shape[0], rows_per_chunk):
            stop = min(decoded.shape[0], start + rows_per_chunk)
            source_chunk = decoded[start:stop].astype(np.float32)
            noise = generator.standard_normal(source_chunk.shape, dtype=np.float32)
            source_chunk += noise * np.float32(variant.sigma)
            noisy[start:stop] = np.clip(source_chunk, 0.0, 255.0).astype(np.uint8)
        return _encode_png(cast(NDArray[np.uint8], noisy))
    assert variant.kind == "gaussian_blur" and variant.kernel_size is not None
    blurred = cast(
        NDArray[np.uint8],
        cv2.GaussianBlur(decoded, (variant.kernel_size, variant.kernel_size), 0),
    )
    return _encode_png(blurred)


def _absolute_repository_path(path: Path) -> Path:
    return path.absolute() if path.is_absolute() else (REPOSITORY_ROOT / path).absolute()


def _validated_materialization_target(
    output_root: Path,
    allowed_root: Path,
    *,
    repository_data_root: Path,
) -> Path:
    data_candidate = _absolute_repository_path(repository_data_root)
    allowed_candidate = _absolute_repository_path(allowed_root)
    output_candidate = _absolute_repository_path(output_root)
    if any(part == ".." for part in (*data_candidate.parts, *allowed_candidate.parts)):
        _fail("MATERIALIZATION_PATH_NOT_ALLOWED")
    data = _validated_root(data_candidate)
    try:
        allowed_relative = allowed_candidate.relative_to(data_candidate)
    except ValueError:
        _fail("MATERIALIZATION_PATH_NOT_ALLOWED")
    current = data
    for part in allowed_relative.parts:
        current /= part
        metadata = _safe_lstat(current, "MATERIALIZATION_PATH_NOT_ALLOWED")
        if not stat.S_ISDIR(metadata.st_mode):
            _fail("MATERIALIZATION_PATH_NOT_ALLOWED")
    try:
        allowed = current.resolve(strict=True)
        requested_allowed = allowed_candidate.resolve(strict=True)
        requested_parent = output_candidate.parent.resolve(strict=True)
    except OSError:
        _fail("MATERIALIZATION_PATH_NOT_ALLOWED")
    if allowed != requested_allowed or not allowed.is_relative_to(data):
        _fail("MATERIALIZATION_PATH_NOT_ALLOWED")
    if requested_parent != allowed:
        _fail("MATERIALIZATION_PATH_NOT_ALLOWED")
    if _SAFE_ID.fullmatch(output_candidate.name) is None:
        _fail("MATERIALIZATION_PATH_NOT_ALLOWED")
    try:
        output_candidate.lstat()
    except FileNotFoundError:
        return output_candidate
    except OSError:
        _fail("MATERIALIZATION_PATH_NOT_ALLOWED")
    _fail("MATERIALIZATION_ALREADY_EXISTS")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_create_bytes(target: Path, payload: bytes) -> tuple[int, int, int, int]:
    temporary_name: str | None = None
    linked = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_name, target)
        linked = True
        _fsync_directory(target.parent)
        Path(temporary_name).unlink()
        temporary_name = None
        metadata = _safe_lstat(target, "MATERIALIZATION_WRITE_FAILED")
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(payload):
            _fail("MATERIALIZATION_WRITE_FAILED")
        return _state(metadata)
    except FileExistsError:
        if linked:
            with suppress(OSError):
                target.unlink()
        _fail("MATERIALIZATION_ALREADY_EXISTS")
    except DevelopmentRasterEvaluationError:
        if linked:
            with suppress(OSError):
                target.unlink()
        raise
    except OSError:
        if linked:
            with suppress(OSError):
                target.unlink()
        _fail("MATERIALIZATION_WRITE_FAILED")
    finally:
        if temporary_name is not None:
            with suppress(OSError):
                Path(temporary_name).unlink()


def _cleanup_materialization(
    root: Path,
    created_files: Sequence[tuple[Path, tuple[int, int, int, int]]],
    created_directories: Sequence[Path],
) -> None:
    for path, expected_state in reversed(created_files):
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISLNK(metadata.st_mode)
                and not _is_reparse(metadata)
                and stat.S_ISREG(metadata.st_mode)
                and _state(metadata) == expected_state
            ):
                path.unlink()
        except OSError:
            continue
    for directory in reversed(created_directories):
        with suppress(OSError):
            directory.rmdir()
    with suppress(OSError):
        root.rmdir()
    _fsync_directory(root.parent)


def _materialization_cases(raw_cases: Sequence[object]) -> tuple[_MaterializationCase, ...]:
    cases: list[_MaterializationCase] = []
    noise_variants: list[_Variant] = []
    blur_count = 0
    total = 0
    for raw_case in raw_cases:
        case = _exact_fields(raw_case, _CASE_FIELDS, "EVALUATION_CASE_INVALID")
        case_id = _safe_id(case.get("case_id"), "EVALUATION_CASE_INVALID")
        if _CASE_ID.fullmatch(case_id) is None:
            _fail("EVALUATION_CASE_INVALID")
        variants = tuple(
            variant
            for variant in _load_variants(case.get("variants"))
            if variant.kind != "original"
        )
        if not variants:
            continue
        total += len(variants)
        noise_variants.extend(variant for variant in variants if variant.kind == "gaussian_noise")
        blur_count += sum(variant.kind == "gaussian_blur" for variant in variants)
        cases.append(_MaterializationCase(case_id, case.get("image"), variants))
    seeds = {variant.seed for variant in noise_variants}
    levels = {variant.sigma for variant in noise_variants}
    if (
        not 3 <= total <= MAX_MATERIALIZED_VARIANTS
        or len(noise_variants) < 2
        or len(seeds) < 2
        or len(levels) < 2
        or blur_count < 1
    ):
        _fail("MATERIALIZATION_VARIANTS_INSUFFICIENT")
    return tuple(sorted(cases, key=lambda item: item.case_id))


def _materialize_development_variants(
    *,
    corpus_root: Path,
    sources: Mapping[str, _LockedSource],
    configuration: _TracerConfiguration,
    raw_cases: Sequence[object],
    case_results: Sequence[Mapping[str, Any]],
    output_root: Path,
    allowed_root: Path,
    repository_data_root: Path,
    input_manifest_sha256: str,
    corpus_lock_sha256: str,
    evaluation_id: str,
) -> dict[str, Any]:
    cases = _materialization_cases(raw_cases)
    target_root = _validated_materialization_target(
        output_root,
        allowed_root,
        repository_data_root=repository_data_root,
    )
    expected_hashes: dict[tuple[str, str], str] = {}
    for case_result in case_results:
        case_id = str(case_result["case_id"])
        result_variants = cast(Sequence[Mapping[str, Any]], case_result["variants"])
        for result_variant in result_variants:
            expected_hashes[(case_id, str(result_variant["variant_id"]))] = str(
                result_variant["derived_source_sha256"]
            )

    created_files: list[tuple[Path, tuple[int, int, int, int]]] = []
    created_directories: list[Path] = []
    derivations: list[dict[str, Any]] = []
    total_bytes = 0
    root_created = False
    try:
        target_root.mkdir()
        root_created = True
        _fsync_directory(target_root.parent)
        for case in cases:
            case_root = target_root / case.case_id
            case_root.mkdir()
            created_directories.append(case_root)
            _fsync_directory(target_root)
            source = _load_image(
                corpus_root,
                sources,
                case.image,
                max_bytes=configuration.limits.max_bytes,
            )
            source_before = hashlib.sha256(source.payload).hexdigest()
            for materialized_variant in case.variants:
                payload = _variant_payload(source.payload, materialized_variant)
                total_bytes += len(payload)
                if total_bytes > MAX_MATERIALIZED_BYTES:
                    _fail("MATERIALIZATION_BYTE_LIMIT_EXCEEDED")
                output_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
                if (
                    expected_hashes.get((case.case_id, materialized_variant.variant_id))
                    != output_digest
                ):
                    _fail("MATERIALIZATION_TRACE_HASH_MISMATCH")
                artifact_ref = f"{case.case_id}/{materialized_variant.variant_id}.png"
                target = case_root / f"{materialized_variant.variant_id}.png"
                state = _atomic_create_bytes(target, payload)
                created_files.append((target, state))
                transform = _variant_transform(materialized_variant)
                derivation_identity = {
                    "source_sha256": source.source_sha256,
                    "transform": transform,
                    "output_sha256": output_digest,
                }
                derivations.append(
                    {
                        "case_id": case.case_id,
                        "variant_id": materialized_variant.variant_id,
                        "derivation_id": sha256_of(derivation_identity),
                        "source_sha256": source.source_sha256,
                        "transform": transform,
                        "output_sha256": output_digest,
                        "size_bytes": len(payload),
                        "media_type": "image/png",
                        "artifact_ref": artifact_ref,
                    }
                )
            source_after = _load_image(
                corpus_root,
                sources,
                case.image,
                max_bytes=configuration.limits.max_bytes,
            )
            if (
                source_after.source_sha256 != source.source_sha256
                or hashlib.sha256(source_after.payload).hexdigest() != source_before
            ):
                _fail("SOURCE_CHANGED_DURING_MATERIALIZATION")
        derivations.sort(key=lambda item: (str(item["case_id"]), str(item["variant_id"])))
        manifest: dict[str, Any] = {
            "schema_version": "1.0",
            "manifest_kind": "development_raster_derivations",
            "production_evidence": False,
            "production_acceptance_eligible": False,
            "engineer_reviewed": False,
            "company_approved": False,
            "customer_data_used": False,
            "source_names_omitted": True,
            "source_paths_omitted": True,
            "input_manifest_sha256": input_manifest_sha256,
            "corpus_lock_sha256": corpus_lock_sha256,
            "evaluation_id": evaluation_id,
            "case_count": len(cases),
            "derivation_count": len(derivations),
            "total_size_bytes": total_bytes,
            "derivations": derivations,
        }
        manifest_payload = (canonical_json(manifest) + "\n").encode("utf-8")
        manifest_target = target_root / DERIVATION_MANIFEST_FILENAME
        state = _atomic_create_bytes(manifest_target, manifest_payload)
        created_files.append((manifest_target, state))
        _fsync_directory(target_root)
        _fsync_directory(target_root.parent)
        return manifest
    except DevelopmentRasterEvaluationError:
        if root_created:
            _cleanup_materialization(target_root, created_files, created_directories)
        raise
    except OSError:
        if root_created:
            _cleanup_materialization(target_root, created_files, created_directories)
        _fail("MATERIALIZATION_WRITE_FAILED")


def _reference_descriptors(
    reference: _ReferenceInput,
    *,
    max_entities: int,
) -> tuple[tuple[_ReferenceDescriptor, ...], dict[str, int]]:
    try:
        text = reference.payload.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        document = ezdxf.read(io.StringIO(text))
    except (UnicodeError, ezdxf.DXFError, ValueError, TypeError):
        _fail("REFERENCE_DXF_PARSE_FAILED")
    if document.units != 0:
        try:
            declared_scale = float(
                ezdxf.units.conversion_factor(
                    ezdxf.units.InsertUnits(document.units),
                    ezdxf.units.InsertUnits.Millimeters,
                )
            )
        except (TypeError, ValueError):
            _fail("REFERENCE_UNIT_INVALID")
        if not math.isclose(
            declared_scale,
            reference.millimetres_per_unit,
            rel_tol=0.0,
            abs_tol=1.0e-12,
        ):
            _fail("REFERENCE_UNIT_MISMATCH")
    descriptors: list[_ReferenceDescriptor] = []
    unsupported: Counter[str] = Counter()
    scanned = 0
    logger = logging.getLogger("ezdxf")
    previous_disabled = logger.disabled
    try:
        logger.disabled = True
        for entity in document.modelspace():
            scanned += 1
            if scanned > max_entities:
                _fail("REFERENCE_ENTITY_LIMIT_EXCEEDED")
            entity_type = entity.dxftype()
            scale = reference.millimetres_per_unit
            if entity_type == "LINE":
                length = math.dist(tuple(entity.dxf.start)[:2], tuple(entity.dxf.end)[:2]) * scale
                if length > 0.0:
                    descriptors.append(_ReferenceDescriptor("line", length))
            elif entity_type == "CIRCLE":
                radius = float(entity.dxf.radius) * scale
                if radius > 0.0:
                    descriptors.append(_ReferenceDescriptor("circle", radius))
            elif entity_type == "ARC":
                radius = float(entity.dxf.radius) * scale
                if radius > 0.0:
                    descriptors.append(_ReferenceDescriptor("arc", radius))
            elif entity_type == "LWPOLYLINE" and bool(cast(LWPolyline, entity).closed):
                points = tuple(cast(LWPolyline, entity).get_points("xyb"))
                if points and all(abs(float(point[2])) <= 1.0e-12 for point in points):
                    perimeter = (
                        sum(
                            math.dist(first[:2], second[:2])
                            for first, second in zip(points, points[1:] + points[:1], strict=True)
                        )
                        * scale
                    )
                    if perimeter > 0.0:
                        descriptors.append(_ReferenceDescriptor("polyline", perimeter))
                else:
                    unsupported["LWPOLYLINE_WITH_BULGE"] += 1
            else:
                unsupported[entity_type] += 1
    finally:
        logger.disabled = previous_disabled
    descriptors.sort(key=lambda item: (item.kind, item.size_mm))
    return tuple(descriptors), dict(sorted(unsupported.items()))


def _candidate_descriptor(candidate: RasterVectorCandidate) -> _ReferenceDescriptor | None:
    geometry = candidate.geometry
    if isinstance(geometry, LineGeometry):
        return _ReferenceDescriptor("line", math.dist(geometry.start_mm, geometry.end_mm))
    if isinstance(geometry, CircleGeometry):
        return _ReferenceDescriptor("circle", geometry.radius_mm)
    if isinstance(geometry, ArcGeometry):
        return _ReferenceDescriptor("arc", geometry.radius_mm)
    if isinstance(geometry, PolylineGeometry) and geometry.closed:
        points = tuple(vertex.point_mm for vertex in geometry.vertices)
        if len(points) >= 3 and all(abs(vertex.bulge) <= 1.0e-12 for vertex in geometry.vertices):
            perimeter = sum(
                math.dist(first, second)
                for first, second in zip(points, points[1:] + points[:1], strict=True)
            )
            return _ReferenceDescriptor("polyline", perimeter)
    return None


def _round_metric(value: float) -> float:
    rounded = round(float(value), 9)
    return 0.0 if rounded == 0.0 else rounded


def _stats(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "minimum": None, "mean": None, "maximum": None}
    return {
        "count": len(values),
        "minimum": _round_metric(min(values)),
        "mean": _round_metric(sum(values) / len(values)),
        "maximum": _round_metric(max(values)),
    }


def _reference_comparison(
    report: RasterTraceReport,
    reference: _ReferenceInput | None,
    reference_descriptors: tuple[_ReferenceDescriptor, ...],
    unsupported: Mapping[str, int],
) -> dict[str, Any]:
    if reference is None:
        return {
            "available": False,
            "geometric_accuracy_measured": False,
            "reason": "no_explicit_vector_reference",
        }
    by_kind: dict[str, list[float]] = {}
    for descriptor in reference_descriptors:
        by_kind.setdefault(descriptor.kind, []).append(descriptor.size_mm)
    errors: list[float] = []
    compared = 0
    for candidate in report.candidates:
        candidate_descriptor = _candidate_descriptor(candidate)
        if candidate_descriptor is None or candidate_descriptor.kind not in by_kind:
            continue
        compared += 1
        errors.append(
            min(
                abs(candidate_descriptor.size_mm - value)
                for value in by_kind[candidate_descriptor.kind]
            )
        )
    within_tolerance = sum(error <= reference.maximum_size_error_mm for error in errors)
    return {
        "available": True,
        "comparison_scope": "nearest_primitive_size_only",
        "diagnostic_only": True,
        "geometric_accuracy_measured": False,
        "reference_source_sha256": reference.source_sha256,
        "reference_primitive_count": len(reference_descriptors),
        "reference_primitive_counts": dict(
            sorted(Counter(item.kind for item in reference_descriptors).items())
        ),
        "unsupported_reference_entity_counts": dict(sorted(unsupported.items())),
        "candidate_size_comparison_count": compared,
        "nearest_size_error_mm": _stats(errors),
        "declared_maximum_size_error_mm": reference.maximum_size_error_mm,
        "within_declared_size_tolerance_count": within_tolerance,
    }


def _trace_once(
    payload: bytes,
    calibration: RasterCalibration,
    configuration: _TracerConfiguration,
    output_root: Path,
) -> RasterTraceReport:
    tracer = LocalRasterTracer(
        output_root,
        limits=configuration.limits,
        confidence_threshold=configuration.confidence_threshold,
    )
    try:
        return tracer.trace(
            payload, display_name="development-raster-input", calibration=calibration
        )
    except (TypeError, ValueError, cv2.error):
        _fail("RASTER_TRACE_FAILED")


def _variant_metrics(
    payload: bytes,
    variant: _Variant,
    calibration: RasterCalibration,
    configuration: _TracerConfiguration,
    reference: _ReferenceInput | None,
    reference_descriptors: tuple[_ReferenceDescriptor, ...],
    unsupported_reference: Mapping[str, int],
    work_root: Path,
) -> dict[str, Any]:
    variant_payload = _variant_payload(payload, variant)
    first_root = work_root / f"{variant.variant_id}-a"
    second_root = work_root / f"{variant.variant_id}-b"
    first = _trace_once(variant_payload, calibration, configuration, first_root)
    second = _trace_once(variant_payload, calibration, configuration, second_root)
    deterministic = first.model_dump(mode="json") == second.model_dump(mode="json")
    if not deterministic:
        _fail("TRACE_NONDETERMINISTIC")
    status_counts: Counter[str] = Counter(candidate.status.value for candidate in first.candidates)
    geometry_counts: Counter[str] = Counter(
        candidate.geometry.kind for candidate in first.candidates
    )
    millimetres_per_pixel = calibration.millimetres_per_pixel
    confidence_values = [candidate.confidence for candidate in first.candidates]
    fit_errors_px = [candidate.fit_error_px for candidate in first.candidates]
    fit_errors_mm = [value * millimetres_per_pixel for value in fit_errors_px]
    return {
        "variant_id": variant.variant_id,
        "variant_kind": variant.kind,
        "derived_source_sha256": first.source.source_sha256,
        "trace_digest": first.trace_digest,
        "deterministic_repeat": True,
        "candidate_count": len(first.candidates),
        "candidate_status_counts": {
            name: status_counts.get(name, 0) for name in ("proposed", "ambiguous", "rejected")
        },
        "candidate_geometry_counts": {
            name: geometry_counts.get(name, 0) for name in ("line", "circle", "arc", "polyline")
        },
        "confidence": _stats(confidence_values),
        "fit_error_px": _stats(fit_errors_px),
        "calibrated_fit_error_mm": _stats(fit_errors_mm),
        "requires_engineer_review": first.requires_engineer_review,
        "production_ready": first.production_ready,
        "reference_comparison": _reference_comparison(
            first, reference, reference_descriptors, unsupported_reference
        ),
    }


def _validate_manifest(payload: Mapping[str, Any]) -> None:
    if frozenset(payload) != _TOP_LEVEL_FIELDS:
        _fail("EVALUATION_MANIFEST_INVALID")
    if (
        payload.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or payload.get("manifest_kind") != "development_raster_evaluation"
        or payload.get("production_evidence") is not False
        or payload.get("customer_inputs_allowed") is not False
    ):
        _fail("EVALUATION_MANIFEST_INVALID")


def evaluate_development_raster_corpus(
    corpus_root: Path,
    input_manifest_path: Path,
    *,
    materialize_root: Path | None = None,
    materialize_allowed_root: Path | None = None,
    materialize_data_root: Path = DEFAULT_DATA_ROOT,
) -> dict[str, Any]:
    """Run a deterministic, redacted development evaluation over explicit cases."""
    if (materialize_root is None) != (materialize_allowed_root is None):
        _fail("MATERIALIZATION_ALLOWLIST_REQUIRED")
    root = _validated_root(corpus_root)
    manifest, manifest_digest = _read_json(
        input_manifest_path,
        max_bytes=MAX_MANIFEST_BYTES,
        error_code="EVALUATION_MANIFEST_UNREADABLE",
    )
    _validate_manifest(manifest)
    sources, lock_digest = _load_lock(root, manifest.get("corpus_lock_sha256"))
    configuration = _load_tracer_configuration(manifest.get("tracer"))
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= MAX_CASES:
        _fail("EVALUATION_MANIFEST_INVALID")
    case_ids: set[str] = set()
    case_results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="cad-harness-raster-evaluation-") as temporary:
        temporary_root = Path(temporary)
        for raw_case in raw_cases:
            case = _exact_fields(raw_case, _CASE_FIELDS, "EVALUATION_CASE_INVALID")
            case_id = _safe_id(case.get("case_id"), "EVALUATION_CASE_INVALID")
            if _CASE_ID.fullmatch(case_id) is None:
                _fail("EVALUATION_CASE_INVALID")
            if case_id in case_ids:
                _fail("EVALUATION_CASE_INVALID")
            case_ids.add(case_id)
            calibration, calibration_digest = _load_calibration(
                case.get("calibration"), case.get("calibration_evidence")
            )
            image = _load_image(
                root,
                sources,
                case.get("image"),
                max_bytes=configuration.limits.max_bytes,
            )
            reference = _load_reference(
                root,
                sources,
                case.get("reference"),
                configuration=configuration,
            )
            variants = _load_variants(case.get("variants"))
            reference_descriptors: tuple[_ReferenceDescriptor, ...] = ()
            unsupported_reference: dict[str, int] = {}
            if reference is not None:
                reference_descriptors, unsupported_reference = _reference_descriptors(
                    reference,
                    max_entities=configuration.max_reference_entities,
                )
            case_work = temporary_root / case_id
            variants_result = [
                _variant_metrics(
                    image.payload,
                    variant,
                    calibration,
                    configuration,
                    reference,
                    reference_descriptors,
                    unsupported_reference,
                    case_work,
                )
                for variant in variants
            ]
            case_results.append(
                {
                    "case_id": case_id,
                    "source_sha256": image.source_sha256,
                    "calibration_digest": calibration_digest,
                    "derivation_status": image.derivation_status,
                    "reference_available": reference is not None,
                    "variant_count": len(variants_result),
                    "variants": variants_result,
                }
            )
    case_results.sort(key=lambda item: str(item["case_id"]))
    all_variants = [variant for case in case_results for variant in case["variants"]]
    summary_status_counts: Counter[str] = Counter()
    for variant in all_variants:
        summary_status_counts.update(cast(Mapping[str, int], variant["candidate_status_counts"]))
    algorithm_id = (
        f"{EVALUATOR_VERSION}:opencv-{cv2.__version__}:"
        f"numpy-{np.__version__}:ezdxf-{ezdxf.__version__}"
    )
    evaluation_identity = {
        "algorithm": algorithm_id,
        "input_manifest_sha256": f"sha256:{manifest_digest}",
        "corpus_lock_sha256": f"sha256:{lock_digest}",
        "case_trace_digests": [
            [variant["trace_digest"] for variant in case["variants"]] for case in case_results
        ],
    }
    report: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "report_kind": "development_raster_corpus_evaluation",
        "evaluation_id": sha256_of(evaluation_identity),
        "algorithm_id": evaluation_identity["algorithm"],
        "input_manifest_sha256": evaluation_identity["input_manifest_sha256"],
        "corpus_lock_sha256": evaluation_identity["corpus_lock_sha256"],
        "production_evidence": False,
        "production_acceptance_eligible": False,
        "engineer_reviewed": False,
        "company_approved": False,
        "customer_data_used": False,
        "privacy": {
            "source_names_omitted": True,
            "source_paths_omitted": True,
            "raw_images_omitted": True,
            "candidate_geometry_omitted": True,
        },
        "summary": {
            "case_count": len(case_results),
            "variant_count": len(all_variants),
            "deterministic_repeat_count": sum(
                bool(variant["deterministic_repeat"]) for variant in all_variants
            ),
            "reference_case_count": sum(bool(case["reference_available"]) for case in case_results),
            "observation_only_case_count": sum(
                not bool(case["reference_available"]) for case in case_results
            ),
            "candidate_status_counts": {
                name: summary_status_counts.get(name, 0)
                for name in ("proposed", "ambiguous", "rejected")
            },
            "geometric_accuracy_measured": False,
        },
        "limitations": [
            "public_development_fixtures_are_not_engineer_selected_company_evidence",
            "candidate_acceptance_and_live_autocad_readback_were_not_performed",
            "nearest_primitive_size_comparison_is_diagnostic_not_geometric_accuracy",
            "derived_scan_provenance_is_hash_bound_but_not_recomputed_by_this_evaluator",
        ],
        "cases": case_results,
    }
    if materialize_root is not None and materialize_allowed_root is not None:
        _materialize_development_variants(
            corpus_root=root,
            sources=sources,
            configuration=configuration,
            raw_cases=cast(Sequence[object], raw_cases),
            case_results=cast(Sequence[Mapping[str, Any]], case_results),
            output_root=materialize_root,
            allowed_root=materialize_allowed_root,
            repository_data_root=materialize_data_root,
            input_manifest_sha256=str(report["input_manifest_sha256"]),
            corpus_lock_sha256=str(report["corpus_lock_sha256"]),
            evaluation_id=str(report["evaluation_id"]),
        )
    return report


def render_evaluation(report: Mapping[str, Any]) -> str:
    """Return one canonical UTF-8 JSON line."""
    return canonical_json(dict(report)) + "\n"


def _output_target(path: Path, output_root: Path) -> Path:
    root = _validated_root(output_root)
    if path.suffix.casefold() != ".json":
        _fail("OUTPUT_FORMAT_INVALID")
    try:
        candidate = path if path.is_absolute() else root / path
        parent = candidate.parent.resolve(strict=True)
        parent.relative_to(root)
    except (OSError, ValueError):
        _fail("OUTPUT_PATH_NOT_ALLOWED")
    relative_parent = parent.relative_to(root)
    current = root
    for part in relative_parent.parts:
        current /= part
        metadata = _safe_lstat(current, "OUTPUT_PATH_NOT_ALLOWED")
        if not stat.S_ISDIR(metadata.st_mode):
            _fail("OUTPUT_PATH_NOT_ALLOWED")
    target = parent / candidate.name
    try:
        target.lstat()
    except FileNotFoundError:
        return target
    except OSError:
        _fail("OUTPUT_PATH_NOT_ALLOWED")
    _fail("OUTPUT_ALREADY_EXISTS")


def _write_once(path: Path, payload: str, *, output_root: Path) -> None:
    target = _output_target(path, output_root)
    try:
        with target.open("x", encoding="utf-8", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        _fail("OUTPUT_ALREADY_EXISTS")
    except OSError:
        with suppress(OSError):
            if target.is_file() and target.stat().st_size == 0:
                target.unlink()
        _fail("OUTPUT_WRITE_FAILED")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-manifest", required=True, type=Path)
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--output", type=Path, help="optional no-overwrite canonical JSON target")
    parser.add_argument(
        "--output-root",
        type=Path,
        help="required allowlisted root whenever --output is used",
    )
    parser.add_argument(
        "--materialize-root",
        type=Path,
        help="optional new dedicated directory for deterministic derived PNG files",
    )
    parser.add_argument(
        "--materialize-allow-root",
        type=Path,
        help="required existing ignored data allowlist for --materialize-root",
    )
    args = parser.parse_args(argv)
    try:
        if (args.output is None) != (args.output_root is None):
            _fail("OUTPUT_ALLOWLIST_REQUIRED")
        report = evaluate_development_raster_corpus(
            args.corpus_root,
            args.input_manifest,
            materialize_root=args.materialize_root,
            materialize_allowed_root=args.materialize_allow_root,
        )
        rendered = render_evaluation(report)
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            _write_once(args.output, rendered, output_root=args.output_root)
    except DevelopmentRasterEvaluationError as error:
        sys.stderr.write(
            json.dumps(
                {
                    "error": {"code": error.code},
                    "production_evidence": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
