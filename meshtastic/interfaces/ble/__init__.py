"""BLE interface package exports."""

from bleak import BLEDevice
from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner
from threading import current_thread
from meshtastic import publishingThread

from .client import BLEClient
from .config import (
    BLEConfig,
    BLEAK_VERSION,
    DISCONNECT_TIMEOUT_SECONDS,
    EVENT_THREAD_JOIN_TIMEOUT,
    RECEIVE_THREAD_JOIN_TIMEOUT,
    FROMNUM_UUID,
    FROMRADIO_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    MALFORMED_NOTIFICATION_THRESHOLD,
    SERVICE_UUID,
    TORADIO_UUID,
    ReconnectPolicy,
    RetryPolicy,
    _sleep,
    ERROR_CONNECTION_FAILED,
    ERROR_MULTIPLE_DEVICES,
    ERROR_NO_PERIPHERAL_FOUND,
    ERROR_NO_PERIPHERALS_FOUND,
    ERROR_READING_BLE,
    ERROR_TIMEOUT,
    ERROR_WRITING_BLE,
)
from .connection import ClientManager, ConnectionOrchestrator, ConnectionValidator
from .coordination import ThreadCoordinator
from .discovery import ConnectedStrategy, DiscoveryManager
from .errors import BLEError, BLEErrorHandler
from .interface import (
    BLEInterface,
    AUTO_RECONNECT_BACKOFF,
    AUTO_RECONNECT_INITIAL_DELAY,
    AUTO_RECONNECT_JITTER_RATIO,
    AUTO_RECONNECT_MAX_DELAY,
    BLE_SCAN_TIMEOUT,
    BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION,
    CONNECTION_TIMEOUT,
    EMPTY_READ_MAX_RETRIES,
    EMPTY_READ_RETRY_DELAY,
    EMPTY_READ_WARNING_COOLDOWN,
    RECEIVE_WAIT_TIMEOUT,
    SEND_PROPAGATION_DELAY,
    TRANSIENT_READ_MAX_RETRIES,
    TRANSIENT_READ_RETRY_DELAY,
    _bleak_supports_connected_fallback,
)
from .notifications import NotificationManager
from .reconnect import ReconnectScheduler, ReconnectWorker
from .state import BLEStateManager, ConnectionState

__all__ = [
    "BLEDevice",
    "BleakScanner",
    "BleakRootClient",
    "current_thread",
    "publishingThread",
    "BLEClient",
    "BLEConfig",
    "BLEInterface",
    "BLEStateManager",
    "BLEError",
    "BLEErrorHandler",
    "ConnectionState",
    "ReconnectPolicy",
    "RetryPolicy",
    "ClientManager",
    "ConnectionOrchestrator",
    "ConnectionValidator",
    "ThreadCoordinator",
    "DiscoveryManager",
    "ConnectedStrategy",
    "NotificationManager",
    "ReconnectScheduler",
    "ReconnectWorker",
    "BLEAK_VERSION",
    "SERVICE_UUID",
    "TORADIO_UUID",
    "FROMRADIO_UUID",
    "FROMNUM_UUID",
    "LEGACY_LOGRADIO_UUID",
    "LOGRADIO_UUID",
    "DISCONNECT_TIMEOUT_SECONDS",
    "EVENT_THREAD_JOIN_TIMEOUT",
    "RECEIVE_THREAD_JOIN_TIMEOUT",
    "MALFORMED_NOTIFICATION_THRESHOLD",
    "ERROR_TIMEOUT",
    "ERROR_READING_BLE",
    "ERROR_WRITING_BLE",
    "ERROR_CONNECTION_FAILED",
    "ERROR_NO_PERIPHERALS_FOUND",
    "ERROR_NO_PERIPHERAL_FOUND",
    "ERROR_MULTIPLE_DEVICES",
    "_sleep",
    "BLE_SCAN_TIMEOUT",
    "RECEIVE_WAIT_TIMEOUT",
    "EMPTY_READ_RETRY_DELAY",
    "EMPTY_READ_MAX_RETRIES",
    "TRANSIENT_READ_MAX_RETRIES",
    "TRANSIENT_READ_RETRY_DELAY",
    "SEND_PROPAGATION_DELAY",
    "CONNECTION_TIMEOUT",
    "EMPTY_READ_WARNING_COOLDOWN",
    "AUTO_RECONNECT_INITIAL_DELAY",
    "AUTO_RECONNECT_MAX_DELAY",
    "AUTO_RECONNECT_BACKOFF",
    "AUTO_RECONNECT_JITTER_RATIO",
    "BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION",
    "_bleak_supports_connected_fallback",
]
