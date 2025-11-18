# ruff: noqa: F401
"""The public API for the Meshtastic BLE interface."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    class DecodeError(Exception):
        """Fallback DecodeError type used for static type checking."""
        pass
else:  # pragma: no cover - import real exception only at runtime
    from google.protobuf.message import DecodeError

from .interfaces.ble.config import BLEConfig
from .interfaces.ble.core import BLEInterface
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
]
