# ruff: noqa: F401
"""The public API for the Meshtastic BLE interface."""

from typing import TYPE_CHECKING

from bleak.backends.device import BLEDevice
from .interfaces.ble.connection import ConnectionValidator
from .interfaces.ble.discovery import ConnectedStrategy, DiscoveryManager

if TYPE_CHECKING:

    class DecodeError(Exception):
        """Fallback DecodeError type used for static type checking."""

        pass
else:  # pragma: no cover - import real exception only at runtime
    from google.protobuf.message import DecodeError

from .interfaces.ble.config import BLEConfig
from .interfaces.ble.core import BLEInterface
from .interfaces.ble.exceptions import BLEError
from .interfaces.ble.gatt import (
    FROMNUM_UUID,
    FROMRADIO_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    SERVICE_UUID,
    TORADIO_UUID,
)
from .interfaces.ble.reconnect import (
    ReconnectPolicy,
    RetryPolicy,
    ReconnectScheduler,
    ReconnectWorker,
)
from .interfaces.ble.state import BLEStateManager, ConnectionState
from .interfaces.ble.client import BLEClient
from .interfaces.ble.util import (
    BLEErrorHandler,
    _sleep,
    _bleak_supports_connected_fallback,
)
from meshtastic import publishingThread
from threading import current_thread

__all__ = [
    "BLEInterface",
    "BLEStateManager",
    "ConnectionState",
    "BLEConfig",
    "ReconnectPolicy",
    "RetryPolicy",
    "BLEClient",
    "DecodeError",
    "BLEError",
    "FROMNUM_UUID",
    "FROMRADIO_UUID",
    "LEGACY_LOGRADIO_UUID",
    "LOGRADIO_UUID",
    "SERVICE_UUID",
    "TORADIO_UUID",
    "BLEDevice",
    "ConnectionValidator",
    "ConnectedStrategy",
    "DiscoveryManager",
    "ReconnectScheduler",
    "ReconnectWorker",
]

# Expose module-level attributes for backward compatibility with tests
BLEErrorHandler = BLEErrorHandler
publishingThread = publishingThread
current_thread = current_thread
_sleep = _sleep
_bleak_supports_connected_fallback = _bleak_supports_connected_fallback

# Also expose BleakScanner for tests that expect it at module level
from bleak import BleakScanner
