"""Shared configuration, constants, and retry policies for the BLE interface."""

from __future__ import annotations

import importlib.metadata
import logging
import random
import re
import time
from typing import Optional, Tuple, cast

logger = logging.getLogger("meshtastic.ble")

# Get bleak version using importlib.metadata (reliable method)
BLEAK_VERSION = importlib.metadata.version("bleak")

SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"
LEGACY_LOGRADIO_UUID = "6c6fd238-78fa-436b-aacf-15c5be1ef2e2"
LOGRADIO_UUID = "5a3d6e49-06e6-4423-9944-e9de8cdf9547"
MALFORMED_NOTIFICATION_THRESHOLD = 10

DISCONNECT_TIMEOUT_SECONDS = 5.0
RECEIVE_THREAD_JOIN_TIMEOUT = 2.0
EVENT_THREAD_JOIN_TIMEOUT = 2.0


class BLEConfig:
    """Configuration constants for BLE operations."""

    BLE_SCAN_TIMEOUT = 10.0
    RECEIVE_WAIT_TIMEOUT = 0.5
    EMPTY_READ_RETRY_DELAY = 0.1
    EMPTY_READ_MAX_RETRIES = 5
    TRANSIENT_READ_MAX_RETRIES = 3
    TRANSIENT_READ_RETRY_DELAY = 0.1
    SEND_PROPAGATION_DELAY = 0.01
    GATT_IO_TIMEOUT = 10.0
    NOTIFICATION_START_TIMEOUT: Optional[float] = 10.0
    CONNECTION_TIMEOUT = 60.0
    EMPTY_READ_WARNING_COOLDOWN = 10.0
    AUTO_RECONNECT_INITIAL_DELAY = 1.0
    AUTO_RECONNECT_MAX_DELAY = 30.0
    AUTO_RECONNECT_BACKOFF = 2.0
    AUTO_RECONNECT_JITTER_RATIO = 0.15
    BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION: Tuple[int, int, int] = (1, 1, 0)


def _sleep(delay: float) -> None:
    """Allow callsites to throttle activity (wrapped for easier testing)."""
    time.sleep(delay)


class ReconnectPolicy:
    """Unified reconnection / retry policy with jittered exponential backoff."""

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
        """Compute the jittered delay for the provided attempt."""
        if attempt is None:
            attempt = self._attempt_count
        delay = min(self.initial_delay * (self.backoff**attempt), self.max_delay)
        jitter = delay * self.jitter_ratio * (self._random.random() * 2.0 - 1.0)
        return delay + jitter

    def should_retry(self, attempt: Optional[int] = None) -> bool:
        """Determine whether another retry should be attempted."""
        if attempt is None:
            attempt = self._attempt_count
        return self.max_retries is None or attempt < self.max_retries

    def next_attempt(self) -> Tuple[float, bool]:
        """Advance attempt counter and return (delay, should_retry)."""
        delay = self.get_delay()
        should_retry = self.should_retry()
        self._attempt_count += 1
        return delay, should_retry

    def get_attempt_count(self) -> int:
        """Expose the current attempt count (primarily for logging)."""
        return self._attempt_count

    def sleep_with_backoff(self, attempt: int) -> None:
        """Sleep for the jittered delay associated with the supplied attempt."""
        _sleep(self.get_delay(attempt))


class RetryPolicy:
    """Static retry policy presets for BLE operations."""

    EMPTY_READ = ReconnectPolicy(
        initial_delay=BLEConfig.EMPTY_READ_RETRY_DELAY,
        max_delay=1.0,
        backoff=1.5,
        jitter_ratio=0.1,
        max_retries=BLEConfig.EMPTY_READ_MAX_RETRIES,
    )

    TRANSIENT_ERROR = ReconnectPolicy(
        initial_delay=BLEConfig.TRANSIENT_READ_RETRY_DELAY,
        max_delay=2.0,
        backoff=1.5,
        jitter_ratio=0.1,
        max_retries=BLEConfig.TRANSIENT_READ_MAX_RETRIES,
    )

    AUTO_RECONNECT = ReconnectPolicy(
        initial_delay=BLEConfig.AUTO_RECONNECT_INITIAL_DELAY,
        max_delay=BLEConfig.AUTO_RECONNECT_MAX_DELAY,
        backoff=BLEConfig.AUTO_RECONNECT_BACKOFF,
        jitter_ratio=BLEConfig.AUTO_RECONNECT_JITTER_RATIO,
        max_retries=None,
    )


ERROR_TIMEOUT = "{0} timed out after {1:.1f} seconds"
ERROR_MULTIPLE_DEVICES = (
    "Multiple Meshtastic BLE peripherals found matching '{0}'. Please specify one:\n{1}"
)
ERROR_READING_BLE = "Error reading BLE"
ERROR_NO_PERIPHERAL_FOUND = (
    "No Meshtastic BLE peripheral with identifier or address '{0}' found. Try --ble-scan to find it."
)
ERROR_WRITING_BLE = (
    "Error writing BLE. This is often caused by missing Bluetooth "
    "permissions (e.g. not being in the 'bluetooth' group) or pairing issues."
)
ERROR_CONNECTION_FAILED = "Connection failed: {0}"
ERROR_NO_PERIPHERALS_FOUND = (
    "No Meshtastic BLE peripherals found. Try --ble-scan to find them."
)

BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT = 2.0
BLECLIENT_ERROR_ASYNC_TIMEOUT = "Async operation timed out"


def bleak_supports_connected_fallback() -> bool:
    """Return True if the installed bleak version supports connected-device fallback."""
    return (
        _parse_version_triplet(BLEAK_VERSION)
        >= BLEConfig.BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION
    )


def _parse_version_triplet(version_str: str) -> Tuple[int, int, int]:
    """Extract a three-part integer version tuple from `version_str`."""
    matches = re.findall(r"\d+", version_str or "")
    while len(matches) < 3:
        matches.append("0")
    try:
        return cast(
            Tuple[int, int, int],
            tuple(int(segment) for segment in matches[:3]),
        )
    except ValueError:
        return 0, 0, 0


__all__ = [
    "BLEConfig",
    "BLEAK_VERSION",
    "BLECLIENT_ERROR_ASYNC_TIMEOUT",
    "BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT",
    "DISCONNECT_TIMEOUT_SECONDS",
    "EVENT_THREAD_JOIN_TIMEOUT",
    "ERROR_CONNECTION_FAILED",
    "ERROR_MULTIPLE_DEVICES",
    "ERROR_NO_PERIPHERAL_FOUND",
    "ERROR_NO_PERIPHERALS_FOUND",
    "ERROR_READING_BLE",
    "ERROR_TIMEOUT",
    "ERROR_WRITING_BLE",
    "FROMNUM_UUID",
    "FROMRADIO_UUID",
    "LEGACY_LOGRADIO_UUID",
    "LOGRADIO_UUID",
    "MALFORMED_NOTIFICATION_THRESHOLD",
    "RECEIVE_THREAD_JOIN_TIMEOUT",
    "ReconnectPolicy",
    "RetryPolicy",
    "SERVICE_UUID",
    "TORADIO_UUID",
    "_sleep",
    "bleak_supports_connected_fallback",
    "logger",
]
