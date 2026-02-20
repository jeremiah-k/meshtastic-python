"""Retry and reconnection policies for BLE operations."""

import random
from typing import Any, Callable, Optional, Protocol, Tuple

from meshtastic.interfaces.ble.constants import BLEConfig


class _RandomLike(Protocol):
    """Protocol for objects that provide a random() method."""

    def random(self) -> float:
        """
        Produce a pseudo-random floating-point value in the half-open interval [0.0, 1.0).

        Returns
        -------
            float: A value x such that 0.0 <= x < 1.0.

        """
        ...


class ReconnectPolicy:
    """
    Unified reconnection / retry policy with jittered exponential backoff.
    """

    class ConfigurationError(ValueError):
        """Raised when a ReconnectPolicy is constructed with invalid parameters."""

    def __init__(
        self,
        *,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff: float = 2.0,
        jitter_ratio: float = 0.1,
        max_retries: Optional[int] = None,
        random_source: Optional[_RandomLike] = None,
    ) -> None:
        """
        Initialize a jittered exponential-backoff reconnect policy.

        Parameters
        ----------
                initial_delay (float): Starting delay in seconds; must be greater than 0.
                max_delay (float): Maximum delay in seconds; must be greater than or equal to `initial_delay`.
                backoff (float): Multiplicative backoff factor applied between attempts; must be greater than 1.0.
                jitter_ratio (float): Fractional symmetric jitter applied to the computed delay, between 0.0 and 1.0.
                max_retries (Optional[int]): Maximum number of retry attempts; `None` means unlimited. If provided, must be >= 0.
                random_source: Source of randomness for jitter (object with a `random()` method); defaults to the module-level `random`.

        Raises
        ------
                ValueError: If any parameter is outside its valid range.

        """
        if initial_delay <= 0:
            raise ReconnectPolicy.ConfigurationError(
                f"initial_delay must be > 0, got {initial_delay}"
            )
        if max_delay < initial_delay:
            raise ReconnectPolicy.ConfigurationError(
                f"max_delay ({max_delay}) must be >= initial_delay ({initial_delay})"
            )
        if backoff <= 1.0:
            raise ReconnectPolicy.ConfigurationError(
                f"backoff must be > 1.0, got {backoff}"
            )
        if not 0.0 <= jitter_ratio <= 1.0:
            raise ReconnectPolicy.ConfigurationError(
                f"jitter_ratio must be between 0.0 and 1.0, got {jitter_ratio}"
            )
        if max_retries is not None and max_retries < 0:
            raise ReconnectPolicy.ConfigurationError(
                f"max_retries must be >= 0 or None, got {max_retries}"
            )
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.backoff = backoff
        self.jitter_ratio = jitter_ratio
        self.max_retries = max_retries
        self._random: _RandomLike = random_source or random
        self._attempt_count = 0

    def reset(self) -> None:
        """Reset internal state to begin a fresh retry cycle."""
        self._attempt_count = 0

    def _get_delay(self, attempt: Optional[int] = None) -> float:
        """
        Internal helper: compute jittered exponential-backoff delay.

        Computes initial_delay * backoff**attempt, applies symmetric jitter based on
        jitter_ratio and the configured random source, and clamps the result to the
        range [0.001, max_delay].

        Parameters
        ----------
        attempt (Optional[int]):
            Zero-based retry attempt index to compute the delay for; if omitted, uses
            the policy's current attempt count.

        Returns
        -------
        delay (float):
            Delay in seconds — exponential-backoff value with symmetric jitter applied,
            clamped to at least 0.001 and not exceeding the policy's max_delay.

        """
        if attempt is None:
            attempt = self._attempt_count
        delay = min(self.initial_delay * (self.backoff**attempt), self.max_delay)
        jitter = delay * self.jitter_ratio * (self._random.random() * 2.0 - 1.0)
        return min(
            self.max_delay, max(0.001, delay + jitter)
        )  # Clamp to [0.001, max_delay]

    def _should_retry(self, attempt: Optional[int] = None) -> bool:
        """
        Internal helper: determine whether another retry attempt is permitted.

        Parameters
        ----------
            attempt (Optional[int]): Zero-based attempt index to evaluate; when omitted the policy's current attempt count is used.

        Returns
        -------
            bool: True if another retry is allowed (retries are unlimited when `max_retries` is None), False otherwise.

        """
        if attempt is None:
            attempt = self._attempt_count
        return self.max_retries is None or attempt < self.max_retries

    def _next_attempt(self) -> Tuple[float, bool]:
        """
        Internal helper: compute the jittered backoff delay for the current attempt, indicate whether
        another retry is permitted, and then advance the internal attempt counter.

        `should_retry` is computed by calling _should_retry() before incrementing
        _attempt_count, so `max_retries` represents retries after the initial
        connection attempt (total attempts = 1 + max_retries).

        Returns
        -------
            delay (float): Computed jittered backoff delay for the current attempt.
            should_retry (bool): `True` if another retry is permitted, `False` otherwise.

        """
        delay = self._get_delay()
        should_retry = self._should_retry()
        self._attempt_count += 1
        return delay, should_retry

    def _get_attempt_count(self) -> int:
        """
        Internal helper: return the number of attempts performed by this policy.

        Returns
        -------
            int: The number of attempts performed so far.

        """
        return self._attempt_count

    def _sleep_with_backoff(self, attempt: int) -> None:
        """
        Internal helper: sleep for the policy's jittered exponential-backoff delay.

        Parameters
        ----------
            attempt (int): Zero-based attempt index used to compute the jittered exponential-backoff delay.

        """
        from meshtastic.interfaces.ble.utils import _sleep

        _sleep(self._get_delay(attempt))


