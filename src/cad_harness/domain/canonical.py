"""Canonical JSON and plan hashing (architecture section 13.3).

The plan hash is the contract between preview, approval and commit. It must be
stable across processes and machines, so the canonicalization rules are explicit:

* UTF-8, sorted keys, no insignificant whitespace.
* Floats normalized to a fixed precision; ``-0.0`` collapsed to ``0.0``.
* Arrays keep their semantic order.
* Volatile fields (timestamps, trace ids, the hash itself) are excluded by the caller.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

#: Decimal places retained when normalizing floats. Tighter than any tolerance
#: profile so hashing never hides a geometrically meaningful difference.
FLOAT_PRECISION = 9

#: Fields never included in a plan hash because they do not change the plan.
VOLATILE_FIELDS: frozenset[str] = frozenset(
    {"plan_hash", "created_at", "updated_at", "request_id", "trace_id", "audit_event_id"}
)

#: Instance identifiers, also excluded. The hash answers "is this the same *change*",
#: so the same spec compiled under a different job must produce the same hash.
PLAN_IDENTITY_FIELDS: frozenset[str] = frozenset({"plan_id", "job_id"})


def normalize(value: Any) -> Any:
    """Recursively normalize a JSON-compatible value for canonical serialization."""
    if isinstance(value, bool) or value is None or isinstance(value, str | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Non-finite float cannot be canonicalized: {value!r}")
        rounded = round(value, FLOAT_PRECISION)
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list | tuple):
        return [normalize(v) for v in value]
    raise TypeError(f"Unsupported type for canonical JSON: {type(value).__name__}")


def strip_volatile(value: Any, fields: frozenset[str] = VOLATILE_FIELDS) -> Any:
    """Drop volatile keys at every depth so the hash covers plan semantics only."""
    if isinstance(value, dict):
        return {k: strip_volatile(v, fields) for k, v in value.items() if k not in fields}
    if isinstance(value, list | tuple):
        return [strip_volatile(v, fields) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """Serialize ``value`` deterministically."""
    return json.dumps(
        normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_of(value: Any) -> str:
    """Hash any JSON-compatible value, prefixed for self-description."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def compute_plan_hash(plan: dict[str, Any]) -> str:
    """Hash an operation plan.

    Excludes volatile fields and instance identifiers, so the same spec compiled twice
    hashes identically. Document identity, expected revision, profile ref, operations
    and expectations are all included, so any of them changing invalidates approvals.
    """
    return sha256_of(strip_volatile(plan, VOLATILE_FIELDS | PLAN_IDENTITY_FIELDS))


def hash_prefix(hash_value: str, length: int = 12) -> str:
    """Short form for logs. Never log full hashes alongside document identity."""
    _, _, digest = hash_value.partition(":")
    return digest[:length]
