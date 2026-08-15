"""Tests for shared internal transport retry policy primitives."""

from meshtastic.transport_retry import (
    _exponential_retry_delay,
    _linear_retry_delay,
    _plan_counted_retry,
    _RetryDisposition,
    _RetryWindow,
    _sleep_interruptibly,
)


def test_retry_window_clamps_delay_to_remaining_budget() -> None:
    """A bounded retry should never sleep beyond its remaining time budget."""
    window = _RetryWindow.start(now=10.0, duration=2.0, max_attempts=4)

    decision = window.after_failure(attempt=1, retry_delay=5.0, now=11.25)

    assert decision.disposition is _RetryDisposition.RETRY
    assert decision.delay == 0.75
    assert decision.remaining == 0.75


def test_retry_window_exhausts_on_attempt_or_deadline() -> None:
    """Either retry bound should independently terminate the sequence."""
    window = _RetryWindow.start(now=1.0, duration=5.0, max_attempts=2)

    assert (
        window.after_failure(attempt=2, retry_delay=1.0, now=2.0).disposition
        is _RetryDisposition.EXHAUSTED
    )
    assert (
        window.after_failure(attempt=1, retry_delay=1.0, now=6.0).disposition
        is _RetryDisposition.EXHAUSTED
    )


def test_counted_retry_plan_preserves_immediate_first_attempt() -> None:
    """TCP-style counted retries should keep the first reconnect immediate."""
    first = _plan_counted_retry(
        completed_attempts=0,
        max_attempts=3,
        base_delay=1.0,
        backoff=2.0,
        max_delay=10.0,
    )
    second = _plan_counted_retry(
        completed_attempts=1,
        max_attempts=3,
        base_delay=1.0,
        backoff=2.0,
        max_delay=10.0,
    )
    exhausted = _plan_counted_retry(
        completed_attempts=3,
        max_attempts=3,
        base_delay=1.0,
        backoff=2.0,
        max_delay=10.0,
    )

    assert (first.attempt, first.delay) == (1, 0.0)
    assert (second.attempt, second.delay) == (2, 2.0)
    assert exhausted.disposition is _RetryDisposition.EXHAUSTED


def test_retry_delay_helpers_bound_backoff() -> None:
    """Shared delay helpers should preserve exponential and linear bounds."""
    assert (
        _exponential_retry_delay(
            attempt=4, base_delay=2.0, backoff=2.0, max_delay=10.0
        )
        == 10.0
    )
    assert _linear_retry_delay(retry_number=4, base_delay=0.1) == 0.4
    assert (
        _linear_retry_delay(retry_number=4, base_delay=0.1, max_delay=0.25)
        == 0.25
    )


def test_interruptible_sleep_stops_when_abort_becomes_true() -> None:
    """Interruptible retry delay should return immediately after an abort signal."""
    now = [0.0]
    aborted = [False]
    sleeps: list[float] = []

    def _monotonic() -> float:
        return now[0]

    def _sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay
        aborted[0] = True

    assert not _sleep_interruptibly(
        2.0,
        should_abort=lambda: aborted[0],
        sleep_slice=0.25,
        monotonic=_monotonic,
        sleep=_sleep,
    )
    assert sleeps == [0.25]
