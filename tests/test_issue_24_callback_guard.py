"""Regression tests for issue #24: on_attempt/sleeper callback guard.

See https://github.com/yunaremaia/tool-call-retry/issues/24
These tests verify that an exploding on_attempt observer never replaces
the real error that the caller must see.

The guard inside _notify catches Exception (not BaseException), so
KeyboardInterrupt / SystemExit still propagate.
"""

from __future__ import annotations

import asyncio

import pytest

from tool_call_retry.errors import NonRetryableError, RetryableError
from tool_call_retry.policy import RetryExhausted, RetryPolicy


class Terminal(NonRetryableError):
    """Non-retryable error — simulates a hard failure."""

    pass


class Transient(RetryableError):
    """Retryable error — simulates a transient failure."""

    pass


@pytest.fixture
def make_policy():
    """Fast policy: 3 attempts, no delay."""
    return lambda: RetryPolicy(max_attempts=3, base_delay=0.0, jitter="none")


def test_terminal_error_survives_exploding_observer(make_policy):
    """A terminal (non-retryable) error is re-raised even when on_attempt explodes."""

    def exploding_observer(record):
        raise ValueError("observer exploded")

    def func():
        raise Terminal("the real error")

    with pytest.raises(Terminal, match="the real error"):
        make_policy().run(func, on_attempt=exploding_observer)


def test_cause_is_the_real_error_not_the_observer(make_policy):
    """RetryExhausted.__cause__ must be the Transient, not the observer error."""

    def exploding_observer(record):
        raise ValueError("observer exploded")

    def func():
        raise Transient("the real transient")

    with pytest.raises(RetryExhausted) as excinfo:
        make_policy().run(
            func, sleep=lambda d: None, on_attempt=exploding_observer
        )

    cause = excinfo.value.__cause__
    assert isinstance(
        cause, Transient
    ), f"__cause__ was {cause!r}, expected Transient with 'the real transient'"
    assert "observer exploded" not in str(
        cause
    ), "observer error leaked into __cause__"


def test_exploding_observer_on_success_does_not_leave_a_leading_space(
    make_policy,
):
    """An observer that raises on the success path must not record a leading space."""

    kept = []

    def exploding_observer(record):
        kept.append(record)
        raise ValueError("observer exploded")

    result = make_policy().run(lambda: "fine", on_attempt=exploding_observer)

    assert result == "fine"
    # The error note must have no leading space
    assert (
        "observer exploded" in kept[0].error
    ), f"observer error not recorded: {kept[0].error!r}"
    assert not kept[0].error.startswith(
        " "
    ), f"leading space in success-path error: {kept[0].error!r}"


def test_keyboard_interrupt_still_propagates(make_policy):
    """_notify catches Exception, not BaseException — Ctrl-C must still escape."""

    def ki_observer(record):
        raise KeyboardInterrupt()

    def func():
        raise Terminal("the real error")

    with pytest.raises(KeyboardInterrupt):
        make_policy().run(func, on_attempt=ki_observer)


# --- async variants ---

# Note: on_attempt is called synchronously in both run() and arun().
# The async tests verify the same guard logic applies in the async code path.


def test_cause_is_the_real_error_not_the_observer_arun(make_policy):
    """arun: RetryExhausted.__cause__ must be the Transient, not the observer error."""

    def exploding_observer(record):
        raise ValueError("observer exploded")

    async def func():
        raise Transient("the real transient")

    async def main():
        return await make_policy().arun(func, sleep=lambda d: None, on_attempt=exploding_observer)

    with pytest.raises(RetryExhausted) as excinfo:
        asyncio.run(main())

    cause = excinfo.value.__cause__
    assert isinstance(cause, Transient), f"__cause__ was {cause!r}"
    assert "observer exploded" not in str(cause)


def test_exploding_observer_on_success_no_leading_space_arun(make_policy):
    """arun: success-path observer error must not leave a leading space."""

    kept = []

    def exploding_observer(record):
        kept.append(record)
        raise ValueError("observer exploded")

    async def main():
        return await make_policy().arun(lambda: "fine", on_attempt=exploding_observer)

    result = asyncio.run(main())

    assert result == "fine"
    assert "observer exploded" in kept[0].error
    assert not kept[0].error.startswith(" ")


def test_retry_attempt_is_mutable():
    """RetryAttempt is a plain @dataclass (not frozen) — direct mutation must not raise."""
    from tool_call_retry.models import RetryAttempt

    record = RetryAttempt(step_id=0, attempt=1, delay=0.0, succeeded=False)
    record.error = "mutate ok"  # must not raise FrozenInstanceError
    assert record.error == "mutate ok"
