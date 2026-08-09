"""Redaction for logs, audit payloads and tool responses.

Drawing paths routinely encode customer and project names. Those leave the workstation
only as pseudonyms unless an operator explicitly turns redaction off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cad_harness.domain.canonical import sha256_of

#: Keys whose values are replaced wholesale, at any depth.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "approval_token",
        "approval_secret",
        "secret",
        "password",
        "token",
        "prompt",
        "raw_prompt",
        "api_key",
        # Geometry volume must stay bounded independently of entity count. Counts and
        # aggregate measurements remain observable; coordinate collections do not.
        "geometry",
        "geometries",
        "coordinates",
        "vertices",
        "points",
    }
)

#: Keys holding filesystem paths, replaced with a stable pseudonym.
PATH_KEYS: frozenset[str] = frozenset(
    {
        "path",
        "full_path",
        "target_path",
        "file_path",
        "artifact_ref",
        "document_path",
        "display_name",
        "filename",
    }
)

REDACTED = "[redacted]"


def redact_path(path: str | Path) -> str:
    """Keep the file extension, pseudonymize everything else."""
    candidate = Path(str(path))
    digest = sha256_of(str(candidate).strip().lower()).removeprefix("sha256:")[:16]
    return f"path:{digest}{candidate.suffix}"


def redact_payload(payload: Any, *, redact_paths: bool = True) -> Any:
    """Recursively redact a payload for logging or audit storage."""
    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered in SENSITIVE_KEYS:
                result[key] = REDACTED
            elif redact_paths and lowered in PATH_KEYS and isinstance(value, str | Path):
                result[key] = redact_path(value)
            else:
                result[key] = redact_payload(value, redact_paths=redact_paths)
        return result
    if isinstance(payload, list | tuple):
        return [redact_payload(item, redact_paths=redact_paths) for item in payload]
    return payload
