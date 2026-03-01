"""Retry and reconnection policies for BLE operations."""

import random
from typing import Any, Callable, Protocol

from meshtastic.interfaces.ble.constants import BLEConfig
from meshtastic.interfaces.ble.utils import _sleep


class _RandomLike(Protocol):
    """Protocol for objects that provide a random() method."""

    def random(self) -> float:
        """Return a float uniformly sampled from the half-open interval [0.0, 1.0).

        Returns
        -------
        float
            A value x such that 0.0 <= x < 1.0.
        """
        ...


class ReconnectPolicy:
    """Unified reconnection / retry policy with jittered exponential backoff."""

    class ConfigurationError(ValueError):
        """Raised when a ReconnectPolicy is constructed with invalid parameters."""

        @classmethod
        def invalid_value(
            cls, name: str, requirement: str, value: Any
        ) -> "ReconnectPolicy.ConfigurationError":
            """Build a standardized invalid-value configuration error."""
            return cls(f"{name} must be {requirement}, got {value}")

        @classmethod
        def invalid_relation(
            cls,
            *,
            left_name: str,
            left_value: float,
            relation: str,
            right_name: str,
            right_value: float,
        ) -> "ReconnectPolicy.ConfigurationError":
            """Build a standardized invalid cross-field configuration error."""
            return cls(
                f"{left_name} ({left_value}) must be {relation} {right_name} ({right_value})"
            )

    def __init__(
        self,
        *,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff: float = 2.0,
        jitter_ratio: float = 0.1,
        max_retries: int | None = None,
        random_source: _RandomLike | None = None,
    ) -> None:
        """Initialize a reconnect policy that uses jittered exponential backoff.

        Parameters
        ----------
        initial_delay : float
            Starting delay in seconds; must be greater than 0. (Default value = 1.0)
        max_delay : float
            Maximum delay in seconds; must be greater than or equal to initial_delay. (Default value = 30.0)
        backoff : float
            Multiplicative factor applied to the delay between attempts; must be
            greater than 1.0 (backoff == 1.0 is rejected because it yields
            constant retry delay with no exponential growth). (Default value = 2.0)
        jitter_ratio : float
            Fractional symmetric jitter applied to the computed delay, between 0.0 and 1.0. (Default value = 0.1)
        max_retries : int | None
            Maximum number of retry attempts; `None` means unlimited and any provided value must be >= 0. (Default value = None)
        random_source : _RandomLike | None
            Source of randomness for jitter (object with a `random()` method); defaults to the module-level `random`.

        Raises
        ------
        ReconnectPolicy.ConfigurationError
            If any parameter is outside its valid range.
        """
        if initial_delay <= 0:
            raise ReconnectPolicy.ConfigurationError.invalid_value(
                "initial_delay", "> 0", initial_delay
            )
        if max_delay < initial_delay:
            raise ReconnectPolicy.ConfigurationError.invalid_relation(
                left_name="max_delay",
                left_value=max_delay,
                relation=">=",
                right_name="initial_delay",
                right_value=initial_delay,
            )
        if backoff <= 1.0:
            # ReconnectPolicy requires exponential growth; fixed/decelerating intervals
            # (<= 1.0) should use a different retry policy.
            raise ReconnectPolicy.ConfigurationError.invalid_value(
                "backoff", "> 1.0", backoff
            )
        if not 0.0 <= jitter_ratio <= 1.0:
            raise ReconnectPolicy.ConfigurationError.invalid_value(
                "jitter_ratio", "between 0.0 and 1.0", jitter_ratio
            )
        if max_retries is not None and max_retries < 0:
            raise ReconnectPolicy.ConfigurationError.invalid_value(
                "max_retries", ">= 0 or None", max_retries
            )
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.backoff = backoff
        self.jitter_ratio = jitter_ratio
        self.max_retries = max_retries
        self._random: _RandomLike = (
            random_source if random_source is not None else random
        )
        self._attempt_count = 0

    def reset(self) -> None:
        """Reset the policy's internal attempt counter to start a new retry cycle."""
        self._attempt_count = 0

    def _get_delay(self, attempt: int | None = None) -> float:
        """Compute a jittered exponential-backoff delay for a given retry attempt.

        Parameters
        ----------
        attempt : int | None
            Zero-based retry attempt index to compute the delay for;
            if omitted, uses the policy's current attempt count. (Default value = None)

        Returns
        -------
        float
            Delay in seconds — exponential-backoff value with symmetric jitter
            applied, clamped to at least 0.001 and not exceeding the policy's max_delay.
        """
        if attempt is None:
            attempt = self._attempt_count
        try:
            exp_delay = self.initial_delay * (self.backoff**attempt)
        except OverflowError:
            exp_delay = self.max_delay
        delay = min(exp_delay, self.max_delay)
        jitter = delay * self.jitter_ratio * (self._random.random() * 2.0 - 1.0)
        return min(
            self.max_delay, max(0.001, delay + jitter)
        )  # Clamp to [0.001, max_delay]

    def _should_retry(self, attempt: int | None = None) -> bool:
        """Return whether another retry attempt is permitted.

        Parameters
        ----------
        attempt : int | None
            Zero-based attempt index to evaluate; when omitted the policy's current attempt count is used. (Default value = None)

        Returns
        -------
        bool
            True if another retry is allowed (retries are unlimited when `max_retries` is None), False otherwise.
        """
        if attempt is None:
            attempt = self._attempt_count
        return self.max_retries is None or attempt < self.max_retries

    def next_attempt(self) -> tuple[float, bool]:
        """Compute delay for the current retry attempt and report whether that consumed attempt is permitted.

        Order of operations for each call:
        1. `_get_delay()` computes the delay for the current attempt counter.
        2. `_attempt_count` is incremented to consume this attempt.
        3. `_should_retry(consumed_attempt)` computes whether the consumed/current
           attempt index is permitted by the retry budget.

        `max_retries` counts retries after the initial attempt, so total
        attempts are `1 + max_retries`. The returned boolean indicates whether
        the attempt whose delay was just computed is allowed, not whether an
        additional attempt remains after this call.

        Returns
        -------
        delay_and_permission : tuple[float, bool]
            `(delay_seconds, consumed_attempt_is_permitted)`.
        """
        consumed_attempt = self._attempt_count
        delay = self._get_delay()
        self._attempt_count += 1
        should_retry = self._should_retry(consumed_attempt)
        return delay, should_retry

    def get_attempt_count(self) -> int:
        """Return the number of retry attempts performed so far.

        Returns
        -------
        attempt_count : int
            The count of attempts that have been made.
        """
        return self._attempt_count

    def _sleep_with_backoff(self, attempt: int) -> None:
        """Sleep for the policy's jittered exponential-backoff delay.

        Parameters
        ----------
        attempt : int
            Zero-based attempt index used to compute the delay.

        Returns
        -------
        None
        """
        _sleep(self._get_delay(attempt))


