"""Backward-compatible shim for the BLE interface.

The BLE implementation now lives under :mod:`meshtastic.interfaces.ble`.
This module re-exports the public API for existing import paths.
"""

from meshtastic.interfaces.ble import *  # noqa: F403
from meshtastic.interfaces.ble import (
    __all__ as _INTERFACE_ALL,
    _bleak_supports_connected_fallback as _bleak_supports_connected_fallback,
    _sleep as _sleep,
    logger as logger,
)

__all__ = [*_INTERFACE_ALL, "_sleep", "_bleak_supports_connected_fallback", "logger"]
