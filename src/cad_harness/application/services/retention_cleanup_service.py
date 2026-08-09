"""Production-safe filesystem cleanup for preview and checkpoint retention."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from cad_harness.security.retention import (
    RetentionArtifact,
    RetentionPolicy,
    select_artifacts_for_deletion,
)

_REPARSE_POINT = 0x400


class RetentionCleanupFailureCode(StrEnum):
    """Finite, path-free reasons why an artifact could not be removed."""

    SCAN_FAILED = "scan_failed"
    TARGET_MISSING = "target_missing"
    TARGET_CHANGED = "target_changed"
    OUTSIDE_ROOT = "outside_root"
    DELETE_FAILED = "delete_failed"


class UnsafeRetentionRootError(ValueError):
    """Raised before scanning a root that is too broad for automatic deletion."""


@dataclass(frozen=True, slots=True)
class RetentionCleanupFailure:
    collection: str
    artifact_id: str
    code: RetentionCleanupFailureCode


@dataclass(frozen=True, slots=True)
class RetentionCollectionResult:
    collection: str
    selected: tuple[str, ...]
    deleted: tuple[str, ...]
    failures: tuple[RetentionCleanupFailure, ...]


@dataclass(frozen=True, slots=True)
class RetentionCleanupResult:
    preview: RetentionCollectionResult
    checkpoint: RetentionCollectionResult


@dataclass(frozen=True, slots=True)
class _ScannedTarget:
    path: Path
    device: int
    inode: int
    byte_size: int
    modified_ns: int


def _opaque_id(collection: str, relative_ref: str) -> str:
    payload = f"{collection}\0{relative_ref}".encode()
    return hashlib.sha256(payload).hexdigest()


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _validated_root(root: Path) -> Path:
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        root_stat = None
    except OSError as exc:
        raise UnsafeRetentionRootError("retention root cannot be safely inspected") from exc
    if root_stat is not None and (
        stat.S_ISLNK(root_stat.st_mode)
        or _is_reparse_point(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        raise UnsafeRetentionRootError("retention root must be a regular directory")
    resolved = root.resolve(strict=False)
    anchor = Path(resolved.anchor).resolve(strict=False)
    forbidden = {anchor, Path.cwd().resolve(strict=False), Path.home().resolve(strict=False)}
    if resolved in forbidden or len(resolved.parts) <= 2:
        raise UnsafeRetentionRootError("retention root is dangerously broad")
    return resolved


def _scan_regular_files(
    *,
    collection: str,
    root: Path,
) -> tuple[
    tuple[RetentionArtifact, ...],
    dict[str, _ScannedTarget],
    tuple[RetentionCleanupFailure, ...],
]:
    artifacts: list[RetentionArtifact] = []
    paths: dict[str, _ScannedTarget] = {}
    failures: list[RetentionCleanupFailure] = []
    if not root.exists():
        return (), {}, ()

    pending: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    while pending:
        directory, relative_parts = pending.pop()
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(scanner, key=lambda item: (item.name.casefold(), item.name))
        except OSError:
            failures.append(
                RetentionCleanupFailure(
                    collection=collection,
                    artifact_id=_opaque_id(collection, "/".join(relative_parts) or "."),
                    code=RetentionCleanupFailureCode.SCAN_FAILED,
                )
            )
            continue

        child_directories: list[tuple[Path, tuple[str, ...]]] = []
        for entry in entries:
            child_parts = (*relative_parts, entry.name)
            relative_ref = "/".join(child_parts)
            entry_path = Path(entry.path)
            try:
                # Path.lstat supplies stable file identity on Windows, where
                # DirEntry.stat may report zero device/inode values.
                entry_stat = entry_path.lstat()
            except OSError:
                failures.append(
                    RetentionCleanupFailure(
                        collection=collection,
                        artifact_id=_opaque_id(collection, relative_ref),
                        code=RetentionCleanupFailureCode.SCAN_FAILED,
                    )
                )
                continue
            if entry.is_symlink() or _is_reparse_point(entry_stat):
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                child_directories.append((entry_path, child_parts))
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                continue
            artifact_id = _opaque_id(collection, relative_ref)
            artifacts.append(
                RetentionArtifact(
                    id=artifact_id,
                    artifact_ref=artifact_id,
                    created_at=datetime.fromtimestamp(entry_stat.st_mtime, UTC),
                    byte_size=entry_stat.st_size,
                )
            )
            paths[artifact_id] = _ScannedTarget(
                path=entry_path,
                device=entry_stat.st_dev,
                inode=entry_stat.st_ino,
                byte_size=entry_stat.st_size,
                modified_ns=entry_stat.st_mtime_ns,
            )
        pending.extend(reversed(child_directories))

    return tuple(artifacts), paths, tuple(failures)


def _delete_selected(
    *,
    collection: str,
    root: Path,
    selected: tuple[RetentionArtifact, ...],
    paths: dict[str, _ScannedTarget],
) -> tuple[tuple[str, ...], tuple[RetentionCleanupFailure, ...]]:
    deleted: list[str] = []
    failures: list[RetentionCleanupFailure] = []
    for artifact in selected:
        scanned_target = paths[artifact.id]
        target = scanned_target.path
        try:
            target_stat = target.lstat()
        except FileNotFoundError:
            failures.append(
                RetentionCleanupFailure(
                    collection, artifact.id, RetentionCleanupFailureCode.TARGET_MISSING
                )
            )
            continue
        except OSError:
            failures.append(
                RetentionCleanupFailure(
                    collection, artifact.id, RetentionCleanupFailureCode.TARGET_CHANGED
                )
            )
            continue
        if (
            target.is_symlink()
            or _is_reparse_point(target_stat)
            or not stat.S_ISREG(target_stat.st_mode)
            or target_stat.st_dev != scanned_target.device
            or target_stat.st_ino != scanned_target.inode
            or target_stat.st_size != scanned_target.byte_size
            or target_stat.st_mtime_ns != scanned_target.modified_ns
        ):
            failures.append(
                RetentionCleanupFailure(
                    collection, artifact.id, RetentionCleanupFailureCode.TARGET_CHANGED
                )
            )
            continue
        try:
            resolved_target = target.resolve(strict=True)
        except OSError:
            failures.append(
                RetentionCleanupFailure(
                    collection, artifact.id, RetentionCleanupFailureCode.TARGET_CHANGED
                )
            )
            continue
        if resolved_target == root or not resolved_target.is_relative_to(root):
            failures.append(
                RetentionCleanupFailure(
                    collection, artifact.id, RetentionCleanupFailureCode.OUTSIDE_ROOT
                )
            )
            continue
        try:
            target.unlink(missing_ok=False)
        except FileNotFoundError:
            failures.append(
                RetentionCleanupFailure(
                    collection, artifact.id, RetentionCleanupFailureCode.TARGET_MISSING
                )
            )
        except OSError:
            failures.append(
                RetentionCleanupFailure(
                    collection, artifact.id, RetentionCleanupFailureCode.DELETE_FAILED
                )
            )
        else:
            deleted.append(artifact.id)
    return tuple(deleted), tuple(failures)


def _cleanup_collection(
    *,
    collection: str,
    root: Path,
    policy: RetentionPolicy,
    now: datetime,
) -> RetentionCollectionResult:
    resolved_root = _validated_root(root)
    artifacts, paths, scan_failures = _scan_regular_files(
        collection=collection,
        root=resolved_root,
    )
    selected = select_artifacts_for_deletion(artifacts, policy, now=now)
    deleted, delete_failures = _delete_selected(
        collection=collection,
        root=resolved_root,
        selected=selected,
        paths=paths,
    )
    return RetentionCollectionResult(
        collection=collection,
        selected=tuple(artifact.id for artifact in selected),
        deleted=deleted,
        failures=(*scan_failures, *delete_failures),
    )


def cleanup_filesystem_retention(
    *,
    preview_root: Path,
    preview_policy: RetentionPolicy,
    checkpoint_root: Path,
    checkpoint_policy: RetentionPolicy,
    now: datetime,
) -> RetentionCleanupResult:
    """Apply independent retention policies to two explicitly allowlisted roots."""

    # Validate every allowlisted root before the first mutation.  A bad checkpoint
    # root must not leave preview cleanup half-applied (or vice versa).
    resolved_preview_root = _validated_root(preview_root)
    resolved_checkpoint_root = _validated_root(checkpoint_root)
    if (
        resolved_preview_root == resolved_checkpoint_root
        or resolved_preview_root.is_relative_to(resolved_checkpoint_root)
        or resolved_checkpoint_root.is_relative_to(resolved_preview_root)
    ):
        raise UnsafeRetentionRootError(
            "preview and checkpoint retention roots must be separate, non-overlapping directories"
        )
    return RetentionCleanupResult(
        preview=_cleanup_collection(
            collection="preview",
            root=resolved_preview_root,
            policy=preview_policy,
            now=now,
        ),
        checkpoint=_cleanup_collection(
            collection="checkpoint",
            root=resolved_checkpoint_root,
            policy=checkpoint_policy,
            now=now,
        ),
    )


__all__ = [
    "RetentionCleanupFailure",
    "RetentionCleanupFailureCode",
    "RetentionCleanupResult",
    "RetentionCollectionResult",
    "UnsafeRetentionRootError",
    "cleanup_filesystem_retention",
]
