"""Build a private, fail-closed draft packet for engineer corpus review.

The packet is preparation material, never production evidence. It binds exact
local/public drawing bytes to opaque identifiers and creates blank review forms;
it deliberately does not calculate expectations or assert human approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, NoReturn, cast

from scripts.fetch_development_corpus import CONFIG_PATH as PUBLIC_CORPUS_CONFIG_PATH
from scripts.fetch_development_corpus import CorpusFetchError, load_manifest

MIN_CASES: Final = 30
MAX_CASES: Final = 50
MIN_TAKEOFF_CASES: Final = 5
REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWED_OUTPUT_PARENT: Final = REPOSITORY_ROOT / "data" / "engineer-review-packets"
_DRAWING_SUFFIXES: Final = frozenset({".dxf", ".dwg"})
_LOCAL_SOURCE_KINDS: Final = frozenset({"customer_local", "generated", "public_licensed"})
_SHA256: Final = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_MANIFEST_BYTES: Final = 16 * 1024 * 1024
_COPY_CHUNK_BYTES: Final = 1024 * 1024
_BASE_ARTIFACTS: Final = (
    "input_spec",
    "company_profile",
    "expected_plan",
    "expected_semantic_entities",
    "expected_validation",
    "preview_reference",
)
_TAKEOFF_ARTIFACTS: Final = (
    "input_drawing",
    "takeoff_request",
    "expected_takeoff",
)
_BASE_ATTESTATION_ROLES: Final = (
    "engineer_selector",
    "golden_reviewer",
    "company_profile_approver",
)
_TAKEOFF_ATTESTATION_ROLES: Final = (
    "takeoff_calculator",
    "takeoff_reviewer",
    "material_table_approver",
)


class EngineerReviewPacketError(ValueError):
    """Redacted intake failure suitable for a command-line boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise EngineerReviewPacketError(code)


@dataclass(frozen=True)
class _FileState:
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class _DrawingSource:
    path: Path
    sha256: str
    size_bytes: int
    drawing_format: str
    origin: str
    source_class: str
    synthetic: bool
    development_fixture: bool
    provenance_sha256: str


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & flag)


