"""Utility functions for BLE operations."""

import importlib
import time
from typing import Any, Optional


def _sleep(delay: float) -> None:
    """Allow callsites to throttle activity (wrapped for easier testing)."""
    time.sleep(delay)


def resolve_ble_module() -> Optional[Any]:
    """Return the loaded BLE module (interfaces.ble or back-compat shim) if available."""
    for module_name in (
        "meshtastic.interfaces.ble",
        "meshtastic.ble_interface",
    ):
        try:
            return importlib.import_module(module_name)
        except ImportError:
            continue
    return None


def get_sleep_fn():
    """Resolve the BLE sleep function, defaulting to the local implementation."""
    ble_mod = resolve_ble_module()
    sleep_fn = getattr(ble_mod, "_sleep", None) if ble_mod else None
    return sleep_fn or _sleep
