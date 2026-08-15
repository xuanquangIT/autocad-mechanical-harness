"""Build a deterministic, privacy-safe manifest for a local development corpus.

This is an offline intake tool, not a production-acceptance verifier. It never
downloads data, never opens DWG through AutoCAD, and never emits source paths or
filenames. Public-license metadata and optional local labels must be supplied
explicitly in separate YAML files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import stat
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, NoReturn, cast
from urllib.parse import urlsplit

import ezdxf
import yaml

_ALLOWED_SUFFIXES: Final = frozenset({".dxf", ".dwg", ".png", ".jpg", ".jpeg", ".tif", ".tiff"})
_DECLARED_FORMAT: Final = {
    ".dxf": "dxf",
    ".dwg": "dwg",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".tif": "tiff",
    ".tiff": "tiff",
}
_DWG_VERSIONS: Final = {
    "AC1009": "AutoCAD R12",
    "AC1012": "AutoCAD R13",
    "AC1014": "AutoCAD R14",
    "AC1015": "AutoCAD 2000/2002",
    "AC1018": "AutoCAD 2004/2005/2006",
    "AC1021": "AutoCAD 2007/2008/2009",
    "AC1024": "AutoCAD 2010/2011/2012",
    "AC1027": "AutoCAD 2013/2014/2015/2016/2017",
    "AC1032": "AutoCAD 2018 or later",
}
_DWG_SIGNATURE = re.compile(rb"^AC[0-9]{4}")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]")
_MAX_METADATA_BYTES: Final = 1024 * 1024
_HEADER_BYTES: Final = 4096
MAX_DXF_BYTES: Final = 16 * 1024 * 1024
MAX_DXF_ENTITIES: Final = 20_000


class CorpusIntakeError(ValueError):
    """A fail-closed intake error whose message never contains a customer path."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _SourceFile:
    path: Path
    relative_key: str


@dataclass(frozen=True)
class _HashedSource:
    sha256: str
    size_bytes: int
    header: bytes


@dataclass(frozen=True)
class _LocalMetadata:
    label: str | None = None
    source_kind: str = "customer_local"


def _fail(code: str) -> NoReturn:
    raise CorpusIntakeError(code)


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


def _validate_root(root: Path) -> Path:
    metadata = _safe_lstat(root, "INPUT_ROOT_UNREADABLE")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("INPUT_ROOT_NOT_DIRECTORY")
    try:
        return root.resolve(strict=True)
    except OSError:
        _fail("INPUT_ROOT_UNREADABLE")