def _lstat(path: Path, code: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        _fail(code)
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        _fail("REPARSE_POINT_NOT_ALLOWED")
    return metadata


def _state(metadata: os.stat_result) -> _FileState:
    return _FileState(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
    )


def _regular_file(path: Path, code: str) -> os.stat_result:
    metadata = _lstat(path, code)
    if not stat.S_ISREG(metadata.st_mode):
        _fail(code)
    return metadata


def _directory(path: Path, code: str) -> Path:
    metadata = _lstat(path, code)
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(code)
    try:
        return path.resolve(strict=True)
    except OSError:
        _fail(code)


def _load_json(path: Path, code: str) -> Mapping[str, Any]:
    metadata = _regular_file(path, code)
    if metadata.st_size > _MAX_MANIFEST_BYTES:
        _fail(code)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail(code)
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _fail(code)
    return cast(Mapping[str, Any], value)


def _hash_regular_file(path: Path, code: str) -> tuple[str, int]:
    before = _regular_file(path, code)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _state(opened) != _state(before) or not stat.S_ISREG(opened.st_mode):
                _fail("SOURCE_CHANGED_DURING_READ")
            for chunk in iter(lambda: stream.read(_COPY_CHUNK_BYTES), b""):
                digest.update(chunk)
    except EngineerReviewPacketError:
        raise
    except OSError:
        _fail(code)
    after = _regular_file(path, code)
    if _state(before) != _state(after):
        _fail("SOURCE_CHANGED_DURING_READ")
    return digest.hexdigest(), after.st_size


def _safe_relative_path(value: object, code: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        _fail(code)
    relative = PurePosixPath(value)
    if relative.is_absolute() or relative.as_posix() in {"", "."} or ".." in relative.parts:
        _fail(code)
    if any(part in {"", "."} for part in relative.parts):
        _fail(code)
    return relative


def _walk_drawings(root: Path) -> list[Path]:
    resolved_root = _directory(root, "SOURCE_ROOT_INVALID")
    drawings: list[Path] = []

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: (entry.name.casefold(), entry.name))
        except OSError:
            _fail("SOURCE_SCAN_FAILED")
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                _fail("SOURCE_SCAN_FAILED")
            path = Path(entry.path)
            if entry.is_symlink() or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                if path.suffix.casefold() in _DRAWING_SUFFIXES or stat.S_ISDIR(metadata.st_mode):
                    _fail("REPARSE_POINT_NOT_ALLOWED")
                continue
            if stat.S_ISDIR(metadata.st_mode):
                visit(path)
            elif path.suffix.casefold() in _DRAWING_SUFFIXES:
                if not stat.S_ISREG(metadata.st_mode):
                    _fail("DRAWING_NOT_REGULAR")
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(resolved_root)
                except (OSError, ValueError):
                    _fail("SOURCE_PATH_ESCAPE")
                drawings.append(resolved)

    visit(resolved_root)
    return drawings


def _local_sources(manifest_path: Path, source_root: Path) -> tuple[list[_DrawingSource], str]:
    manifest = _load_json(manifest_path, "LOCAL_MANIFEST_UNREADABLE")
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("manifest_kind") != "development_corpus_intake"
        or not isinstance(manifest.get("cases"), list)
        or manifest.get("case_count") != len(cast(list[object], manifest["cases"]))
    ):
        _fail("LOCAL_MANIFEST_INVALID")

    declared: dict[str, tuple[int, str, bool, str]] = {}
    seen_manifest_hashes: set[str] = set()
    for raw_case in cast(list[object], manifest["cases"]):
        if not isinstance(raw_case, Mapping):
            _fail("LOCAL_MANIFEST_INVALID")
        digest = raw_case.get("sha256")
        size = raw_case.get("size_bytes")
        source_kind = raw_case.get("source_kind")
        format_info = raw_case.get("format")
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(source_kind, str)
            or not isinstance(format_info, Mapping)
        ):
            _fail("LOCAL_MANIFEST_INVALID")
        if source_kind not in _LOCAL_SOURCE_KINDS:
            _fail("LOCAL_SOURCE_CLASSIFICATION_INVALID")
        normalized_hash = digest.casefold()
        declared_format = format_info.get("declared_format")
        detected_format = format_info.get("detected_format")
        header_status = format_info.get("header_status")
        is_drawing = (
            declared_format in {"dxf", "dwg"}
            and detected_format == declared_format
            and header_status == "recognized"
        )
        eligible = is_drawing and source_kind == "customer_local"
        if normalized_hash in seen_manifest_hashes:
            _fail("DUPLICATE_SOURCE_HASH")
        seen_manifest_hashes.add(normalized_hash)
        if is_drawing:
            declared[normalized_hash] = (size, str(declared_format), eligible, source_kind)

    actual: dict[str, tuple[Path, int, str]] = {}
    for path in _walk_drawings(source_root):
        digest, size = _hash_regular_file(path, "LOCAL_SOURCE_UNREADABLE")
        if digest in actual:
            _fail("DUPLICATE_SOURCE_HASH")
        actual[digest] = (path, size, path.suffix.casefold().removeprefix("."))
    if set(actual) != set(declared):
        _fail("LOCAL_SOURCE_MANIFEST_MISMATCH")

    manifest_digest = _canonical_sha256(manifest)
    sources: list[_DrawingSource] = []
    for digest, (declared_size, drawing_format, eligible, source_kind) in declared.items():
        path, actual_size, actual_format = actual[digest]
        if actual_size != declared_size or actual_format != drawing_format:
            _fail("LOCAL_SOURCE_MANIFEST_MISMATCH")
        if not eligible:
            continue
        provenance = {
            "origin": "local_intake",
            "manifest_sha256": manifest_digest,
            "source_kind": source_kind,
            "source_record_sha256": _canonical_sha256(
                next(
                    item
                    for item in cast(list[object], manifest["cases"])
                    if isinstance(item, Mapping)
                    and str(item.get("sha256", "")).casefold() == digest
                )
            ),
        }
        sources.append(
            _DrawingSource(
                path=path,
                sha256=digest,
                size_bytes=actual_size,
                drawing_format=drawing_format,
                origin="local_intake",
                source_class="customer_local_unreviewed",
                synthetic=False,
                development_fixture=False,
                provenance_sha256=_canonical_sha256(provenance),
            )
        )
    return sources, manifest_digest


