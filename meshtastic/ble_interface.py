"""Backward-compatible shim for the BLE interface.

The BLE implementation now lives under :mod:`meshtastic.interfaces.ble`.
This module re-exports the public API for existing import paths.
"""

from meshtastic.interfaces.ble import *  # noqa: F401,F403
from meshtastic.interfaces.ble import __all__ as _INTERFACE_ALL

__all__ = list(_INTERFACE_ALL)
