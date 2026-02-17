"""Utility functions for BLE operations."""

import importlib
import time
from types import ModuleType
from typing import Optional


def sanitize_address(address: Optional[str]) -> Optional[str]:
    """
    Normalize a BLE address or identifier by removing common separators and converting to lowercase.

    Strips leading and trailing whitespace; if the resulting string is empty or the input is None, returns None. Removes hyphens, underscores, colons, and spaces before lowercasing.

    Parameters
    ----------
        address (Optional[str]): Address or identifier to normalize.

    Returns
    -------
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
    Pause execution for a given number of seconds.

    Parameters
    ----------
        delay (float): Number of seconds to sleep; may be fractional.

    """
    time.sleep(delay)


def resolve_ble_module() -> Optional[ModuleType]:
    """
    Locate the first available BLE module for the package.
    
    Attempts to import "meshtastic.interfaces.ble" then "meshtastic.ble_interface" in order and returns the first module that can be imported.
    
    Returns:
        The imported module as a ModuleType if found, `None` otherwise.
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