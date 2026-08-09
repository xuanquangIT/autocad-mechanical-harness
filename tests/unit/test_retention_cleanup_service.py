from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import cad_harness.application.services.retention_cleanup_service as cleanup_module
from cad_harness.application.services.retention_cleanup_service import (
    RetentionCleanupFailureCode,
    UnsafeRetentionRootError,
    cleanup_filesystem_retention,
)
from cad_harness.security.retention import RetentionPolicy


def _file(path: Path, *, size: int, modified_at: datetime) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    timestamp = modified_at.timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


def _cleanup(
    preview: Path,
    checkpoint: Path,
    *,
    now: datetime,
    preview_policy: RetentionPolicy | None = None,
    checkpoint_policy: RetentionPolicy | None = None,
):
    return cleanup_filesystem_retention(
        preview_root=preview,
        preview_policy=preview_policy or RetentionPolicy(1, 10_000),
        checkpoint_root=checkpoint,
        checkpoint_policy=checkpoint_policy or RetentionPolicy(1, 10_000),
        now=now,
    )


def test_age_boundary_is_retained_and_older_file_is_deleted(tmp_path: Path) -> None:
    now = datetime(2030, 1, 2, tzinfo=UTC)
    preview = tmp_path / "previews"
    checkpoint = tmp_path / "checkpoints"
    boundary = _file(
        preview / "nested" / "boundary.dxf", size=1, modified_at=now - timedelta(days=1)
    )
    expired = _file(
        preview / "nested" / "expired.dxf",
        size=1,
        modified_at=now - timedelta(days=1, seconds=1),
    )

    result = _cleanup(preview, checkpoint, now=now)

    assert boundary.exists()
    assert not expired.exists()
    assert len(result.preview.selected) == 1
    assert result.preview.deleted == result.preview.selected
    assert result.preview.failures == ()


@pytest.mark.parametrize(
    ("quota", "expected_remaining"),
    [(10, {"old.bin", "new.bin"}), (9, {"old.bin", "new.bin"}), (8, {"new.bin"})],
)
def test_quota_exact_under_and_over_behavior(
    tmp_path: Path,
    quota: int,
    expected_remaining: set[str],
) -> None:
    now = datetime(2030, 1, 2, tzinfo=UTC)
    preview = tmp_path / "previews"
    checkpoint = tmp_path / "checkpoints"
    _file(preview / "old.bin", size=4, modified_at=now - timedelta(hours=2))
    _file(preview / "new.bin", size=5, modified_at=now - timedelta(hours=1))

    _cleanup(
        preview,
        checkpoint,
        now=now,
        preview_policy=RetentionPolicy(1, quota),
    )

    assert {item.name for item in preview.iterdir()} == expected_remaining


def test_preview_and_checkpoint_policies_are_independent(tmp_path: Path) -> None:
    now = datetime(2030, 1, 3, tzinfo=UTC)
    preview = tmp_path / "previews"
    checkpoint = tmp_path / "checkpoints"
    preview_file = _file(preview / "same-age.bin", size=2, modified_at=now - timedelta(days=2))
    checkpoint_file = _file(
        checkpoint / "same-age.bin", size=2, modified_at=now - timedelta(days=2)
    )

    result = _cleanup(
        preview,
        checkpoint,
        now=now,
        preview_policy=RetentionPolicy(1, 10),
        checkpoint_policy=RetentionPolicy(3, 10),
    )

    assert not preview_file.exists()
    assert checkpoint_file.exists()
    assert len(result.preview.deleted) == 1
    assert result.checkpoint.deleted == ()


def test_reparse_point_is_not_followed_or_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2030, 1, 3, tzinfo=UTC)
    preview = tmp_path / "previews"
    checkpoint = tmp_path / "checkpoints"
    reparse_like = _file(preview / "reparse-like.bin", size=1, modified_at=now - timedelta(days=5))
    reparse_inode = reparse_like.lstat().st_ino
    real_is_reparse = cleanup_module._is_reparse_point

    def simulated_reparse(file_stat: os.stat_result) -> bool:
        return file_stat.st_ino == reparse_inode or real_is_reparse(file_stat)

    monkeypatch.setattr(cleanup_module, "_is_reparse_point", simulated_reparse)

    result = _cleanup(preview, checkpoint, now=now)

    assert reparse_like.exists()
    assert result.preview.selected == ()
    assert result.preview.deleted == ()


