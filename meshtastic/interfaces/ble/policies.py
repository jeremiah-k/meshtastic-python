"""Retry and reconnection policies for BLE operations."""

import random
from typing import Optional, Tuple

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
        Compute the jittered delay for the provided attempt (defaults to current attempt count).
        """
        if attempt is None:
            attempt = self._attempt_count
        delay = min(self.initial_delay * (self.backoff**attempt), self.max_delay)
        jitter = delay * self.jitter_ratio * (self._random.random() * 2.0 - 1.0)
        return max(0.001, delay + jitter)  # Ensure a small positive delay

    def should_retry(self, attempt: Optional[int] = None) -> bool:
        """
        Determine whether another retry should be attempted.
        """
        if attempt is None:
            attempt = self._attempt_count
        return self.max_retries is None or attempt < self.max_retries

    def next_attempt(self) -> Tuple[float, bool]:
        """
        Advance the attempt counter and return (delay, should_retry).
        """
        delay = self.get_delay()
        should_retry = self.should_retry()
        self._attempt_count += 1
        return delay, should_retry

    def get_attempt_count(self) -> int:
        """Expose the current attempt count (primarily for logging)."""
        return self._attempt_count

    def sleep_with_backoff(self, attempt: int) -> None:
        """Sleep for the jittered delay associated with the supplied attempt."""
        from meshtastic.interfaces.ble.utils import _sleep

        _sleep(self.get_delay(attempt))

class RetryPolicy:
    """
    Static retry policy presets for BLE operations.
    """

    @staticmethod
    def empty_read() -> ReconnectPolicy:
        """Factory for empty-read retry policy."""
        return ReconnectPolicy(
            initial_delay=BLEConfig.EMPTY_READ_RETRY_DELAY,
            max_delay=1.0,
            backoff=1.5,
            jitter_ratio=0.1,
            max_retries=BLEConfig.EMPTY_READ_MAX_RETRIES,
        )

    @staticmethod
    def transient_error() -> ReconnectPolicy:
        """Factory for transient BLE error retry policy."""
        return ReconnectPolicy(
            initial_delay=BLEConfig.TRANSIENT_READ_RETRY_DELAY,
            max_delay=2.0,
            backoff=1.5,
            jitter_ratio=0.1,
            max_retries=BLEConfig.TRANSIENT_READ_MAX_RETRIES,
        )

    @staticmethod
    def auto_reconnect() -> ReconnectPolicy:
        """Factory for auto-reconnect backoff policy."""
        return ReconnectPolicy(
            initial_delay=BLEConfig.AUTO_RECONNECT_INITIAL_DELAY,
            max_delay=BLEConfig.AUTO_RECONNECT_MAX_DELAY,
            backoff=BLEConfig.AUTO_RECONNECT_BACKOFF,
            jitter_ratio=BLEConfig.AUTO_RECONNECT_JITTER_RATIO,
            max_retries=None,
        )