def _scan_source_files(root: Path) -> list[_SourceFile]:
    resolved_root = _validate_root(root)
    sources: list[_SourceFile] = []

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: (entry.name.casefold(), entry.name))
        except OSError:
            _fail("INPUT_SCAN_FAILED")
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                _fail("INPUT_SCAN_FAILED")
            if entry.is_symlink() or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                _fail("REPARSE_POINT_NOT_ALLOWED")
            entry_path = Path(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                visit(entry_path)
                continue
            suffix = entry_path.suffix.casefold()
            if suffix not in _ALLOWED_SUFFIXES:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                _fail("SUPPORTED_ENTRY_NOT_REGULAR")
            try:
                resolved = entry_path.resolve(strict=True)
                relative = resolved.relative_to(resolved_root).as_posix()
            except (OSError, ValueError):
                _fail("SOURCE_PATH_ESCAPE")
            sources.append(_SourceFile(path=resolved, relative_key=relative))

    visit(resolved_root)
    sources.sort(key=lambda item: (item.relative_key.casefold(), item.relative_key))
    folded = [item.relative_key.casefold() for item in sources]
    if len(folded) != len(set(folded)):
        _fail("SOURCE_REFERENCE_COLLISION")
    return sources


def _source_state(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _hash_source(source: _SourceFile) -> _HashedSource:
    before = _safe_lstat(source.path, "SOURCE_UNREADABLE")
    if not stat.S_ISREG(before.st_mode):
        _fail("SUPPORTED_ENTRY_NOT_REGULAR")
    digest = hashlib.sha256()
    header = bytearray()
    try:
        with source.path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                if len(header) < _HEADER_BYTES:
                    header.extend(chunk[: _HEADER_BYTES - len(header)])
    except OSError:
        _fail("SOURCE_UNREADABLE")
    after = _safe_lstat(source.path, "SOURCE_UNREADABLE")
    if _source_state(before) != _source_state(after):
        _fail("SOURCE_CHANGED_DURING_READ")
    return _HashedSource(sha256=digest.hexdigest(), size_bytes=after.st_size, header=bytes(header))


def _looks_like_ascii_dxf(header: bytes) -> bool:
    normalized = header.removeprefix(b"\xef\xbb\xbf").lstrip().upper()
    return re.match(rb"0\s*\r?\nSECTION(?:\s|\r|\n)", normalized) is not None


def _classify_header(declared_format: str, header: bytes) -> dict[str, str]:
    detected = "unknown"
    signature = "UNRECOGNIZED"
    dwg_version: str | None = None
    if match := _DWG_SIGNATURE.match(header):
        detected = "dwg"
        signature = match.group(0).decode("ascii")
        dwg_version = _DWG_VERSIONS.get(signature, "Unknown DWG generation")
    elif header.startswith(b"AutoCAD Binary DXF\r\n\x1a\x00"):
        detected = "dxf"
        signature = "BINARY_DXF"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "png"
        signature = "PNG"
    elif header.startswith(b"\xff\xd8\xff"):
        detected = "jpeg"
        signature = "JPEG"
    elif header.startswith((b"II*\x00", b"MM\x00*")):
        detected = "tiff"
        signature = "TIFF_LE" if header.startswith(b"II") else "TIFF_BE"
    elif _looks_like_ascii_dxf(header):
        detected = "dxf"
        signature = "ASCII_DXF"

    if detected == "unknown":
        status = "unrecognized"
    elif detected == declared_format:
        status = "recognized"
    else:
        status = "extension_mismatch"
    result = {
        "declared_format": declared_format,
        "detected_format": detected,
        "header_status": status,
        "signature": signature,
    }
    if dwg_version is not None:
        result["dwg_version"] = dwg_version
    return result


def _dxf_semantic_summary(path: Path, size_bytes: int) -> dict[str, Any]:
    if size_bytes > MAX_DXF_BYTES:
        return {
            "status": "skipped",
            "reason": "DXF_BYTE_LIMIT_EXCEEDED",
            "byte_limit": MAX_DXF_BYTES,
        }
    ezdxf_logger = logging.getLogger("ezdxf")
    logger_was_disabled = ezdxf_logger.disabled
    try:
        ezdxf_logger.disabled = True
        document = ezdxf.readfile(path)
        modelspace = document.modelspace()
        counts: Counter[str] = Counter()
        scanned = 0
        truncated = False
        for entity in modelspace:
            if scanned >= MAX_DXF_ENTITIES:
                truncated = True
                break
            counts[entity.dxftype()] += 1
            scanned += 1
        units_value = document.header.get("$INSUNITS", 0)
        units_code = int(units_value) if isinstance(units_value, (int, float)) else 0
        return {
            "status": "truncated" if truncated else "parsed",
            "entities_scanned": scanned,
            "entity_limit": MAX_DXF_ENTITIES,
            "entity_type_counts": dict(sorted(counts.items())),
            "units_code": units_code,
        }
    except Exception:
        # DXF is untrusted intake. Keep parser details and customer paths out of output.
        return {"status": "error", "reason": "DXF_PARSE_FAILED"}
    finally:
        ezdxf_logger.disabled = logger_was_disabled


def _semantic_summary(
    source: _SourceFile, format_header: Mapping[str, str], size_bytes: int
) -> dict[str, Any]:
    declared = format_header["declared_format"]
    if format_header["header_status"] != "recognized":
        return {"status": "unsupported", "reason": "FORMAT_HEADER_NOT_RECOGNIZED"}
    if declared == "dwg":
        return {"status": "offline_header_only", "reason": "DWG_HEADER_ONLY_OFFLINE"}
    if declared == "dxf":
        return _dxf_semantic_summary(source.path, size_bytes)
    return {"status": "not_applicable"}


def _metadata_relative_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        _fail("METADATA_REFERENCE_INVALID")
    normalized = value.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", normalized) or normalized.startswith("/"):
        _fail("METADATA_PATH_ESCAPE")
    relative = PurePosixPath(normalized)
    if ".." in relative.parts or relative.as_posix() in {"", "."}:
        _fail("METADATA_PATH_ESCAPE")
    return relative.as_posix()


def _load_yaml_file(path: Path) -> Mapping[str, Any]:
    metadata = _safe_lstat(path, "METADATA_UNREADABLE")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_METADATA_BYTES:
        _fail("METADATA_UNREADABLE")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        _fail("METADATA_UNREADABLE")
    if not isinstance(loaded, Mapping) or not all(isinstance(key, str) for key in loaded):
        _fail("METADATA_SCHEMA_INVALID")
    return cast(Mapping[str, Any], loaded)


def _metadata_entries(path: Path) -> Mapping[str, Any]:
    payload = _load_yaml_file(path)
    if not set(payload).issubset({"schema_version", "files"}):
        _fail("METADATA_SCHEMA_INVALID")
    if payload.get("schema_version", "1.0") != "1.0":
        _fail("METADATA_SCHEMA_INVALID")
    files = payload.get("files")
    if not isinstance(files, Mapping) or not all(isinstance(key, str) for key in files):
        _fail("METADATA_SCHEMA_INVALID")
    return cast(Mapping[str, Any], files)


def _valid_web_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2048 or "\x00" in value or "\\" in value:
        return False
    parsed = urlsplit(value)
    public_components = " ".join((parsed.path, parsed.query, parsed.fragment))
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and _WINDOWS_ABSOLUTE_PATH.search(public_components) is None
    )


def _valid_retrieved_at(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _load_public_provenance(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    raw_entries = _metadata_entries(path)
    required = {"source_url", "license_id", "license_url", "retrieved_at", "expected_sha256"}
    result: dict[str, dict[str, str]] = {}
    for raw_key, raw_value in raw_entries.items():
        key = _metadata_relative_key(raw_key).casefold()
        if key in result or not isinstance(raw_value, Mapping) or set(raw_value) != required:
            _fail("PUBLIC_PROVENANCE_INVALID")
        license_id = raw_value.get("license_id")
        expected_hash = raw_value.get("expected_sha256")
        if (
            not _valid_web_url(raw_value.get("source_url"))
            or not _valid_web_url(raw_value.get("license_url"))
            or not isinstance(license_id, str)
            or not license_id.strip()
            or len(license_id) > 128
            or not _valid_retrieved_at(raw_value.get("retrieved_at"))
            or not isinstance(expected_hash, str)
            or _SHA256.fullmatch(expected_hash) is None
        ):
            _fail("PUBLIC_PROVENANCE_INVALID")
        result[key] = {
            "source_url": str(raw_value["source_url"]),
            "license_id": license_id.strip(),
            "license_url": str(raw_value["license_url"]),
            "retrieved_at": str(raw_value["retrieved_at"]),
            "expected_sha256": expected_hash.casefold(),
        }
    return result


def _safe_local_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        _fail("LOCAL_LABEL_INVALID")
    label = value.strip()
    if (
        label.startswith(("/", "\\\\"))
        or _WINDOWS_ABSOLUTE_PATH.search(label)
        or "\\\\" in label
        or "\x00" in label
        or any(ord(character) < 32 for character in label)
    ):
        _fail("LOCAL_LABEL_INVALID")
    return label


def _load_local_metadata(path: Path | None) -> dict[str, _LocalMetadata]:
    if path is None:
        return {}
    raw_entries = _metadata_entries(path)
    result: dict[str, _LocalMetadata] = {}
    for raw_key, raw_value in raw_entries.items():
        key = _metadata_relative_key(raw_key).casefold()
        if key in result:
            _fail("LOCAL_METADATA_INVALID")
        if isinstance(raw_value, str):
            result[key] = _LocalMetadata(label=_safe_local_label(raw_value))
            continue
        if not isinstance(raw_value, Mapping) or not set(raw_value).issubset(
            {"label", "source_kind"}
        ):
            _fail("LOCAL_METADATA_INVALID")
        source_kind = raw_value.get("source_kind", "customer_local")
        if source_kind not in {"customer_local", "generated"}:
            _fail("LOCAL_METADATA_INVALID")
        label_value = raw_value.get("label")
        label = _safe_local_label(label_value) if label_value is not None else None
        result[key] = _LocalMetadata(label=label, source_kind=str(source_kind))
    return result


def build_development_corpus_manifest(
    root: Path,
    *,
    public_provenance_path: Path | None = None,
    local_label_map_path: Path | None = None,
) -> dict[str, Any]:
    """Return a deterministic development-only manifest for files below *root*."""
    sources = _scan_source_files(root)
    public = _load_public_provenance(public_provenance_path)
    local = _load_local_metadata(local_label_map_path)
    inventory_keys = {source.relative_key.casefold() for source in sources}
    if not set(public).issubset(inventory_keys):
        _fail("PUBLIC_PROVENANCE_FILE_NOT_FOUND")
    if not set(local).issubset(inventory_keys):
        _fail("LOCAL_METADATA_FILE_NOT_FOUND")

    cases: list[dict[str, Any]] = []
    for index, source in enumerate(sources, start=1):
        key = source.relative_key.casefold()
        hashed = _hash_source(source)
        declared_format = _DECLARED_FORMAT[source.path.suffix.casefold()]
        format_header = _classify_header(declared_format, hashed.header)
        provenance = public.get(key)
        local_metadata = local.get(key, _LocalMetadata())
        if provenance is not None and local_metadata.source_kind == "generated":
            _fail("SOURCE_CLASSIFICATION_CONFLICT")
        if provenance is not None and provenance["expected_sha256"] != hashed.sha256:
            _fail("PUBLIC_HASH_MISMATCH")
        source_kind = "public_licensed" if provenance is not None else local_metadata.source_kind
        case: dict[str, Any] = {
            "case_id": f"case-{index:04d}",
            "source_kind": source_kind,
            "sha256": hashed.sha256,
            "size_bytes": hashed.size_bytes,
            "format": format_header,
            "semantic_summary": _semantic_summary(source, format_header, hashed.size_bytes),
        }
        if provenance is not None:
            case["public_provenance"] = provenance
        if local_metadata.label is not None:
            case["local_label"] = local_metadata.label
        cases.append(case)

    return {
        "schema_version": "1.0",
        "manifest_kind": "development_corpus_intake",
        "production_claim_eligible": False,
        "privacy": {
            "source_filenames_omitted": True,
            "absolute_paths_omitted": True,
            "local_labels_included": local_label_map_path is not None,
        },
        "limits": {
            "dxf_max_bytes": MAX_DXF_BYTES,
            "dxf_max_entities": MAX_DXF_ENTITIES,
        },
        "case_count": len(cases),
        "cases": cases,
    }


def render_manifest(manifest: Mapping[str, Any]) -> str:
    """Serialize a manifest canonically so identical intake produces identical bytes."""
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _validate_output_target(output: Path, output_root: Path) -> Path:
    root = _validate_root(output_root)
    if output.suffix.casefold() != ".json":
        _fail("OUTPUT_FORMAT_INVALID")
    candidate = output if output.is_absolute() else root / output
    try:
        parent = candidate.parent.resolve(strict=True)
        target = parent / candidate.name
        target.relative_to(root)
    except (OSError, ValueError):
        _fail("OUTPUT_PATH_NOT_ALLOWED")
    relative_parent = parent.relative_to(root)
    current = root
    for part in relative_parent.parts:
        current /= part
        metadata = _safe_lstat(current, "OUTPUT_PATH_NOT_ALLOWED")
        if not stat.S_ISDIR(metadata.st_mode):
            _fail("OUTPUT_PATH_NOT_ALLOWED")
    try:
        target.lstat()
    except FileNotFoundError:
        return target
    except OSError:
        _fail("OUTPUT_PATH_NOT_ALLOWED")
    _fail("OUTPUT_ALREADY_EXISTS")


def write_manifest(text: str, *, output: Path, output_root: Path) -> None:
    """Write *text* once to an allowlisted target without overwriting existing data."""
    target = _validate_output_target(output, output_root)
    created = False
    try:
        with target.open("x", encoding="utf-8", newline="") as stream:
            created = True
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        if created:
            with suppress(OSError):
                target.unlink()
        _fail("OUTPUT_WRITE_FAILED")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="local corpus root (never emitted)")
    parser.add_argument("--public-provenance", type=Path, help="optional public-license YAML")
    parser.add_argument("--local-label-map", type=Path, help="optional explicit local-label YAML")
    parser.add_argument("--output", type=Path, help="write canonical JSON instead of stdout")
    parser.add_argument(
        "--output-root", type=Path, help="required allowlisted root whenever --output is used"
    )
    args = parser.parse_args(argv)
    try:
        if (args.output is None) != (args.output_root is None):
            _fail("OUTPUT_ALLOWLIST_REQUIRED")
        manifest = build_development_corpus_manifest(
            args.root,
            public_provenance_path=args.public_provenance,
            local_label_map_path=args.local_label_map,
        )
        rendered = render_manifest(manifest)
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            write_manifest(rendered, output=args.output, output_root=args.output_root)
    except CorpusIntakeError as error:
        sys.stderr.write(json.dumps({"error": {"code": error.code}}, sort_keys=True) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
