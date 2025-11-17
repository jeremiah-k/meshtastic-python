"""Backward-compatible entry point for the BLE interface."""

import sys
from typing import List

from meshtastic.interfaces.ble import interface as _interface
from meshtastic.interfaces.ble.interface import BLEInterface

if hasattr(_interface, "__all__"):
    __all__ = list(_interface.__all__)  # type: ignore[attr-defined]
else:
    __all__ = [name for name in dir(_interface) if not name.startswith("_")]

# Re-export the implementation module so imports continue to work and
# attribute updates/monkeypatches apply to the real implementation.
sys.modules[__name__] = _interface
