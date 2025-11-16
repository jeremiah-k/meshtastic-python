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
    logger,
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
# Preserve the historical meshtastic.ble_interface surface (minus private helpers)
# so downstream consumers can migrate incrementally.
from .state import BLEStateManager, ConnectionState

__all__ = sorted(
    [
        "AUTO_RECONNECT_BACKOFF",
        "AUTO_RECONNECT_INITIAL_DELAY",
        "AUTO_RECONNECT_JITTER_RATIO",
        "AUTO_RECONNECT_MAX_DELAY",
        "BLEClient",
        "BLEConfig",
        "BLEDevice",
        "BLEError",
        "BLEErrorHandler",
        "BLEInterface",
        "BLEStateManager",
        "BLE_SCAN_TIMEOUT",
        "BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION",
        "BLEAK_VERSION",
        "BleakRootClient",
        "BleakScanner",
        "ClientManager",
        "CONNECTION_TIMEOUT",
        "ConnectedStrategy",
        "ConnectionOrchestrator",
        "ConnectionState",
        "ConnectionValidator",
        "DISCONNECT_TIMEOUT_SECONDS",
        "DiscoveryManager",
        "EMPTY_READ_MAX_RETRIES",
        "EMPTY_READ_RETRY_DELAY",
        "EMPTY_READ_WARNING_COOLDOWN",
        "ERROR_CONNECTION_FAILED",
        "ERROR_MULTIPLE_DEVICES",
        "ERROR_NO_PERIPHERAL_FOUND",
        "ERROR_NO_PERIPHERALS_FOUND",
        "ERROR_READING_BLE",
        "ERROR_TIMEOUT",
        "ERROR_WRITING_BLE",
        "EVENT_THREAD_JOIN_TIMEOUT",
        "FROMNUM_UUID",
        "FROMRADIO_UUID",
        "LEGACY_LOGRADIO_UUID",
        "LOGRADIO_UUID",
        "logger",
        "MALFORMED_NOTIFICATION_THRESHOLD",
        "NotificationManager",
        "RECEIVE_THREAD_JOIN_TIMEOUT",
        "RECEIVE_WAIT_TIMEOUT",
        "ReconnectPolicy",
        "ReconnectScheduler",
        "ReconnectWorker",
        "RetryPolicy",
        "SERVICE_UUID",
        "SEND_PROPAGATION_DELAY",
        "ThreadCoordinator",
        "TORADIO_UUID",
        "TRANSIENT_READ_MAX_RETRIES",
        "TRANSIENT_READ_RETRY_DELAY",
        "current_thread",
        "publishingThread",
    ]
)
