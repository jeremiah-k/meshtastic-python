"""Retry and reconnection policies for BLE operations."""

import random
from typing import Optional, Tuple, Callable

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
    ):
        """
        Create a jittered exponential-backoff retry policy instance.
        
        Parameters:
        	initial_delay (float): Starting delay in seconds; must be greater than 0.
        	max_delay (float): Maximum delay in seconds; must be greater than or equal to `initial_delay`.
        	backoff (float): Exponential multiplication factor applied to the delay between attempts; must be greater than 1.0.
        	jitter_ratio (float): Fractional jitter to apply to the computed delay (0.0 to 1.0).
        	max_retries (Optional[int]): Maximum number of retry attempts; `None` means unlimited.
        	random_source: Optional random-number generator used to apply jitter; defaults to the module-level `random`.
        
        Raises:
        	ValueError: If any parameter is outside its valid range (invalid `initial_delay`, `max_delay`, `backoff`, `jitter_ratio`, or `max_retries`).
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
        Compute the jittered backoff delay for a given retry attempt.
        
        Parameters:
        	attempt (Optional[int]): Zero-based attempt index to compute the delay for; if omitted, uses the policy's current attempt count.
        
        Returns:
        	delay (float): Delay in seconds with exponential backoff and symmetric jitter applied, clamped to at least 0.001s (to cover extreme jitter) and not exceeding the policy's max_delay.
        """
        if attempt is None:
            attempt = self._attempt_count
        delay = min(self.initial_delay * (self.backoff**attempt), self.max_delay)
        jitter = delay * self.jitter_ratio * (self._random.random() * 2.0 - 1.0)
        return max(0.001, delay + jitter)  # Ensure a small positive delay

    def should_retry(self, attempt: Optional[int] = None) -> bool:
        """
        Decides if another retry attempt is allowed under the current policy.
        
        Parameters:
            attempt (Optional[int]): Attempt index to evaluate; when omitted, the policy's current attempt count is used.
        
        Returns:
            `True` if another retry is allowed by `max_retries`, `False` otherwise.
        """
        if attempt is None:
            attempt = self._attempt_count
        return self.max_retries is None or attempt < self.max_retries

    def next_attempt(self) -> Tuple[float, bool]:
        """
        Compute the delay for the upcoming attempt, determine whether another retry is allowed, and increment the internal attempt counter.
        
        Returns:
            tuple: (delay, should_retry)
                delay (float): Jittered backoff delay to use for this attempt.
                should_retry (bool): `True` if another retry is permitted, `False` otherwise.
        """
        delay = self.get_delay()
        should_retry = self.should_retry()
        self._attempt_count += 1
        return delay, should_retry

    def get_attempt_count(self) -> int:
        """
        Return the number of attempts recorded by this policy.
        
        Returns:
            int: The current attempt count.
        """
        return self._attempt_count

    def sleep_with_backoff(self, attempt: int) -> None:
        """
        Block execution for the jittered delay corresponding to the given attempt.
        
        Parameters:
            attempt (int): Attempt index used to compute the delay (0-based).
        """
        from meshtastic.interfaces.ble.utils import _sleep

        _sleep(self.get_delay(attempt))


class _PolicyDescriptor:
    """Descriptor that returns a fresh policy instance on each access."""

    def __init__(self, factory_name: str):
        """
        Initialize the descriptor with the name of the factory method used to create policy instances.
        
        Parameters:
            factory_name (str): Name of the factory method on the owner class that returns a fresh policy instance.
        """
        self.factory_name = factory_name

    def __get__(self, _obj, cls):
        """
        Descriptor getter that returns a fresh ReconnectPolicy instance by invoking the named factory on the owning class.
        
        Parameters:
            cls (type): The owner class from which the factory method (stored in `factory_name`) is retrieved.
        
        Returns:
            ReconnectPolicy: A new policy instance produced by the factory method.
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
        Create a ReconnectPolicy configured for handling empty BLE read responses.
        
        Returns:
            policy (ReconnectPolicy): A ReconnectPolicy preset for empty-read retries, configured with a short initial delay, small maximum delay, modest backoff, jitter, and a limited number of retries.
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
        Provide a retry policy configured for transient BLE read errors.
        
        Returns:
            ReconnectPolicy: A policy configured with initial delay from BLEConfig.TRANSIENT_READ_RETRY_DELAY, max_delay 2.0, backoff 1.5, jitter_ratio 0.1, and max_retries from BLEConfig.TRANSIENT_READ_MAX_RETRIES.
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
        
        Returns:
            ReconnectPolicy: Policy initialized with auto-reconnect parameters from BLEConfig and unlimited retries.
        """
        return ReconnectPolicy(
            initial_delay=BLEConfig.AUTO_RECONNECT_INITIAL_DELAY,
            max_delay=BLEConfig.AUTO_RECONNECT_MAX_DELAY,
            backoff=BLEConfig.AUTO_RECONNECT_BACKOFF,
            jitter_ratio=BLEConfig.AUTO_RECONNECT_JITTER_RATIO,
            max_retries=None,
        )
