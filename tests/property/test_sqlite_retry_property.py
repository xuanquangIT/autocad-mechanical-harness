"""Property 3 for the bounded SQLite retry policy."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.exc import OperationalError

from cad_harness.domain.errors import ErrorCode, HarnessError
from cad_harness.persistence.retry import RetryPolicy


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


# Feature: cad-ai-production-roadmap, Property 3: Chính sách retry SQLite có biên xác định
@given(failures=st.integers(min_value=0, max_value=12))
@settings(max_examples=100)
def test_sqlite_retry_is_deterministically_bounded(failures: int) -> None:
    """**Validates: Requirements 1.7**"""
    clock = FakeClock()
    attempts = 0

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            raise OperationalError("write", {}, Exception("database is locked"))
        return "ok"

    policy = RetryPolicy(clock=clock.monotonic, sleep=clock.sleep)
    if failures < policy.max_attempts:
        assert policy.run(operation) == "ok"
        assert attempts == failures + 1
    else:
        try:
            policy.run(operation)
        except HarnessError as error:
            assert error.code is ErrorCode.INTERNAL_ERROR
            assert error.required_action
        else:
            raise AssertionError("an exhausted lock retry must raise HarnessError")

    assert attempts <= 5
    assert sum(clock.sleeps) <= 2.0
    assert clock.now <= 2.0
