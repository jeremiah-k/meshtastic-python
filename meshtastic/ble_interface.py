# ruff: noqa: F401
"""The public API for the Meshtastic BLE interface."""
from typing import TYPE_CHECKING

from bleak import BLEDevice
from .interfaces.ble.connection import ConnectionValidator

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
from .interfaces.ble.reconnect import ReconnectPolicy, RetryPolicy
from .interfaces.ble.state import BLEStateManager, ConnectionState
from .interfaces.ble.client import BLEClient

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
]
