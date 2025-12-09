"""BLE interface package for Meshtastic."""

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
from meshtastic.interfaces.ble.state import *
from meshtastic.interfaces.ble.coordination import *
from meshtastic.interfaces.ble.errors import *
from meshtastic.interfaces.ble.policies import *
from meshtastic.interfaces.ble.client import *
from meshtastic.interfaces.ble.discovery import *
from meshtastic.interfaces.ble.connection import *
from meshtastic.interfaces.ble.reconnection import *
from meshtastic.interfaces.ble.notifications import *
from meshtastic.interfaces.ble.interface import *
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
    "_parse_version_triplet",
    "_bleak_supports_connected_fallback",
    "_sleep",
    "logger",
]
