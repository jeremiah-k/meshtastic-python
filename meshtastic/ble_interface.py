# ruff: noqa: F401, F403
"""The public API for the Meshtastic BLE interface."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    class DecodeError(Exception):
        """Fallback DecodeError type used for static type checking."""
        pass
else:  # pragma: no cover - import real exception only at runtime
    from google.protobuf.message import DecodeError

from .interfaces.ble.config import *
from .interfaces.ble.state import *
from .interfaces.ble.reconnect import *
from .interfaces.ble.core import *
from .interfaces.ble.client import *
from .interfaces.ble.discovery import *
from .interfaces.ble.gatt import *
from .interfaces.ble.util import *