def test_delete_failure_is_path_free_and_does_not_stop_other_deletions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2030, 1, 3, tzinfo=UTC)
    preview = tmp_path / "previews"
    checkpoint = tmp_path / "checkpoints"
    blocked = _file(preview / "blocked.bin", size=1, modified_at=now - timedelta(days=2))
    removable = _file(preview / "removable.bin", size=1, modified_at=now - timedelta(days=2))
    real_unlink = Path.unlink

    def selective_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.name == "blocked.bin":
            raise PermissionError("test denial")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", selective_unlink)

    result = _cleanup(preview, checkpoint, now=now)

    assert blocked.exists()
    assert not removable.exists()
    assert len(result.preview.deleted) == 1
    assert len(result.preview.failures) == 1
    assert result.preview.failures[0].code is RetentionCleanupFailureCode.DELETE_FAILED
    assert "blocked.bin" not in repr(result)
    assert str(preview) not in repr(result)


def test_file_changed_after_selection_is_not_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2030, 1, 3, tzinfo=UTC)
    preview = tmp_path / "previews"
    checkpoint = tmp_path / "checkpoints"
    changed = _file(preview / "changed.bin", size=1, modified_at=now - timedelta(days=2))
    real_select = cleanup_module.select_artifacts_for_deletion

    def select_then_change(artifacts, policy, *, now):
        selected = real_select(artifacts, policy, now=now)
        if changed.exists():
            changed.write_bytes(b"replacement")
        return selected

    monkeypatch.setattr(cleanup_module, "select_artifacts_for_deletion", select_then_change)

    result = _cleanup(preview, checkpoint, now=now)

    assert changed.read_bytes() == b"replacement"
    assert result.preview.deleted == ()
    assert len(result.preview.failures) == 1
    assert result.preview.failures[0].code is RetentionCleanupFailureCode.TARGET_CHANGED


def test_filesystem_anchor_current_directory_and_home_are_rejected(tmp_path: Path) -> None:
    now = datetime(2030, 1, 3, tzinfo=UTC)
    safe = tmp_path / "safe"
    for dangerous in (Path(tmp_path.anchor), Path.cwd(), Path.home()):
        with pytest.raises(UnsafeRetentionRootError, match="dangerously broad"):
            cleanup_filesystem_retention(
                preview_root=dangerous,
                preview_policy=RetentionPolicy(1, 1),
                checkpoint_root=safe,
                checkpoint_policy=RetentionPolicy(1, 1),
                now=now,
            )


def test_both_roots_are_validated_before_any_deletion(tmp_path: Path) -> None:
    now = datetime(2030, 1, 3, tzinfo=UTC)
    preview = tmp_path / "previews"
    expired = _file(preview / "expired.bin", size=1, modified_at=now - timedelta(days=2))

    with pytest.raises(UnsafeRetentionRootError, match="dangerously broad"):
        cleanup_filesystem_retention(
            preview_root=preview,
            preview_policy=RetentionPolicy(1, 1),
            checkpoint_root=Path(tmp_path.anchor),
            checkpoint_policy=RetentionPolicy(1, 1),
            now=now,
        )

    assert expired.exists()


@pytest.mark.parametrize("relation", ("same", "checkpoint_child", "preview_child"))
def test_overlapping_roots_are_rejected_before_any_deletion(tmp_path: Path, relation: str) -> None:
    now = datetime(2030, 1, 3, tzinfo=UTC)
    preview = tmp_path / "previews"
    checkpoint = tmp_path / "checkpoints"
    if relation == "same":
        checkpoint = preview
    elif relation == "checkpoint_child":
        checkpoint = preview / "checkpoints"
    else:
        preview = checkpoint / "previews"
    expired = _file(preview / "expired.bin", size=1, modified_at=now - timedelta(days=2))

    with pytest.raises(UnsafeRetentionRootError, match="non-overlapping"):
        _cleanup(preview, checkpoint, now=now)

    assert expired.exists()
