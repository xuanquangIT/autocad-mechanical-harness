"""Prefixed, sortable-enough identifiers for jobs, specs, plans and audit events.

Identifiers are opaque to clients. They are time-prefixed so that ordering in logs
roughly matches creation order without leaking wall-clock precision.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum


class IdPrefix(StrEnum):
    JOB = "job"
    SPEC = "spec"
    PLAN = "plan"
    DOCUMENT = "doc"
    REQUEST = "req"
    APPROVAL = "approval"
    VALIDATION = "validation"
    EXECUTION = "exec"
    CHECKPOINT = "checkpoint"
    AUDIT_EVENT = "evt"
    PREVIEW = "preview"


_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(number: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        number, remainder = divmod(number, 32)
        chars.append(_BASE32[remainder])
    return "".join(reversed(chars))


def new_id(prefix: IdPrefix) -> str:
    """Return an identifier such as ``job_01J8ZH...`` (ULID-shaped)."""
    timestamp_ms = int(time.time() * 1000)
    randomness = uuid.uuid4().int & ((1 << 80) - 1)
    return f"{prefix.value}_{_encode(timestamp_ms, 10)}{_encode(randomness, 16)}"
