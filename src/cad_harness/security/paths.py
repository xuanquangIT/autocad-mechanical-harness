"""Path allowlisting for previews, checkpoints and exports.

Export is a write to the user's filesystem, so the target is checked against an
allowlist and overwrite is refused unless explicitly requested.
"""

from __future__ import annotations

from pathlib import Path

from cad_harness.domain.errors import ExportPathNotAllowedError


def _resolve(path: Path) -> Path:
    """Resolve symlinks and ``..`` so traversal cannot escape the allowlist."""
    return path.expanduser().resolve(strict=False)


def is_path_allowed(target: Path, allowlist: tuple[Path, ...]) -> bool:
    resolved = _resolve(target)
    for allowed in allowlist:
        allowed_resolved = _resolve(allowed)
        if resolved == allowed_resolved or allowed_resolved in resolved.parents:
            return True
    return False


def ensure_path_allowed(
    target: Path,
    allowlist: tuple[Path, ...],
    *,
    allow_arbitrary: bool = False,
    overwrite: bool = False,
) -> Path:
    """Validate a write target and return its resolved path.

    Raises:
        ExportPathNotAllowedError: outside the allowlist, or the file exists and
            ``overwrite`` was not requested.
    """
    resolved = _resolve(target)

    if not allow_arbitrary and not is_path_allowed(resolved, allowlist):
        raise ExportPathNotAllowedError(
            "Target path is outside the configured allowlist",
            required_action="Choose a path inside an allowlisted directory",
            details={"allowlist": [str(p) for p in allowlist]},
        )

    if resolved.exists() and not overwrite:
        raise ExportPathNotAllowedError(
            "Target file already exists and overwrite was not requested",
            required_action="Pass overwrite=true or choose a different filename",
            details={"filename": resolved.name},
        )

    return resolved
