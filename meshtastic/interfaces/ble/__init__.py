"""BLE interface package for Meshtastic."""
# ruff: noqa: RUF022  # __all__ is intentionally grouped, not sorted

# Explicit imports to preserve backwards-compatible surface (including private helpers).
from meshtastic.interfaces.ble.constants import (
    AUTO_RECONNECT_BACKOFF,
    AUTO_RECONNECT_INITIAL_DELAY,
    AUTO_RECONNECT_JITTER_RATIO,
    AUTO_RECONNECT_MAX_DELAY,
    BLECLIENT_ERROR_ASYNC_TIMEOUT,
    BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT,
    BLEConfig,
    BLE_SCAN_TIMEOUT,
    BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION,
    BLEAK_VERSION,
    CONNECTION_TIMEOUT,
    DISCONNECT_TIMEOUT_SECONDS,
    EMPTY_READ_MAX_RETRIES,
    EMPTY_READ_RETRY_DELAY,
    EMPTY_READ_WARNING_COOLDOWN,
    EVENT_THREAD_JOIN_TIMEOUT,
    FROMNUM_UUID,
    FROMRADIO_UUID,
    GATT_IO_TIMEOUT,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    MALFORMED_NOTIFICATION_THRESHOLD,
    NOTIFICATION_START_TIMEOUT,
    ERROR_TIMEOUT,
    ERROR_MULTIPLE_DEVICES,
    ERROR_READING_BLE,
    ERROR_NO_PERIPHERAL_FOUND,
    ERROR_WRITING_BLE,
    ERROR_CONNECTION_FAILED,
    ERROR_NO_PERIPHERALS_FOUND,
    RECEIVE_THREAD_JOIN_TIMEOUT,
    RECEIVE_WAIT_TIMEOUT,
    SEND_PROPAGATION_DELAY,
    SERVICE_UUID,
    TORADIO_UUID,
    TRANSIENT_READ_MAX_RETRIES,
    TRANSIENT_READ_RETRY_DELAY,
    _bleak_supports_connected_fallback,
    _parse_version_triplet,
    logger,
)
from bleak import BleakScanner, BLEDevice
from threading import current_thread

from meshtastic.interfaces.ble.state import ConnectionState, BLEStateManager
from meshtastic.interfaces.ble.coordination import ThreadCoordinator
from meshtastic.interfaces.ble.errors import BLEErrorHandler
from meshtastic.interfaces.ble.policies import ReconnectPolicy, RetryPolicy
from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.discovery import (
    DiscoveryStrategy,
    ConnectedStrategy,
    DiscoveryManager,
)
from meshtastic.interfaces.ble.connection import (
    ConnectionValidator,
    ClientManager,
    ConnectionOrchestrator,
)
from meshtastic.interfaces.ble.reconnection import ReconnectScheduler, ReconnectWorker
from meshtastic.interfaces.ble.notifications import NotificationManager
from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.interfaces.ble.utils import _sleep

__all__ = [
    # Core classes
    "BLEConfig",
    "ConnectionState",
    "BLEStateManager",
    "ThreadCoordinator",
    "BLEErrorHandler",
    "ReconnectPolicy",
    "RetryPolicy",
    "BLEClient",
    "DiscoveryStrategy",
    "ConnectedStrategy",
    "DiscoveryManager",
    "ConnectionValidator",
    "ClientManager",
    "ConnectionOrchestrator",
    "ReconnectScheduler",
    "ReconnectWorker",
    "NotificationManager",
    "BLEInterface",
    "BleakScanner",
    "BLEDevice",
    "current_thread",
    # Constants/helpers
    "SERVICE_UUID",
    "TORADIO_UUID",
    "FROMRADIO_UUID",
    "FROMNUM_UUID",
    "LEGACY_LOGRADIO_UUID",
    "LOGRADIO_UUID",
    "MALFORMED_NOTIFICATION_THRESHOLD",
    "DISCONNECT_TIMEOUT_SECONDS",
    "RECEIVE_THREAD_JOIN_TIMEOUT",
    "EVENT_THREAD_JOIN_TIMEOUT",
    "ERROR_TIMEOUT",
    "ERROR_MULTIPLE_DEVICES",
    "ERROR_READING_BLE",
    "ERROR_NO_PERIPHERAL_FOUND",
    "ERROR_WRITING_BLE",
    "ERROR_CONNECTION_FAILED",
    "ERROR_NO_PERIPHERALS_FOUND",
    "BLE_SCAN_TIMEOUT",
    "RECEIVE_WAIT_TIMEOUT",
    "EMPTY_READ_RETRY_DELAY",
    "EMPTY_READ_MAX_RETRIES",
    "TRANSIENT_READ_MAX_RETRIES",
    "TRANSIENT_READ_RETRY_DELAY",
    "SEND_PROPAGATION_DELAY",
    "GATT_IO_TIMEOUT",
    "NOTIFICATION_START_TIMEOUT",
    "CONNECTION_TIMEOUT",
    "EMPTY_READ_WARNING_COOLDOWN",
    "AUTO_RECONNECT_INITIAL_DELAY",
    "AUTO_RECONNECT_MAX_DELAY",
    "AUTO_RECONNECT_BACKOFF",
    "AUTO_RECONNECT_JITTER_RATIO",
    "BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION",
    "BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT",
    "BLECLIENT_ERROR_ASYNC_TIMEOUT",
    "BLEAK_VERSION",
    # Private helpers (exported for backwards compatibility)
    "_parse_version_triplet",
    "_bleak_supports_connected_fallback",
    "_sleep",
    "logger",
]
