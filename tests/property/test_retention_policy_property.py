"""Property 77: retention selects exactly the artifacts that violate policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.security.retention import (
    RetentionArtifact,
    RetentionPolicy,
    select_artifacts_for_deletion,
)


@st.composite
def retention_cases(
    draw: st.DrawFn,
) -> tuple[datetime, RetentionPolicy, tuple[RetentionArtifact, ...]]:
    now = draw(
        st.datetimes(
            min_value=datetime(2020, 1, 1),
            max_value=datetime(2040, 12, 31),
            timezones=st.just(UTC),
        )
    )
    max_age_days = draw(st.integers(min_value=1, max_value=365))
    specs = draw(
        st.lists(
            st.tuples(
                st.integers(
                    min_value=-86_400_000_000,
                    max_value=(max_age_days + 5) * 86_400_000_000,
                ),
                st.integers(min_value=0, max_value=50_000),
            ),
            max_size=30,
        )
    )

    artifacts = [
        RetentionArtifact(
            id=f"generated-{index:02d}",
            artifact_ref=f"memory://retention/{index:02d}",
            created_at=now - timedelta(microseconds=age_microseconds),
            byte_size=byte_size,
        )
        for index, (age_microseconds, byte_size) in enumerate(specs)
    ]
    artifacts.extend(
        (
            RetentionArtifact(
                id="exact-age-boundary",
                artifact_ref="memory://retention/exact-boundary",
                created_at=now - timedelta(days=max_age_days),
                byte_size=0,
            ),
            RetentionArtifact(
                id="just-expired",
                artifact_ref="memory://retention/just-expired",
                created_at=now - timedelta(days=max_age_days, microseconds=1),
                byte_size=0,
            ),
            RetentionArtifact(
                id="quota-anchor",
                artifact_ref="memory://retention/quota-anchor",
                created_at=now,
                byte_size=1,
            ),
        )
    )

    age_limit = timedelta(days=max_age_days)
    eligible_bytes = sum(
        artifact.byte_size for artifact in artifacts if now - artifact.created_at <= age_limit
    )
    max_total_bytes = draw(
        st.one_of(
            st.integers(min_value=0, max_value=eligible_bytes - 1),
            st.just(eligible_bytes),
            st.integers(min_value=eligible_bytes + 1, max_value=eligible_bytes + 10_000),
        )
    )
    return now, RetentionPolicy(max_age_days, max_total_bytes), tuple(artifacts)


def _reference_selection(
    artifacts: tuple[RetentionArtifact, ...],
    policy: RetentionPolicy,
    now: datetime,
) -> tuple[RetentionArtifact, ...]:
    order = lambda artifact: (  # noqa: E731 - compact independent oracle key
        artifact.created_at,
        artifact.id,
        artifact.artifact_ref,
        artifact.byte_size,
    )
    age_limit = timedelta(days=policy.max_age_days)
    expired = [artifact for artifact in artifacts if now - artifact.created_at > age_limit]
    survivors = sorted(
        (artifact for artifact in artifacts if now - artifact.created_at <= age_limit),
        key=order,
    )
    survivor_bytes = sum(artifact.byte_size for artifact in survivors)
    quota_evictions: list[RetentionArtifact] = []
    while survivors and survivor_bytes > policy.max_total_bytes:
        oldest = survivors.pop(0)
        quota_evictions.append(oldest)
        survivor_bytes -= oldest.byte_size
    return tuple(sorted((*expired, *quota_evictions), key=order))


# Feature: cad-ai-production-roadmap, Property 77: retention selects correct deletions
@given(case=retention_cases())
@settings(max_examples=150, deadline=None)
def test_retention_policy_selects_exact_expiry_and_quota_violations(
    case: tuple[datetime, RetentionPolicy, tuple[RetentionArtifact, ...]],
) -> None:
    """**Validates: Requirements 27.10, 27.12**"""
    now, policy, artifacts = case

    selected = select_artifacts_for_deletion(artifacts, policy, now=now)

    assert selected == _reference_selection(artifacts, policy, now)
    assert select_artifacts_for_deletion(tuple(reversed(artifacts)), policy, now=now) == selected
    assert tuple(selected) == tuple(
        sorted(
            selected,
            key=lambda item: (
                item.created_at,
                item.id,
                item.artifact_ref,
                item.byte_size,
            ),
        )
    )

    boundary = next(item for item in artifacts if item.id == "exact-age-boundary")
    expired = next(item for item in artifacts if item.id == "just-expired")
    boundary_only_policy = RetentionPolicy(policy.max_age_days, max_total_bytes=0)
    assert select_artifacts_for_deletion((boundary,), boundary_only_policy, now=now) == ()
    assert select_artifacts_for_deletion((expired,), boundary_only_policy, now=now) == (expired,)

    retained = tuple(item for item in artifacts if item not in selected)
    assert sum(item.byte_size for item in retained) <= policy.max_total_bytes
