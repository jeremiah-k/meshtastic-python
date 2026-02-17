# ruff: noqa: RUF022  # __all__ is intentionally grouped, not sorted

"""Backwards compatibility layer for BLE interface.

This module provides a stable public API for the BLE interface.
Internal implementation classes are available from meshtastic.interfaces.ble
but should not be considered part of the stable public API.
"""

# Public API - only export what users actually need
from meshtastic.interfaces.ble import (  # Main classes; UUID constants (for custom operations); Error messages (for error handling); Utility
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
    BLEClient,
    BLEConfig,
    BLEInterface,
    logger,
)

# Stable public API - only what external code needs
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
