"""Utility functions for BLE operations."""

import importlib
import time
from typing import Any, Optional


def _sleep(delay: float) -> None:
    """
    Throttle execution for the given duration in seconds.
    
    Parameters:
        delay (float): Duration to sleep, in seconds.
    """
    time.sleep(delay)


def resolve_ble_module() -> Optional[Any]:
    """
    Locate a loaded BLE module used by the package.
    
    Searches known module names ("meshtastic.interfaces.ble" then "meshtastic.ble_interface") and returns the first successfully imported module.
    
    Returns:
        The imported BLE module if found, `None` otherwise.
    """
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
    """
    Select the sleep function to use, preferring a BLE module-provided `_sleep` when available.
    
    Returns:
        sleep_fn (Callable[[float], None]): The BLE module's `_sleep` function if present; otherwise the local `_sleep` implementation.
    """
    ble_mod = resolve_ble_module()
    sleep_fn = getattr(ble_mod, "_sleep", None) if ble_mod else None
    return sleep_fn or _sleep