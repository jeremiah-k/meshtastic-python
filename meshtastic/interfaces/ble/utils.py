"""Utility functions for BLE operations."""

import importlib
import time
from types import ModuleType
from typing import Callable, Optional


def sanitize_address(address: Optional[str]) -> Optional[str]:
    """
    Normalize BLE addresses or identifiers by removing common separators and lowercasing.

    Parameters
    ----------
    address : Any
        Address or identifier to normalize; may be None or consist only of whitespace.

    Returns
    -------
        Optional[str]: Normalized address string, or None if the input is None or only whitespace.
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
    Throttle execution for the given duration in seconds.

    Parameters
    ----------
    delay : Any
        Duration to sleep, in seconds.
    """
    time.sleep(delay)


def resolve_ble_module() -> Optional[ModuleType]:
    """
    Locate a loaded BLE module used by the package.

    Searches known module names ("meshtastic.interfaces.ble" then "meshtastic.ble_interface") and returns the first successfully imported module.

    Returns
    -------
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


def get_sleep_fn() -> Callable[[float], None]:
    """
    Select the sleep function to use, preferring a BLE module-provided `_sleep` when available.

    Returns
    -------
        sleep_fn (Callable[[float], None]): The BLE module's `_sleep` function if present; otherwise the local `_sleep` implementation.
    """
    ble_mod = resolve_ble_module()
    sleep_fn = getattr(ble_mod, "_sleep", None) if ble_mod else None
    return sleep_fn or _sleep
