"""Fetch the pinned, licensed public development corpus safely.

This command never reads customer drawings.  Its only network inputs are the
version-controlled URLs in ``config/development-corpus.yaml`` and its only file
outputs are beneath an explicitly supplied directory under the ignored
``data/`` tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, Self

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPOSITORY_ROOT / "config" / "development-corpus.yaml"
DEFAULT_ALLOWED_OUTPUT_PARENT = REPOSITORY_ROOT / "data"
LOCK_FILENAME = "development-corpus.lock.json"
LOCK_SCHEMA_VERSION = "1.0"
NETWORK_TIMEOUT_SECONDS = 30.0
READ_CHUNK_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 128 * 1024 * 1024

_EXACT_DOWNLOAD_HOSTS = frozenset({"raw.githubusercontent.com", "archive.org"})
_ARCHIVE_MIRROR_HOST = re.compile(r"^ia[0-9]+\.us\.archive\.org$")
_ARCHIVE_DOWNLOAD_NODE_HOST = re.compile(r"^dn[0-9]+\.ca\.archive\.org$")
_SAFE_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_SAFE_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_DXF_SECTION = re.compile(rb"(?:^|\r?\n)[ \t]*0[ \t]*\r?\n[ \t]*SECTION(?:\r?\n|$)")
_CONTENT_EXTENSIONS = frozenset({".dxf", ".png", ".pdf"})
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "corpus_id",
        "purpose",
        "customer_inputs_allowed",
        "production_evidence",
        "sources",
    }
)
_REQUIRED_SOURCE_FIELDS = frozenset(
    {
        "source_id",
        "url",
        "source_index_url",
        "license_id",
        "license_url",
        "license_notice",
        "attribution",
        "output",
        "max_bytes",
        "intended_use",
        "expected_sha256",
    }
)
_OPTIONAL_SOURCE_FIELDS = frozenset(
    {
        "paired_with",
        "rights_url",
        "selected_page_raster_derivation_only",
    }
)


class CorpusFetchError(RuntimeError):
    """A fail-closed corpus configuration, download, or integrity error."""


class ResponseLike(Protocol):
    """Small response surface used by urllib and offline test doubles."""

    headers: Mapping[str, str]

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def geturl(self) -> str: ...

    def read(self, amount: int = -1) -> bytes: ...


OpenUrl = Callable[..., ResponseLike]


@dataclass(frozen=True)
class CorpusSource:
    """Validated source plus the exact license/provenance metadata to lock."""

    source_id: str
    url: str
    output: str
    max_bytes: int
    expected_sha256: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CorpusManifest:
    """Validated manifest and the exact hash of its version-controlled bytes."""

    manifest_sha256: str
    metadata: dict[str, Any]
    sources: tuple[CorpusSource, ...]


def _download_host_allowed(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.casefold()
    return (
        normalized in _EXACT_DOWNLOAD_HOSTS
        or _ARCHIVE_MIRROR_HOST.fullmatch(normalized) is not None
        or _ARCHIVE_DOWNLOAD_NODE_HOST.fullmatch(normalized) is not None
    )


def _validate_https_url(value: str, *, download: bool) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CorpusFetchError("URL has an invalid port") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or port not in {None, 443}
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise CorpusFetchError(
            "only plain HTTPS URLs without credentials, query, or fragment are allowed"
        )
    if download and not _download_host_allowed(parsed.hostname):
        raise CorpusFetchError("download host is not allowlisted")
    return parsed


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject a redirect before urllib connects to a non-allowlisted host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        validated_url = urllib.parse.urljoin(req.full_url, newurl)
        _validate_https_url(validated_url, download=True)
        return super().redirect_request(req, fp, code, msg, headers, validated_url)


def _default_opener(request: urllib.request.Request, *, timeout: float) -> ResponseLike:
    opener = urllib.request.build_opener(_AllowlistedRedirectHandler())
    return opener.open(request, timeout=timeout)  # type: ignore[no-any-return]


def _non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusFetchError(f"{field} must be a non-empty string")
    return value


def _validate_output_name(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or value != relative.as_posix()
        or len(relative.parts) < 2
        or any(
            part in {"", ".", ".."} or _SAFE_PATH_PART.fullmatch(part) is None
            for part in relative.parts
        )
    ):
        raise CorpusFetchError("source output must be a safe canonical relative path")
    if relative.suffix.casefold() not in _CONTENT_EXTENSIONS:
        raise CorpusFetchError("source output extension is unsupported")
    return relative


def _validate_source(raw: object) -> CorpusSource:
    if not isinstance(raw, Mapping):
        raise CorpusFetchError("every source must be a mapping")
    keys = frozenset(raw)
    if not _REQUIRED_SOURCE_FIELDS.issubset(keys) or not keys.issubset(
        _REQUIRED_SOURCE_FIELDS | _OPTIONAL_SOURCE_FIELDS
    ):
        raise CorpusFetchError("source fields do not match the pinned manifest contract")

    source_id = _non_empty_string(raw.get("source_id"), "source_id")
    if _SAFE_SOURCE_ID.fullmatch(source_id) is None:
        raise CorpusFetchError("source_id is invalid")
    url = _non_empty_string(raw.get("url"), "url")
    parsed_download = _validate_https_url(url, download=True)
    output = _non_empty_string(raw.get("output"), "output")
    relative = _validate_output_name(output)
    if Path(parsed_download.path).suffix.casefold() != relative.suffix.casefold():
        raise CorpusFetchError("download and output extensions must match")

    for field in (
        "source_index_url",
        "license_id",
        "license_url",
        "license_notice",
        "attribution",
        "intended_use",
    ):
        text = _non_empty_string(raw.get(field), field)
        if field.endswith("_url"):
            _validate_https_url(text, download=False)
    if "rights_url" in raw:
        _validate_https_url(_non_empty_string(raw.get("rights_url"), "rights_url"), download=False)

    max_bytes = raw.get("max_bytes")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_SOURCE_BYTES
    ):
        raise CorpusFetchError("max_bytes is outside the safe range")
    expected = raw.get("expected_sha256")
    if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
        raise CorpusFetchError("expected_sha256 must be a 64-character hexadecimal digest")
    if "paired_with" in raw:
        paired_with = _non_empty_string(raw.get("paired_with"), "paired_with")
        if _SAFE_SOURCE_ID.fullmatch(paired_with) is None:
            raise CorpusFetchError("paired_with is invalid")
    if (
        "selected_page_raster_derivation_only" in raw
        and raw.get("selected_page_raster_derivation_only") is not True
    ):
        raise CorpusFetchError("selected_page_raster_derivation_only may only be true")

    metadata = {str(key): value for key, value in raw.items()}
    return CorpusSource(
        source_id=source_id,
        url=url,
        output=output,
        max_bytes=max_bytes,
        expected_sha256=expected.casefold(),
        metadata=metadata,
    )


def load_manifest(config_path: Path = CONFIG_PATH) -> CorpusManifest:
    """Load and validate one version-controlled manifest without any network I/O."""
    try:
        payload_bytes = config_path.read_bytes()
        payload = yaml.safe_load(payload_bytes)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CorpusFetchError("development corpus manifest is unreadable") from exc
    if not isinstance(payload, Mapping) or frozenset(payload) != _MANIFEST_FIELDS:
        raise CorpusFetchError("manifest fields do not match schema 1.0")
    if payload.get("schema_version") != "1.0":
        raise CorpusFetchError("manifest schema version is unsupported")
    corpus_id = _non_empty_string(payload.get("corpus_id"), "corpus_id")
    if _SAFE_SOURCE_ID.fullmatch(corpus_id) is None:
        raise CorpusFetchError("corpus_id is invalid")
    _non_empty_string(payload.get("purpose"), "purpose")
    if (
        payload.get("customer_inputs_allowed") is not False
        or payload.get("production_evidence") is not False
    ):
        raise CorpusFetchError(
            "development corpus must reject customer inputs and production claims"
        )
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise CorpusFetchError("manifest sources must be a non-empty list")
    sources = tuple(_validate_source(raw) for raw in raw_sources)
    source_ids = [source.source_id for source in sources]
    outputs = [source.output.casefold() for source in sources]
    urls = [source.url for source in sources]
    if (
        len(set(source_ids)) != len(source_ids)
        or len(set(outputs)) != len(outputs)
        or len(set(urls)) != len(urls)
    ):
        raise CorpusFetchError("source ids, URLs, and output paths must be unique")
    available_ids = set(source_ids)
    if any(
        isinstance(source.metadata.get("paired_with"), str)
        and source.metadata["paired_with"] not in available_ids
        for source in sources
    ):
        raise CorpusFetchError("paired_with must reference another source in the manifest")
    metadata = {str(key): value for key, value in payload.items() if key != "sources"}
    return CorpusManifest(
        manifest_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        metadata=metadata,
        sources=sources,
    )


def _resolve_output_root(output_root: Path, allowed_parent: Path) -> Path:
    allowed_parent.mkdir(parents=True, exist_ok=True)
    allowed = allowed_parent.resolve(strict=True)
    candidate = output_root if output_root.is_absolute() else REPOSITORY_ROOT / output_root
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate == allowed or not resolved_candidate.is_relative_to(allowed):
        raise CorpusFetchError(
            "output root must be a dedicated directory under the ignored data tree"
        )
    resolved_candidate.mkdir(parents=True, exist_ok=True)
    root = resolved_candidate.resolve(strict=True)
    if root == allowed or not root.is_relative_to(allowed):
        raise CorpusFetchError("output root resolved outside the ignored data tree")
    return root


def _target_path(root: Path, source: CorpusSource) -> Path:
    relative = PurePosixPath(source.output)
    target = root.joinpath(*relative.parts)
    if not target.resolve(strict=False).is_relative_to(root):
        raise CorpusFetchError("source output resolved outside the output root")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.resolve(strict=True).is_relative_to(root):
        raise CorpusFetchError("source output parent resolved outside the output root")
    return target


def _validate_content(path: Path, extension: str) -> None:
    try:
        with path.open("rb") as stream:
            header = stream.read(8192)
    except OSError as exc:
        raise CorpusFetchError("downloaded content is unreadable") from exc
    normalized = extension.casefold()
    if normalized == ".png":
        valid = header.startswith(b"\x89PNG\r\n\x1a\n")
    elif normalized == ".pdf":
        valid = header.startswith(b"%PDF-")
    elif normalized == ".dxf":
        valid = (
            header.startswith(b"AutoCAD Binary DXF\r\n\x1a\x00")
            or _DXF_SECTION.search(header) is not None
        )
    else:
        valid = False
    if not valid:
        raise CorpusFetchError("downloaded content magic does not match its extension")


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(READ_CHUNK_BYTES), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise CorpusFetchError("corpus artifact is unreadable") from exc
    return digest.hexdigest(), size


def _fsync_parent(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return  # Windows does not expose portable directory fsync.
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
        _fsync_parent(target.parent)
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                Path(temporary_name).unlink()


def _download_source(
    source: CorpusSource,
    target: Path,
    opener: OpenUrl,
    *,
    locked_sha256: str | None = None,
    locked_size: int | None = None,
) -> tuple[str, int]:
    request = urllib.request.Request(
        source.url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "cad-harness-development-corpus/1.0",
        },
        method="GET",
    )
    temporary_name: str | None = None
    try:
        with opener(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            _validate_https_url(response.geturl(), download=True)
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    announced_size = int(content_length)
                except ValueError as exc:
                    raise CorpusFetchError("response Content-Length is invalid") from exc
                if announced_size < 0 or announced_size > source.max_bytes:
                    raise CorpusFetchError("response exceeds source max_bytes")
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
            ) as temporary:
                temporary_name = temporary.name
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = response.read(min(READ_CHUNK_BYTES, source.max_bytes - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > source.max_bytes:
                        raise CorpusFetchError("response exceeds source max_bytes")
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
        temporary_path = Path(temporary_name)
        _validate_content(temporary_path, Path(source.output).suffix)
        actual_hash = digest.hexdigest()
        if source.expected_sha256 is not None and actual_hash != source.expected_sha256:
            raise CorpusFetchError("download does not match expected_sha256")
        if locked_sha256 is not None and (
            actual_hash != locked_sha256.casefold() or size != locked_size
        ):
            raise CorpusFetchError("upstream content no longer matches the existing lock")
        os.replace(temporary_path, target)
        temporary_name = None
        _fsync_parent(target.parent)
        return actual_hash, size
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                Path(temporary_name).unlink()


def _lock_base(manifest: CorpusManifest) -> dict[str, Any]:
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "manifest_sha256": manifest.manifest_sha256,
        "manifest": manifest.metadata,
        "source_count": len(manifest.sources),
    }


def _read_lock(lock_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorpusFetchError("development corpus lock is missing or unreadable") from exc
    if not isinstance(payload, dict):
        raise CorpusFetchError("development corpus lock must be a JSON object")
    return payload


def _validate_lock_metadata(
    lock: Mapping[str, Any], manifest: CorpusManifest
) -> list[Mapping[str, Any]]:
    base = _lock_base(manifest)
    if any(lock.get(key) != value for key, value in base.items()):
        raise CorpusFetchError("development corpus lock does not exactly match the manifest")
    raw_entries = lock.get("sources")
    if not isinstance(raw_entries, list) or len(raw_entries) != len(manifest.sources):
        raise CorpusFetchError("development corpus lock source list is invalid")
    entries: list[Mapping[str, Any]] = []
    for source, raw_entry in zip(manifest.sources, raw_entries, strict=True):
        if not isinstance(raw_entry, Mapping) or raw_entry.get("source") != source.metadata:
            raise CorpusFetchError("development corpus lock source metadata is not exact")
        digest = raw_entry.get("sha256")
        size = raw_entry.get("size_bytes")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise CorpusFetchError("development corpus lock digest is invalid")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= source.max_bytes:
            raise CorpusFetchError("development corpus lock size is invalid")
        entries.append(raw_entry)
    if frozenset(lock) != frozenset({*base, "sources"}):
        raise CorpusFetchError("development corpus lock contains unexpected fields")
    return entries


def fetch_development_corpus(
    output_root: Path,
    *,
    config_path: Path = CONFIG_PATH,
    opener: OpenUrl = _default_opener,
    allowed_output_parent: Path = DEFAULT_ALLOWED_OUTPUT_PARENT,
) -> dict[str, Any]:
    """Fetch all pinned sources and atomically write an integrity lock."""
    manifest = load_manifest(config_path)
    root = _resolve_output_root(output_root, allowed_output_parent)
    lock_path = root / LOCK_FILENAME
    existing_entries: list[Mapping[str, Any]] | None = None
    if lock_path.exists():
        existing_entries = _validate_lock_metadata(_read_lock(lock_path), manifest)

    locked_sources: list[dict[str, Any]] = []
    for index, source in enumerate(manifest.sources):
        target = _target_path(root, source)
        entry = existing_entries[index] if existing_entries is not None else None
        digest, size = _download_source(
            source,
            target,
            opener,
            locked_sha256=str(entry["sha256"]) if entry is not None else None,
            locked_size=int(entry["size_bytes"]) if entry is not None else None,
        )
        locked_sources.append(
            {
                "source": source.metadata,
                "sha256": digest,
                "size_bytes": size,
            }
        )
    lock = {**_lock_base(manifest), "sources": locked_sources}
    lock_bytes = (json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    _atomic_write_bytes(lock_path, lock_bytes)
    return lock


def check_development_corpus(
    output_root: Path,
    *,
    config_path: Path = CONFIG_PATH,
    allowed_output_parent: Path = DEFAULT_ALLOWED_OUTPUT_PARENT,
) -> dict[str, Any]:
    """Verify the exact lock and local artifacts without constructing a network opener."""
    manifest = load_manifest(config_path)
    root = _resolve_output_root(output_root, allowed_output_parent)
    lock = _read_lock(root / LOCK_FILENAME)
    entries = _validate_lock_metadata(lock, manifest)
    for source, entry in zip(manifest.sources, entries, strict=True):
        target = _target_path(root, source)
        if not target.is_file():
            raise CorpusFetchError("locked corpus artifact is missing")
        _validate_content(target, Path(source.output).suffix)
        digest, size = _sha256_and_size(target)
        if digest != str(entry["sha256"]).casefold() or size != entry["size_bytes"]:
            raise CorpusFetchError("locked corpus artifact integrity check failed")
        if source.expected_sha256 is not None and digest != source.expected_sha256:
            raise CorpusFetchError("locked corpus artifact does not match expected_sha256")
    return lock


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="dedicated ignored directory under data/ (for example data/development-corpus)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the exact lock and local artifacts without network access",
    )
    args = parser.parse_args(argv)
    try:
        lock = (
            check_development_corpus(args.output_root)
            if args.check
            else fetch_development_corpus(args.output_root)
        )
    except (CorpusFetchError, OSError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "checked": args.check,
                "source_count": lock["source_count"],
                "production_evidence": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