class _PolicyDescriptor:
    """Descriptor that returns a fresh policy instance on each access."""

    def __init__(self, factory_name: str) -> None:
        """
        Store the attribute name of a factory method on the owner class that produces new ReconnectPolicy instances.

        Parameters
        ----------
            factory_name (str): Name of the factory method attribute on the owner class.

        """
        self.factory_name = factory_name

    def __get__(self, _obj: Any, cls: type) -> ReconnectPolicy:
        """
        Provide a fresh ReconnectPolicy produced by the owner class's factory method.

        Each access invokes the named factory on the owner class and returns a new ReconnectPolicy instance to avoid shared state.

        Returns
        -------
            ReconnectPolicy: A new policy instance created by the owner's factory method.

        """
        factory: Callable[[], ReconnectPolicy] = getattr(cls, self.factory_name)
        return factory()


class RetryPolicy:
    """
    Static retry policy presets for BLE operations.
    """

    # Backwards-compatible attribute accessors that return fresh instances
    EMPTY_READ = _PolicyDescriptor("empty_read")
    TRANSIENT_ERROR = _PolicyDescriptor("transient_error")
    AUTO_RECONNECT = _PolicyDescriptor("auto_reconnect")

    @staticmethod
    def empty_read() -> ReconnectPolicy:
        """
        Return a ReconnectPolicy configured for retrying empty BLE read responses.

        The policy uses BLEConfig.EMPTY_READ_RETRY_DELAY as the initial delay, clamps delay to 1.0 second, uses a backoff of 1.5 with a jitter ratio of 0.1, and sets max_retries from BLEConfig.EMPTY_READ_MAX_RETRIES.

        Returns
        -------
            ReconnectPolicy: A ReconnectPolicy configured for empty-read retries.

        """
        return ReconnectPolicy(
            initial_delay=BLEConfig.EMPTY_READ_RETRY_DELAY,
            max_delay=1.0,
            backoff=1.5,
            jitter_ratio=0.1,
            max_retries=BLEConfig.EMPTY_READ_MAX_RETRIES,
        )

    @staticmethod
    def transient_error() -> ReconnectPolicy:
        """
        ReconnectPolicy tuned for transient BLE read errors.

        Returns
        -------
            ReconnectPolicy configured with initial_delay from BLEConfig.TRANSIENT_READ_RETRY_DELAY, max_delay 2.0, backoff 1.5, jitter_ratio 0.1, and max_retries from BLEConfig.TRANSIENT_READ_MAX_RETRIES.

        """
        return ReconnectPolicy(
            initial_delay=BLEConfig.TRANSIENT_READ_RETRY_DELAY,
            max_delay=2.0,
            backoff=1.5,
            jitter_ratio=0.1,
            max_retries=BLEConfig.TRANSIENT_READ_MAX_RETRIES,
        )

    @staticmethod
    def auto_reconnect() -> ReconnectPolicy:
        """
        Create a ReconnectPolicy configured for automatic BLE reconnection.

        Returns
        -------
            ReconnectPolicy: Configured using BLEConfig AUTO_RECONNECT_* values with unlimited retries.

        """
        return ReconnectPolicy(
            initial_delay=BLEConfig.AUTO_RECONNECT_INITIAL_DELAY,
            max_delay=BLEConfig.AUTO_RECONNECT_MAX_DELAY,
            backoff=BLEConfig.AUTO_RECONNECT_BACKOFF,
            jitter_ratio=BLEConfig.AUTO_RECONNECT_JITTER_RATIO,
            max_retries=None,
        )