def _public_sources(lock_path: Path, source_root: Path) -> tuple[list[_DrawingSource], str, str]:
    lock = _load_json(lock_path, "PUBLIC_LOCK_UNREADABLE")
    raw_sources = lock.get("sources")
    try:
        source_contract = load_manifest(PUBLIC_CORPUS_CONFIG_PATH)
    except CorpusFetchError:
        _fail("PUBLIC_SOURCE_CONTRACT_INVALID")
    expected_lock_fields = {
        "schema_version",
        "manifest_sha256",
        "manifest",
        "source_count",
        "sources",
    }
    if (
        set(lock) != expected_lock_fields
        or lock.get("schema_version") != "1.0"
        or not isinstance(raw_sources, list)
        or lock.get("manifest_sha256") != source_contract.manifest_sha256
        or lock.get("manifest") != source_contract.metadata
        or lock.get("source_count") != len(source_contract.sources)
        or len(raw_sources) != len(source_contract.sources)
    ):
        _fail("PUBLIC_LOCK_CONTRACT_MISMATCH")
    root = _directory(source_root, "SOURCE_ROOT_INVALID")
    inventory_paths = set(_walk_drawings(root))
    locked_drawing_paths: set[Path] = set()
    seen_hashes: set[str] = set()
    sources: list[_DrawingSource] = []
    for raw_entry, contracted_source in zip(raw_sources, source_contract.sources, strict=True):
        if not isinstance(raw_entry, Mapping) or not isinstance(raw_entry.get("source"), Mapping):
            _fail("PUBLIC_LOCK_CONTRACT_MISMATCH")
        source = cast(Mapping[str, Any], raw_entry["source"])
        if source != contracted_source.metadata:
            _fail("PUBLIC_LOCK_CONTRACT_MISMATCH")
        relative = _safe_relative_path(source.get("output"), "PUBLIC_LOCK_INVALID")
        suffix = PurePosixPath(relative).suffix.casefold()
        digest = raw_entry.get("sha256")
        size = raw_entry.get("size_bytes")
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > contracted_source.max_bytes
            or (
                contracted_source.expected_sha256 is not None
                and isinstance(digest, str)
                and digest.casefold() != contracted_source.expected_sha256
            )
        ):
            _fail("PUBLIC_LOCK_INVALID")
        if suffix not in _DRAWING_SUFFIXES:
            continue
        normalized_hash = digest.casefold()
        if normalized_hash in seen_hashes:
            _fail("DUPLICATE_SOURCE_HASH")
        seen_hashes.add(normalized_hash)
        candidate = root.joinpath(*relative.parts)
        try:
            parent = candidate.parent.resolve(strict=True)
            parent.relative_to(root)
        except (OSError, ValueError):
            _fail("SOURCE_PATH_ESCAPE")
        path = parent / candidate.name
        actual_hash, actual_size = _hash_regular_file(path, "PUBLIC_SOURCE_UNREADABLE")
        if actual_hash != normalized_hash or actual_size != size:
            _fail("PUBLIC_SOURCE_LOCK_MISMATCH")
        locked_drawing_paths.add(path.resolve(strict=True))
        sources.append(
            _DrawingSource(
                path=path,
                sha256=normalized_hash,
                size_bytes=actual_size,
                drawing_format=suffix.removeprefix("."),
                origin="public_fetch_lock",
                source_class="licensed_public_development",
                synthetic=True,
                development_fixture=True,
                provenance_sha256=_canonical_sha256(source),
            )
        )
    if inventory_paths != locked_drawing_paths:
        _fail("PUBLIC_SOURCE_LOCK_MISMATCH")
    return sources, _canonical_sha256(lock), source_contract.manifest_sha256


def _resolve_output_target(output_root: Path, allowed_parent: Path) -> tuple[Path, Path]:
    parent_root = _directory(allowed_parent, "OUTPUT_ALLOWLIST_INVALID")
    candidate = output_root if output_root.is_absolute() else parent_root / output_root
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
        resolved_parent.relative_to(parent_root)
    except (OSError, ValueError):
        _fail("OUTPUT_PATH_NOT_ALLOWED")
    target = resolved_parent / candidate.name
    if not candidate.name or candidate.name in {".", ".."}:
        _fail("OUTPUT_PATH_NOT_ALLOWED")
    try:
        target.lstat()
    except FileNotFoundError:
        return target, parent_root
    except OSError:
        _fail("OUTPUT_PATH_NOT_ALLOWED")
    _fail("OUTPUT_ALREADY_EXISTS")


