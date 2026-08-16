"""Internal retry-policy primitives shared by stream transports."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto


class _RetryDisposition(Enum):
    """Decision returned by retry planners."""

    RETRY = auto()
    EXHAUSTED = auto()


@dataclass(frozen=True, slots=True)
class _RetryDecision:
    """Immutable retry decision produced from transport-local state."""

    disposition: _RetryDisposition
    attempt: int
    delay: float = 0.0
    remaining: float | None = None

    @property
    def should_retry(self) -> bool:
        """Return whether the failed operation should be retried."""
        return self.disposition is _RetryDisposition.RETRY


@dataclass(frozen=True, slots=True)
class _RetryWindow:
    """Deadline and attempt limit for a bounded retry sequence."""

    deadline: float | None
    max_attempts: int

    @classmethod
    def start(
        cls, *, now: float, duration: float | None, max_attempts: int
    ) -> "_RetryWindow":
        """Create a retry window from a monotonic timestamp and duration."""
        deadline = None if duration is None else now + max(0.0, duration)
        return cls(deadline=deadline, max_attempts=max_attempts)

    def after_failure(
        self, *, attempt: int, retry_delay: float, now: float
    ) -> _RetryDecision:
        """Plan the retry following a failed one-based attempt."""
        remaining = None if self.deadline is None else self.deadline - now
        exhausted = attempt >= self.max_attempts or (
            remaining is not None and remaining <= 0.0
        )
        if exhausted:
            return _RetryDecision(
                _RetryDisposition.EXHAUSTED,
                attempt=attempt,
                remaining=remaining,
            )
        delay = max(0.0, retry_delay)
        if remaining is not None:
            delay = min(delay, remaining)
        return _RetryDecision(
            _RetryDisposition.RETRY,
            attempt=attempt,
            delay=delay,
            remaining=remaining,
        )


def _exponential_retry_delay(
    *, attempt: int, base_delay: float, backoff: float, max_delay: float
) -> float:
    """Return bounded exponential delay for a one-based retry attempt."""
    exponent = max(0, attempt - 1)
    return min(max_delay, base_delay * (backoff**exponent))


def _linear_retry_delay(
    *, retry_number: int, base_delay: float, max_delay: float | None = None
) -> float:
    """Return a non-negative linear delay for a retry counter.

    A zero retry counter produces zero delay, preserving the raw
    ``base_delay * retry_number`` semantics used by the original stream loop.
    """
    delay = max(0.0, base_delay * retry_number)
    if max_delay is not None:
        return min(max_delay, delay)
    return delay


def _plan_counted_retry(
    *,
    completed_attempts: int,
    max_attempts: int,
    base_delay: float,
    backoff: float,
    max_delay: float,
    immediate_first_attempt: bool = True,
) -> _RetryDecision:
    """Plan the next attempt for a retry counter owned by a transport."""
    if completed_attempts >= max_attempts:
        return _RetryDecision(
            _RetryDisposition.EXHAUSTED,
            attempt=completed_attempts,
        )
    attempt = completed_attempts + 1
    delay = 0.0
    if not (immediate_first_attempt and attempt == 1):
        delay = _exponential_retry_delay(
            attempt=attempt,
            base_delay=base_delay,
            backoff=backoff,
            max_delay=max_delay,
        )
    return _RetryDecision(_RetryDisposition.RETRY, attempt=attempt, delay=delay)


def _sleep_interruptibly(
    delay: float,
    *,
    should_abort: Callable[[], bool],
    sleep_slice: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """Sleep until a retry deadline while polling an abort predicate."""
    deadline = monotonic() + max(0.0, delay)
    while True:
        if should_abort():
            return False
        remaining = deadline - monotonic()
        if remaining <= 0.0:
            return True
        sleep(min(sleep_slice, remaining))
