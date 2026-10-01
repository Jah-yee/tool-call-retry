"""Retry policy: exponential backoff, full jitter, retryable classification, budgets."""

import asyncio
import random

import pytest

from tool_call_retry.errors import (
    HTTPStatusError,
    NonRetryableError,
    RetryableError,
    ToolCallError,
)
from tool_call_retry.policy import RetryExhausted, RetryPolicy


def test_default_policy_values_match_issue_spec():
    policy = RetryPolicy()
    assert policy.max_attempts == 3
    assert policy.base_delay == pytest.approx(0.1)
    assert policy.max_delay == pytest.approx(30.0)
    assert policy.multiplier == pytest.approx(2.0)
    assert policy.jitter == "full"
    assert policy.max_total_delay == pytest.approx(60.0)


def test_exponential_backoff_without_jitter():
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0, jitter="none", max_delay=100.0)
    assert [policy.delay_for(attempt) for attempt in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]


def test_backoff_is_capped_by_max_delay():
    policy = RetryPolicy(base_delay=1.0, multiplier=10.0, jitter="none", max_delay=5.0)
    assert policy.delay_for(1) == pytest.approx(1.0)
    assert policy.delay_for(2) == pytest.approx(5.0)
    assert policy.delay_for(3) == pytest.approx(5.0)


def test_full_jitter_stays_within_zero_to_cap_window():
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0, jitter="full", max_delay=10.0)
    rng = random.Random(1234)
    samples = [policy.delay_for(2, rng=rng) for _ in range(500)]
    assert all(0.0 <= d <= 2.0 for d in samples)
    assert len(set(samples)) > 100, "full jitter must randomize the delay"
    assert 0.5 < sum(samples) / len(samples) < 1.5


def test_sequential_jitter_is_centred_on_the_capped_delay():
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0, jitter="sequential", max_delay=10.0)
    rng = random.Random(7)
    samples = [policy.delay_for(1, rng=rng) for _ in range(200)]
    assert all(0.5 <= d <= 1.0 for d in samples)
    assert 0.6 < sum(samples) / len(samples) < 0.9


def test_invalid_jitter_strategy_is_rejected():
    with pytest.raises(ValueError, match="jitter"):
        RetryPolicy(jitter="gaussian")


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("socket timeout"),
        ConnectionError("connection reset by peer"),
        ConnectionResetError("reset"),
        HTTPStatusError(503, "Service Unavailable"),
        HTTPStatusError(429, "Too Many Requests"),
        HTTPStatusError(500, "Internal Server Error"),
        ToolCallError("transient upstream failure", retryable=True),
    ],
)
def test_transient_errors_are_retryable(exc):
    assert RetryPolicy().is_retryable(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        HTTPStatusError(400, "Bad Request"),
        HTTPStatusError(401, "Unauthorized"),
        HTTPStatusError(403, "Forbidden"),
        HTTPStatusError(404, "Not Found"),
        HTTPStatusError(422, "Unprocessable Entity"),
        ToolCallError("bad arguments", retryable=False),
        NonRetryableError("unsupported tool"),
        ValueError("invalid payload"),
        KeyError("order_id"),
        TypeError("wrong type"),
    ],
)
def test_client_errors_are_not_retryable(exc):
    assert RetryPolicy().is_retryable(exc) is False


def test_retryable_error_subclass_is_retryable_and_non_retryable_is_not():
    assert RetryPolicy().is_retryable(RetryableError("later")) is True
    assert RetryPolicy().is_retryable(NonRetryableError("never")) is False


def test_is_retryable_honours_attempt_budget():
    policy = RetryPolicy(max_attempts=2)
    assert policy.is_retryable(TimeoutError(), attempt=1) is True
    assert policy.is_retryable(TimeoutError(), attempt=2) is False


def test_custom_retryable_types_override_defaults():
    policy = RetryPolicy(retryable_exceptions=(KeyError,))
    assert policy.is_retryable(KeyError("k")) is True
    assert policy.is_retryable(TimeoutError()) is False


