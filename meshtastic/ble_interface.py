"""Backward-compatible entry point for the BLE interface."""

import sys

from meshtastic.interfaces.ble import interface as _interface
from meshtastic.interfaces.ble.interface import BLEInterface

# Re-export the implementation module to preserve backwards compatibility for imports and monkeypatching.
sys.modules[__name__] = _interface