class _PolicyDescriptor:
    """Descriptor that returns a fresh policy instance on each access."""

    def __init__(self, factory_name: str) -> None:
        """Store the attribute name of a factory method on the owner class that produces new ReconnectPolicy instances.

        Parameters
        ----------
        factory_name : str
            Name of the factory method attribute on the owner class.

        Returns
        -------
        None
        """
        self.factory_name = factory_name

    def __get__(self, _obj: Any, cls: type) -> ReconnectPolicy:
        """Return a fresh ReconnectPolicy instance produced by the owner class's named factory method.

        Calls the factory method stored in this descriptor's factory_name on the owner class and returns its result.

        Parameters
        ----------
        _obj : Any
            The instance through which the descriptor was accessed (None for class access).
        cls : type
            The owning class type.

        Returns
        -------
        ReconnectPolicy
            A new policy instance created by the owner's factory method.
        """
        # Policies are stateful (attempt counters), so descriptor access must
        # always return a fresh instance rather than a shared singleton.
        factory: Callable[[], ReconnectPolicy] = getattr(cls, self.factory_name)
        return factory()


class RetryPolicy:
    """Static retry policy presets for BLE operations."""

    # Backwards-compatible attribute accessors that return fresh instances.
    EMPTY_READ = _PolicyDescriptor("_empty_read")
    TRANSIENT_ERROR = _PolicyDescriptor("_transient_error")
    AUTO_RECONNECT = _PolicyDescriptor("_auto_reconnect")

    @staticmethod
    def _empty_read() -> ReconnectPolicy:
        """Policy configured for retrying empty BLE read responses.

        Uses BLEConfig.EMPTY_READ_RETRY_DELAY as the initial delay, clamps delay to 1.0 second, uses a backoff of 1.5 with a jitter ratio of 0.1, and sets max_retries from BLEConfig.EMPTY_READ_MAX_RETRIES.

        Returns
        -------
        ReconnectPolicy
            A ReconnectPolicy configured for empty-read retries.
        """
        return ReconnectPolicy(
            initial_delay=BLEConfig.EMPTY_READ_RETRY_DELAY,
            max_delay=1.0,
            backoff=1.5,
            jitter_ratio=0.1,
            max_retries=BLEConfig.EMPTY_READ_MAX_RETRIES,
        )

    @staticmethod
    def _transient_error() -> ReconnectPolicy:
        """Create a ReconnectPolicy tuned for transient BLE read errors.

        Returns
        -------
        ReconnectPolicy
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
    def _auto_reconnect() -> ReconnectPolicy:
        """Create a ReconnectPolicy preset for automatic BLE reconnection.

        Returns
        -------
        ReconnectPolicy
            Policy configured from BLEConfig AUTO_RECONNECT_* constants with unlimited retries.
        """
        return ReconnectPolicy(
            initial_delay=BLEConfig.AUTO_RECONNECT_INITIAL_DELAY,
            max_delay=BLEConfig.AUTO_RECONNECT_MAX_DELAY,
            backoff=BLEConfig.AUTO_RECONNECT_BACKOFF,
            jitter_ratio=BLEConfig.AUTO_RECONNECT_JITTER_RATIO,
            max_retries=None,
        )