def test_run_returns_first_success_without_sleeping():
    slept = []
    policy = RetryPolicy(base_delay=0.1)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("slow")
        return "ok"

    assert policy.run(flaky, sleep=slept.append) == "ok"
    assert len(calls) == 3
    assert slept == [pytest.approx(0.1, abs=0.1), pytest.approx(0.2, abs=0.2)]


def test_run_raises_retry_exhausted_after_max_attempts():
    policy = RetryPolicy(max_attempts=2, base_delay=0.01, jitter="none")
    attempts = []

    def always_timeout():
        attempts.append(1)
        raise TimeoutError("still down")

    with pytest.raises(RetryExhausted) as excinfo:
        policy.run(always_timeout, sleep=lambda _d: None)
    assert len(attempts) == 2
    assert excinfo.value.attempts == 2
    assert excinfo.value.last_error.args == ("still down",)
    assert [a.attempt for a in excinfo.value.attempt_history] == [1, 2]


def test_run_does_not_retry_non_retryable_errors():
    calls = []

    def bad_request():
        calls.append(1)
        raise NonRetryableError("unsupported tool")

    policy = RetryPolicy()
    with pytest.raises(NonRetryableError):
        policy.run(bad_request, sleep=lambda _d: None)
    assert len(calls) == 1


def test_run_stops_once_total_delay_budget_is_exhausted():
    policy = RetryPolicy(
        max_attempts=10,
        base_delay=1.0,
        max_delay=1.0,
        jitter="none",
        max_total_delay=1.0,
    )
    assert policy.max_total_delay == pytest.approx(1.0)
    slept = []

    def always_timeout():
        raise TimeoutError("down")

    with pytest.raises(RetryExhausted):
        policy.run(always_timeout, sleep=slept.append)
    assert len(slept) == 1


def test_on_attempt_callback_receives_history():
    seen = []
    policy = RetryPolicy(max_attempts=3, base_delay=0.0, jitter="none")
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise TimeoutError("again")
        return "done"

    policy.run(flaky, sleep=lambda _d: None, on_attempt=seen.append)
    assert [a.attempt for a in seen] == [1, 2]
    assert seen[-1].succeeded is True


def test_arun_retries_async_callables():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("async slow")
        return "async-ok"

    async def main():
        async def no_sleep(_delay):
            return None

        return await RetryPolicy(base_delay=0.0).arun(flaky, sleep=no_sleep)

    assert asyncio.run(main()) == "async-ok"
    assert len(calls) == 3


def test_arun_raises_retry_exhausted():
    async def always_timeout():
        raise TimeoutError("nope")

    async def main():
        async def no_sleep(_delay):
            return None

        policy = RetryPolicy(max_attempts=2, base_delay=0.0)
        return await policy.arun(always_timeout, sleep=no_sleep)

    with pytest.raises(RetryExhausted):
        asyncio.run(main())


def test_policy_round_trips_through_dict_and_yaml(tmp_path):
    policy = RetryPolicy(max_attempts=5, base_delay=0.25, multiplier=3.0, max_delay=9.0)
    restored = RetryPolicy.from_dict(policy.to_dict())
    assert restored == policy

    path = tmp_path / "retry.yml"
    path.write_text(
        "max_attempts: 4\nbase_delay: 0.5\nmax_delay: 4.0\nmultiplier: 2.0\njitter: sequential\n"
    )
    from_yaml = RetryPolicy.from_yaml(path)
    assert from_yaml.max_attempts == 4
    assert from_yaml.jitter == "sequential"
    assert from_yaml.base_delay == pytest.approx(0.5)


def test_retry_exhausted_carries_a_readable_summary():
    policy = RetryPolicy(max_attempts=2, base_delay=0.0)
    with pytest.raises(RetryExhausted) as excinfo:
        def always_timeout():
            raise TimeoutError("gateway timeout")

        policy.run(always_timeout, sleep=lambda _d: None)
    summary = excinfo.value.summary()
    assert "2 attempts" in summary
    assert "TimeoutError" in summary
    assert "gateway timeout" in summary
