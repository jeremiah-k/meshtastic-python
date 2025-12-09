"""BLE constants and configuration."""

import importlib.metadata
import logging
from typing import Optional, Tuple

from meshtastic.interfaces.ble.utils import (
    _bleak_supports_connected_fallback,
    _parse_version_triplet,
)

logger = logging.getLogger("meshtastic.ble")

# Get bleak version using importlib.metadata (reliable method)
BLEAK_VERSION = importlib.metadata.version("bleak")

# BLE Service and Characteristic UUIDs
SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"
LEGACY_LOGRADIO_UUID = "6c6fd238-78fa-436b-aacf-15c5be1ef2e2"
LOGRADIO_UUID = "5a3d6e49-06e6-4423-9944-e9de8cdf9547"
MALFORMED_NOTIFICATION_THRESHOLD = 10

# Timeout constants
DISCONNECT_TIMEOUT_SECONDS = 5.0
RECEIVE_THREAD_JOIN_TIMEOUT = 2.0
EVENT_THREAD_JOIN_TIMEOUT = 2.0

# BLE timeout and retry constants
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
    BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT = 2.0

# Backwards-compatible aliases for legacy module-level constants
BLE_SCAN_TIMEOUT = BLEConfig.BLE_SCAN_TIMEOUT
RECEIVE_WAIT_TIMEOUT = BLEConfig.RECEIVE_WAIT_TIMEOUT
EMPTY_READ_RETRY_DELAY = BLEConfig.EMPTY_READ_RETRY_DELAY
EMPTY_READ_MAX_RETRIES = BLEConfig.EMPTY_READ_MAX_RETRIES
TRANSIENT_READ_MAX_RETRIES = BLEConfig.TRANSIENT_READ_MAX_RETRIES
TRANSIENT_READ_RETRY_DELAY = BLEConfig.TRANSIENT_READ_RETRY_DELAY
SEND_PROPAGATION_DELAY = BLEConfig.SEND_PROPAGATION_DELAY
GATT_IO_TIMEOUT = BLEConfig.GATT_IO_TIMEOUT
NOTIFICATION_START_TIMEOUT = BLEConfig.NOTIFICATION_START_TIMEOUT
CONNECTION_TIMEOUT = BLEConfig.CONNECTION_TIMEOUT
EMPTY_READ_WARNING_COOLDOWN = BLEConfig.EMPTY_READ_WARNING_COOLDOWN
AUTO_RECONNECT_INITIAL_DELAY = BLEConfig.AUTO_RECONNECT_INITIAL_DELAY
AUTO_RECONNECT_MAX_DELAY = BLEConfig.AUTO_RECONNECT_MAX_DELAY
AUTO_RECONNECT_BACKOFF = BLEConfig.AUTO_RECONNECT_BACKOFF
AUTO_RECONNECT_JITTER_RATIO = BLEConfig.AUTO_RECONNECT_JITTER_RATIO
BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION = (
    BLEConfig.BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION
)

# Error message constants
ERROR_TIMEOUT = "{0} timed out after {1:.1f} seconds"
ERROR_MULTIPLE_DEVICES = (
    "Multiple Meshtastic BLE peripherals found matching '{0}'. Please specify one:\n{1}"
)

# Error message constants
ERROR_READING_BLE = "Error reading BLE"
ERROR_NO_PERIPHERAL_FOUND = "No Meshtastic BLE peripheral with identifier or address '{0}' found. Try --ble-scan to find it."

ERROR_WRITING_BLE = (
    "Error writing BLE. This is often caused by missing Bluetooth "
    "permissions (e.g. not being in the 'bluetooth' group) or pairing issues."
)
ERROR_CONNECTION_FAILED = "Connection failed: {0}"
ERROR_NO_PERIPHERALS_FOUND = (
    "No Meshtastic BLE peripherals found. Try --ble-scan to find them."
)

# BLEClient-specific constants
# Alias preserves legacy access while sourcing value from BLEConfig
BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT = BLEConfig.BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT
BLECLIENT_ERROR_ASYNC_TIMEOUT = "Async operation timed out"
