"""
Stable BLE public API for Meshtastic.

This package intentionally exports only the same user-facing BLE symbols exposed
by `meshtastic.ble_interface` (main classes, UUID constants, BLE error strings,
and logger). Internal managers/helpers live in submodules under
`meshtastic.interfaces.ble.*` and are not part of the compatibility surface.
"""
# ruff: noqa: RUF022  # __all__ is intentionally grouped, not sorted

try:
    import bleak  # noqa: F401
except ImportError as exc:  # pragma: no cover - environment/dependency guard
    raise ImportError(
        "BLE support requires the 'bleak' package. Install dependencies with "
        "`poetry install` (or `pip install meshtastic`)."
    ) from exc

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_ASYNC_TIMEOUT,
    ERROR_CONNECTION_FAILED,
    ERROR_MULTIPLE_DEVICES,
    ERROR_NO_PERIPHERAL_FOUND,
    ERROR_NO_PERIPHERALS_FOUND,
    ERROR_READING_BLE,
    ERROR_TIMEOUT,
    ERROR_WRITING_BLE,
    FROMNUM_UUID,
    FROMRADIO_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    SERVICE_UUID,
    TORADIO_UUID,
    BLEConfig,
    logger,
)
from meshtastic.interfaces.ble.interface import BLEInterface

__all__ = [
    # Main classes
    "BLEInterface",
    "BLEClient",
    "BLEConfig",
    # UUID constants
    "SERVICE_UUID",
    "TORADIO_UUID",
    "FROMRADIO_UUID",
    "FROMNUM_UUID",
    "LEGACY_LOGRADIO_UUID",
    "LOGRADIO_UUID",
    # Error messages
    "ERROR_TIMEOUT",
    "ERROR_MULTIPLE_DEVICES",
    "ERROR_READING_BLE",
    "ERROR_NO_PERIPHERAL_FOUND",
    "ERROR_WRITING_BLE",
    "ERROR_CONNECTION_FAILED",
    "ERROR_NO_PERIPHERALS_FOUND",
    "BLECLIENT_ERROR_ASYNC_TIMEOUT",
    # Utility
    "logger",
]