def _write_exclusive(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        _fail("OUTPUT_WRITE_FAILED")


def _copy_exact(source: _DrawingSource, target: Path) -> None:
    before = _regular_file(source.path, "SOURCE_UNREADABLE")
    digest = hashlib.sha256()
    try:
        with source.path.open("rb") as input_stream, target.open("xb") as output_stream:
            opened = os.fstat(input_stream.fileno())
            if _state(opened) != _state(before):
                _fail("SOURCE_CHANGED_DURING_READ")
            for chunk in iter(lambda: input_stream.read(_COPY_CHUNK_BYTES), b""):
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except EngineerReviewPacketError:
        raise
    except OSError:
        _fail("OUTPUT_WRITE_FAILED")
    after = _regular_file(source.path, "SOURCE_UNREADABLE")
    if (
        _state(before) != _state(after)
        or digest.hexdigest() != source.sha256
        or target.stat().st_size != source.size_bytes
    ):
        _fail("SOURCE_CHANGED_DURING_READ")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    with suppress(OSError):
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _artifact_hash_template(*, takeoff: bool, source_sha256: str) -> dict[str, str | None]:
    fields = [*_BASE_ARTIFACTS, *(_TAKEOFF_ARTIFACTS if takeoff else ())]
    return {
        field: source_sha256 if takeoff and field == "input_drawing" else None for field in fields
    }


def _claim_templates(
    *,
    case_id: str,
    source_sha256: str,
    source_class: str,
    synthetic: bool,
    development_fixture: bool,
    takeoff: bool,
) -> dict[str, dict[str, Any]]:
    artifact_hashes = _artifact_hash_template(
        takeoff=takeoff,
        source_sha256=source_sha256,
    )
    claims: dict[str, dict[str, Any]] = {
        "engineer_selector": {
            "evidence_kind": "production_golden_selection_attestation",
            "manifest_kind": "reviewed_production_golden_corpus",
            "case_id": case_id,
            "source_sha256": source_sha256,
            "provenance_sha256": None,
            "source_class": source_class,
            "synthetic": synthetic,
            "development_fixture": development_fixture,
            "source_semantic_evidence_sha256": None,
            "source_drawing_model_sha256": None,
            "artifact_sha256": artifact_hashes,
            "selected": False,
        },
        "golden_reviewer": {
            "evidence_kind": "production_golden_review_attestation",
            "manifest_kind": "reviewed_production_golden_corpus",
            "case_id": case_id,
            "source_sha256": source_sha256,
            "provenance_sha256": None,
            "source_class": source_class,
            "synthetic": synthetic,
            "development_fixture": development_fixture,
            "source_semantic_evidence_sha256": None,
            "source_drawing_model_sha256": None,
            "artifact_sha256": artifact_hashes,
            "review_evidence_sha256": None,
            "accepted": False,
        },
        "company_profile_approver": {
            "evidence_kind": "company_profile_approval_attestation",
            "company_profile_ref": None,
            "company_profile_sha256": None,
            "approved": False,
        },
    }
    if takeoff:
        calculation_claims: dict[str, Any] = {
            "evidence_kind": "takeoff_calculation_attestation",
            "case_id": case_id,
            "source_sha256": source_sha256,
            "takeoff_request_sha256": None,
            "expected_takeoff_sha256": None,
            "recomputed_takeoff_sha256": None,
            "calculation_evidence_sha256": None,
        }
        claims.update(
            {
                "takeoff_calculator": calculation_claims,
                "takeoff_reviewer": {
                    **calculation_claims,
                    "evidence_kind": "takeoff_review_attestation",
                    "calculator_identity": None,
                    "accepted": False,
                },
                "material_table_approver": {
                    "evidence_kind": "material_table_approval_attestation",
                    "case_id": case_id,
                    "material_profile_ref": None,
                    "material_table_sha256": None,
                    "approval_evidence_sha256": None,
                    "approved": False,
                },
            }
        )
    return claims


def _attestation_workflow(*, case_id: str, claim_refs: Mapping[str, str]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "instructions_kind": "production_evidence_attestation_workflow_pending",
        "case_id": case_id,
        "production_evidence": False,
        "ready_to_sign": False,
        "rules": [
            "Replace every null and false claim value only from independently verified evidence.",
            "Recompute artifact hashes and production-derived outputs before signing.",
            (
                "Use a separately controlled Ed25519 trust policy and pin its canonical "
                "SHA-256 digest."
            ),
            (
                "Keep each issuer private key in its own issuer-side environment variable; "
                "never copy private keys into the packet or policy."
            ),
            "Keep selector/reviewer and takeoff calculator/reviewer identities distinct.",
            "Copy each issued attestation JSON into the named manifest field; do not alter it.",
        ],
        "roles": {
            role: {
                "claims_ref": claim_ref,
                "attestation_output_ref": (f"reviews/{case_id}/attestations/{role}.json"),
                "manifest_field": {
                    "engineer_selector": "selector_attestation",
                    "golden_reviewer": "review.attestation",
                    "company_profile_approver": "company_profile_attestation",
                    "takeoff_calculator": "takeoff.calculator_attestation",
                    "takeoff_reviewer": "takeoff.reviewer_attestation",
                    "material_table_approver": "takeoff.material_table.attestation",
                }[role],
                "issue_command_argv": [
                    "scripts/issue_evidence_attestation.py",
                    "--trust-policy",
                    "<operator-supplied-trust-policy.json>",
                    "--claims",
                    claim_ref,
                    "--output",
                    f"reviews/{case_id}/attestations/{role}.json",
                    "--identity-id",
                    "<trusted-opaque-identity-id>",
                    "--role",
                    role,
                    "--private-key-env",
                    "<issuer-private-key-environment-variable>",
                    "--expected-policy-sha256",
                    "<pinned-canonical-policy-sha256>",
                ],
            }
            for role, claim_ref in claim_refs.items()
        },
    }


def _review_form(
    *, case_id: str, source: _DrawingSource, packet_digest: str, takeoff: bool
) -> dict[str, Any]:
    required = [*_BASE_ARTIFACTS, *(_TAKEOFF_ARTIFACTS if takeoff else ())]
    return {
        "schema_version": "1.0",
        "form_kind": "engineer_human_review_pending",
        "status": "pending",
        "case_id": case_id,
        "packet_digest_sha256": packet_digest,
        "source_sha256": source.sha256,
        "provenance_sha256": source.provenance_sha256,
        "source_class": source.source_class,
        "synthetic": source.synthetic,
        "development_fixture": source.development_fixture,
        "production_evidence": False,
        "engineer_selected": False,
        "company_approved": False,
        "required_artifact_checklist": [
            {
                "artifact": artifact,
                "artifact_ref": None,
                "sha256": None,
                "reviewed": False,
            }
            for artifact in required
        ],
        "review": {
            "reviewer_identity": None,
            "reviewed_at": None,
            "evidence_ref": None,
            "decision": None,
            "notes": None,
            "attestation": None,
        },
        "selector_attestation": None,
        "company_profile_attestation": None,
        "takeoff_review": (
            {
                "calculated_by": None,
                "reviewer_identity": None,
                "calculation_source": {
                    "evidence_ref": None,
                    "artifact_ref": None,
                    "sha256": None,
                },
                "material_profile_ref": None,
                "company_approved": False,
                "material_table_artifact": None,
                "material_table_approval": {
                    "evidence_ref": None,
                    "artifact_ref": None,
                    "sha256": None,
                },
                "calculator_attestation": None,
                "reviewer_attestation": None,
                "material_table_attestation": None,
            }
            if takeoff
            else None
        ),
    }


def build_engineer_review_packet(
    *,
    local_manifest_path: Path,
    local_source_root: Path,
    public_lock_path: Path,
    public_source_root: Path,
    output_root: Path,
    allowed_output_parent: Path = DEFAULT_ALLOWED_OUTPUT_PARENT,
) -> dict[str, Any]:
    """Validate sources and atomically publish an approval-neutral review packet."""
    target, _ = _resolve_output_target(output_root, allowed_output_parent)
    local, local_manifest_digest = _local_sources(local_manifest_path, local_source_root)
    public, public_lock_digest, public_contract_digest = _public_sources(
        public_lock_path, public_source_root
    )
    sources = sorted([*local, *public], key=lambda item: item.sha256)
    hashes = [source.sha256 for source in sources]
    if len(hashes) != len(set(hashes)):
        _fail("DUPLICATE_SOURCE_HASH")
    if not MIN_CASES <= len(sources) <= MAX_CASES:
        _fail("CASE_COUNT_OUT_OF_RANGE")
    dxf_sources = [source for source in sources if source.drawing_format == "dxf"]
    if len(dxf_sources) < MIN_TAKEOFF_CASES:
        _fail("TAKEOFF_CASE_COUNT_TOO_LOW")
    takeoff_hashes = {source.sha256 for source in dxf_sources[:MIN_TAKEOFF_CASES]}

    binding_cases = [
        {
            "case_id": f"sha256-{source.sha256}",
            "source_sha256": source.sha256,
            "size_bytes": source.size_bytes,
            "format": source.drawing_format,
            "origin": source.origin,
            "source_class": source.source_class,
            "synthetic": source.synthetic,
            "development_fixture": source.development_fixture,
            "provenance_sha256": source.provenance_sha256,
            "takeoff_review_reserved": source.sha256 in takeoff_hashes,
        }
        for source in sources
    ]
    binding = {
        "schema_version": "1.0",
        "packet_kind": "engineer_review_packet_draft",
        "local_intake_manifest_sha256": local_manifest_digest,
        "public_fetch_lock_sha256": public_lock_digest,
        "public_source_contract_sha256": public_contract_digest,
        "cases": binding_cases,
    }
    packet_digest = _canonical_sha256(binding)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    published = False
    try:
        source_dir = temporary / "sources"
        review_dir = temporary / "reviews"
        source_dir.mkdir()
        review_dir.mkdir()
        manifest_cases: list[dict[str, Any]] = []
        production_manifest_cases: list[dict[str, Any]] = []
        for source, binding_case in zip(sources, binding_cases, strict=True):
            case_id = str(binding_case["case_id"])
            source_ref = f"sources/{source.sha256}.{source.drawing_format}"
            copied = source_dir / f"{source.sha256}.{source.drawing_format}"
            _copy_exact(source, copied)
            form_ref = f"reviews/{case_id}/human-review-form.pending.json"
            case_review_dir = review_dir / case_id
            case_review_dir.mkdir()
            takeoff = source.sha256 in takeoff_hashes
            form = _review_form(
                case_id=case_id,
                source=source,
                packet_digest=packet_digest,
                takeoff=takeoff,
            )
            _write_exclusive(
                case_review_dir / "human-review-form.pending.json", _canonical_bytes(form)
            )
            claims_dir = case_review_dir / "claims"
            claims_dir.mkdir()
            claims = _claim_templates(
                case_id=case_id,
                source_sha256=source.sha256,
                source_class=source.source_class,
                synthetic=source.synthetic,
                development_fixture=source.development_fixture,
                takeoff=takeoff,
            )
            claim_refs: dict[str, str] = {}
            role_order = [
                *_BASE_ATTESTATION_ROLES,
                *(_TAKEOFF_ATTESTATION_ROLES if takeoff else ()),
            ]
            for role in role_order:
                claim_ref = f"reviews/{case_id}/claims/{role}.template.json"
                claim_refs[role] = claim_ref
                _write_exclusive(
                    claims_dir / f"{role}.template.json",
                    _canonical_bytes(claims[role]),
                )
            instructions_ref = f"reviews/{case_id}/attestation-workflow.pending.json"
            _write_exclusive(
                case_review_dir / "attestation-workflow.pending.json",
                _canonical_bytes(_attestation_workflow(case_id=case_id, claim_refs=claim_refs)),
            )
            source_pointer = {
                "artifact_ref": source_ref,
                "sha256": source.sha256,
                "provenance_ref": None,
                "provenance": None,
                "source_class": source.source_class,
                "synthetic": source.synthetic,
                "development_fixture": source.development_fixture,
            }
            if source.drawing_format == "dxf":
                source_pointer["semantic_snapshot"] = None
            else:
                source_pointer["drawing_model"] = None
                source_pointer["bridge_evidence"] = None
            artifact_slots: dict[str, object] = {artifact: None for artifact in _BASE_ARTIFACTS}
            if takeoff:
                artifact_slots.update(
                    {
                        "input_drawing": {
                            "artifact_ref": source_ref,
                            "sha256": source.sha256,
                        },
                        "takeoff_request": None,
                        "expected_takeoff": None,
                    }
                )
            case_template: dict[str, Any] = {
                **binding_case,
                "case_type": "takeoff" if takeoff else "design",
                "production_evidence": False,
                "review_status": "pending",
                "engineer_selected": False,
                "company_approved": False,
                "selector_identity": None,
                "selector_attestation": None,
                "company_profile_attestation": None,
                "source_drawing": source_pointer,
                "human_review_form_ref": form_ref,
                "attestation_workflow_ref": instructions_ref,
                "claim_template_refs": claim_refs,
                "artifacts": artifact_slots,
                "review": {
                    "reviewer_identity": None,
                    "evidence_ref": None,
                    "artifact_ref": None,
                    "sha256": None,
                    "attestation": None,
                },
                "takeoff": (
                    {
                        "calculated_by": None,
                        "reviewer_identity": None,
                        "calculation_source": {
                            "evidence_ref": None,
                            "artifact_ref": None,
                            "sha256": None,
                        },
                        "calculator_attestation": None,
                        "reviewer_attestation": None,
                        "material_table": {
                            "ref": None,
                            "company_approved": False,
                            "table": None,
                            "approval": {
                                "evidence_ref": None,
                                "artifact_ref": None,
                                "sha256": None,
                            },
                            "attestation": None,
                        },
                    }
                    if takeoff
                    else None
                ),
            }
            manifest_cases.append(case_template)
            production_manifest_cases.append(dict(case_template))
        production_manifest = {
            "schema_version": "1.0",
            "manifest_kind": "reviewed_production_golden_corpus",
            "production_evidence": False,
            "review_status": "pending",
            "cases": production_manifest_cases,
        }
        _write_exclusive(
            temporary / "production-manifest.template.json",
            _canonical_bytes(production_manifest),
        )
        manifest = {
            "schema_version": "1.0",
            "manifest_kind": "engineer_review_packet_draft",
            "packet_digest_sha256": packet_digest,
            "production_evidence": False,
            "review_status": "pending",
            "engineer_selected": False,
            "company_approved": False,
            "input_bindings": {
                "local_intake_manifest_sha256": local_manifest_digest,
                "public_fetch_lock_sha256": public_lock_digest,
                "public_source_contract_sha256": public_contract_digest,
            },
            "case_count": len(manifest_cases),
            "takeoff_review_case_count": len(takeoff_hashes),
            "takeoff_review_case_ids": [
                str(case["case_id"])
                for case in manifest_cases
                if case["takeoff_review_reserved"] is True
            ],
            "production_manifest_template_ref": "production-manifest.template.json",
            "cases": manifest_cases,
        }
        _write_exclusive(
            temporary / "engineer-review-manifest.draft.json", _canonical_bytes(manifest)
        )
        for directory in [*sorted(review_dir.iterdir()), source_dir, review_dir, temporary]:
            _fsync_directory(directory)
        if target.exists():
            _fail("OUTPUT_ALREADY_EXISTS")
        try:
            os.rename(temporary, target)
        except FileExistsError:
            _fail("OUTPUT_ALREADY_EXISTS")
        except OSError:
            _fail("OUTPUT_PUBLISH_FAILED")
        published = True
        _fsync_directory(target.parent)
        return manifest
    finally:
        if not published:
            with suppress(OSError):
                shutil.rmtree(temporary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-manifest", required=True, type=Path)
    parser.add_argument("--local-source-root", required=True, type=Path)
    parser.add_argument("--public-lock", required=True, type=Path)
    parser.add_argument("--public-source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument(
        "--allowed-output-parent",
        type=Path,
        default=DEFAULT_ALLOWED_OUTPUT_PARENT,
        help="existing private data directory beneath which the new packet is published",
    )
    args = parser.parse_args(argv)
    try:
        manifest = build_engineer_review_packet(
            local_manifest_path=args.local_manifest,
            local_source_root=args.local_source_root,
            public_lock_path=args.public_lock,
            public_source_root=args.public_source_root,
            output_root=args.output_root,
            allowed_output_parent=args.allowed_output_parent,
        )
    except EngineerReviewPacketError as exc:
        print(
            json.dumps({"error": exc.code}, sort_keys=True, separators=(",", ":")), file=sys.stderr
        )
        return 1
    print(
        json.dumps(
            {
                "case_count": manifest["case_count"],
                "packet_digest_sha256": manifest["packet_digest_sha256"],
                "production_evidence": False,
                "takeoff_review_case_count": manifest["takeoff_review_case_count"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
