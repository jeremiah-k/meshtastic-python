"""Utility functions for BLE operations."""

import importlib
import time
from types import ModuleType
from typing import Callable, Optional


def sanitize_address(address: Optional[str]) -> Optional[str]:
    """
    Normalize a BLE address or identifier by removing common separators and converting to lowercase.
    
    Strips leading and trailing whitespace; if the resulting string is empty or the input is None, returns None. Removes hyphens, underscores, colons, and spaces before lowercasing.
    
    Parameters:
        address (Optional[str]): Address or identifier to normalize.
    
    Returns:
        Optional[str]: Normalized address string, or None if `address` is None or empty after stripping.
    """
    if address is None:
        return None
    stripped = address.strip()
    if not stripped:
        return None
    return (
        stripped.replace("-", "")
        .replace("_", "")
        .replace(":", "")
        .replace(" ", "")
        .lower()
    )


def _sleep(delay: float) -> None:
    """
    Pause execution for the specified number of seconds.
    
    Parameters:
        delay (float): Time to sleep, in seconds.
    """
    time.sleep(delay)


def resolve_ble_module() -> Optional[ModuleType]:
    """
    Locate and return the first importable BLE module used by the package.
    
    Attempts to import, in order, "meshtastic.interfaces.ble" then "meshtastic.ble_interface" and returns the first successfully imported module.
    
    Returns:
        The imported `ModuleType` for the first module that could be imported, `None` if neither module is available.
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


def get_sleep_fn() -> Callable[[float], None]:
    """
    Selects the sleep function to use, preferring a BLE module-provided `_sleep` when available.
    
    Returns:
        Callable[[float], None]: The BLE module's `_sleep` function if available, otherwise the local `_sleep` implementation.
    """
    ble_mod = resolve_ble_module()
    sleep_fn = getattr(ble_mod, "_sleep", None) if ble_mod else None
    return sleep_fn or _sleep