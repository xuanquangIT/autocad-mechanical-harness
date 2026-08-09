"""Pure retention-policy decisions for stored artifacts.

This module only identifies artifacts that should be removed.  The caller owns any
filesystem or object-store mutation needed to carry out that decision.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class RetentionArtifact:
    """Immutable metadata needed to make a retention decision."""

    id: str
    artifact_ref: str
    created_at: datetime
    byte_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.byte_size, int) or isinstance(self.byte_size, bool):
            raise TypeError("byte_size must be an integer")
        if self.byte_size < 0:
            raise ValueError("byte_size must be nonnegative")
        object.__setattr__(
            self,
            "created_at",
            _as_utc(self.created_at, field_name="created_at"),
        )


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Age and aggregate-size limits for one artifact collection."""

    max_age_days: int
    max_total_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.max_age_days, int) or isinstance(self.max_age_days, bool):
            raise TypeError("max_age_days must be an integer")
        if self.max_age_days <= 0:
            raise ValueError("max_age_days must be positive")
        if not isinstance(self.max_total_bytes, int) or isinstance(self.max_total_bytes, bool):
            raise TypeError("max_total_bytes must be an integer")
        if self.max_total_bytes < 0:
            raise ValueError("max_total_bytes must be nonnegative")


def _artifact_order(artifact: RetentionArtifact) -> tuple[datetime, str, str, int]:
    return (
        artifact.created_at,
        artifact.id,
        artifact.artifact_ref,
        artifact.byte_size,
    )


def _is_expired(artifact: RetentionArtifact, now: datetime, max_age_days: int) -> bool:
    age = now - artifact.created_at
    return age.days > max_age_days or (
        age.days == max_age_days and (age.seconds > 0 or age.microseconds > 0)
    )


def select_artifacts_for_deletion(
    artifacts: Iterable[RetentionArtifact],
    policy: RetentionPolicy,
    *,
    now: datetime,
) -> tuple[RetentionArtifact, ...]:
    """Select expired artifacts, then oldest survivors until the quota is met.

    An artifact exactly ``max_age_days`` old remains age-eligible.  Ties are resolved
    without relying on input order, so equivalent collections produce the same tuple.
    """

    now_utc = _as_utc(now, field_name="now")
    ordered = tuple(sorted(artifacts, key=_artifact_order))
    expired: list[RetentionArtifact] = []
    remaining: list[RetentionArtifact] = []
    for artifact in ordered:
        target = expired if _is_expired(artifact, now_utc, policy.max_age_days) else remaining
        target.append(artifact)

    remaining_bytes = sum(artifact.byte_size for artifact in remaining)
    quota_evictions: list[RetentionArtifact] = []
    for artifact in remaining:
        if remaining_bytes <= policy.max_total_bytes:
            break
        quota_evictions.append(artifact)
        remaining_bytes -= artifact.byte_size

    return tuple(sorted((*expired, *quota_evictions), key=_artifact_order))


__all__ = [
    "RetentionArtifact",
    "RetentionPolicy",
    "select_artifacts_for_deletion",
]
