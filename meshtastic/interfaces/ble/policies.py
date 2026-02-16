"""Retry and reconnection policies for BLE operations."""

import random
from typing import Any, Callable, Optional, Tuple

from meshtastic.interfaces.ble.constants import BLEConfig


class ReconnectPolicy:
    """
    Unified reconnection / retry policy with jittered exponential backoff.
    """

    def __init__(
        self,
        *,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff: float = 2.0,
        jitter_ratio: float = 0.1,
        max_retries: Optional[int] = None,
        random_source=None,
    ) -> None:
        """
        Initialize a jittered exponential-backoff reconnect policy.
        
        Parameters:
        	initial_delay (float): Starting delay in seconds; must be greater than 0.
        	max_delay (float): Maximum delay in seconds; must be greater than or equal to `initial_delay`.
        	backoff (float): Multiplicative backoff factor applied between attempts; must be greater than 1.0.
        	jitter_ratio (float): Fractional symmetric jitter applied to the computed delay, between 0.0 and 1.0.
        	max_retries (Optional[int]): Maximum number of retry attempts; `None` means unlimited. If provided, must be >= 0.
        	random_source: Source of randomness for jitter (object with a `random()` method); defaults to the module-level `random`.
        
        Raises:
        	ValueError: If any parameter is outside its valid range.
        """
        if initial_delay <= 0:
            raise ValueError(f"initial_delay must be > 0, got {initial_delay}")
        if max_delay < initial_delay:
            raise ValueError(
                f"max_delay ({max_delay}) must be >= initial_delay ({initial_delay})"
            )
        if backoff <= 1.0:
            raise ValueError(f"backoff must be > 1.0, got {backoff}")
        if not 0.0 <= jitter_ratio <= 1.0:
            raise ValueError(
                f"jitter_ratio must be between 0.0 and 1.0, got {jitter_ratio}"
            )
        if max_retries is not None and max_retries < 0:
            raise ValueError(f"max_retries must be >= 0 or None, got {max_retries}")
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.backoff = backoff
        self.jitter_ratio = jitter_ratio
        self.max_retries = max_retries
        self._random = random_source or random
        self._attempt_count = 0

    def reset(self) -> None:
        """Reset internal state to begin a fresh retry cycle."""
        self._attempt_count = 0

    def get_delay(self, attempt: Optional[int] = None) -> float:
        """
        Compute the jittered exponential-backoff delay for a retry attempt.

        Parameters
        ----------
                attempt (Optional[int]): Zero-based retry attempt index to compute the delay for; if omitted, uses the policy's current attempt count.

        Returns
        -------
                delay (float): Delay in seconds — exponential-backoff value with symmetric jitter applied, clamped to at least 0.001 and not exceeding the policy's max_delay.

        """
        if attempt is None:
            attempt = self._attempt_count
        delay = min(self.initial_delay * (self.backoff**attempt), self.max_delay)
        jitter = delay * self.jitter_ratio * (self._random.random() * 2.0 - 1.0)
        return min(
            self.max_delay, max(0.001, delay + jitter)
        )  # Clamp to [0.001, max_delay]

    def should_retry(self, attempt: Optional[int] = None) -> bool:
        """
        Determine whether another retry attempt is permitted by this policy.

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

    def next_attempt(self) -> Tuple[float, bool]:
        """
        Return the delay and retry permission for the next attempt and advance the internal attempt counter.
        
        The method computes the jittered backoff delay for the upcoming attempt, determines whether another retry is allowed, increments the internal attempt counter, and returns both values.
        
        Returns:
            tuple:
                delay (float): Jittered backoff delay to use for the upcoming attempt.
                should_retry (bool): `True` if another retry is permitted, `False` otherwise.
        """
        delay = self.get_delay()
        should_retry = self.should_retry()
        self._attempt_count += 1
        return delay, should_retry

    def get_attempt_count(self) -> int:
        """
        Return the number of attempts that have been performed by this policy.
        
        Returns:
            int: The current attempt count.
        """
        return self._attempt_count

    def sleep_with_backoff(self, attempt: int) -> None:
        """
        Pause execution for the policy's backoff delay for a given retry attempt.
        
        Parameters:
            attempt (int): 0-based attempt index used to compute the delay with the policy's backoff and jitter.
        """
        from meshtastic.interfaces.ble.utils import _sleep

        _sleep(self.get_delay(attempt))


class _PolicyDescriptor:
    """Descriptor that returns a fresh policy instance on each access."""

    def __init__(self, factory_name: str) -> None:
        """
        Store the name of a factory method that will be called on the owner class to produce fresh ReconnectPolicy instances.
        
        Parameters:
            factory_name (str): The attribute name of the factory method on the owner class which, when called, returns a new ReconnectPolicy instance.
        """
        self.factory_name = factory_name

    def __get__(self, _obj: Any, cls: type) -> ReconnectPolicy:
        """
        Return a fresh ReconnectPolicy instance by invoking the owner's factory method named by this descriptor.
        
        Returns:
            ReconnectPolicy: A new policy instance produced by the owner's factory method referenced by `factory_name`.
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
        ReconnectPolicy preset for retrying empty BLE read responses.
        
        Uses BLEConfig.EMPTY_READ_RETRY_DELAY as the initial delay, caps delay at 1.0 second, applies a backoff of 1.5 with a jitter ratio of 0.1, and limits retries to BLEConfig.EMPTY_READ_MAX_RETRIES.
        
        Returns:
            ReconnectPolicy: Configured policy for empty-read retries.
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
        Create a ReconnectPolicy tuned for transient BLE read errors.
        
        Returns:
            ReconnectPolicy: A policy with initial_delay from BLEConfig.TRANSIENT_READ_RETRY_DELAY, max_delay 2.0, backoff 1.5, jitter_ratio 0.1, and max_retries from BLEConfig.TRANSIENT_READ_MAX_RETRIES.
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
        Create a ReconnectPolicy preconfigured for automatic BLE reconnection.
        
        Returns:
            ReconnectPolicy: Policy configured from BLEConfig AUTO_RECONNECT_* values with unlimited retries (max_retries=None).
        """
        return ReconnectPolicy(
            initial_delay=BLEConfig.AUTO_RECONNECT_INITIAL_DELAY,
            max_delay=BLEConfig.AUTO_RECONNECT_MAX_DELAY,
            backoff=BLEConfig.AUTO_RECONNECT_BACKOFF,
            jitter_ratio=BLEConfig.AUTO_RECONNECT_JITTER_RATIO,
            max_retries=None,
        )