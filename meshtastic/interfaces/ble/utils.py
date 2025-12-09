"""Utility functions for BLE operations."""

import importlib
import re
import time
from typing import Any, Optional, Tuple, cast


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


def _parse_version_triplet(version_str: str) -> Tuple[int, int, int]:
    """
    Extract a three-part integer version tuple from a version string.

    Non-numeric segments are ignored and missing components are treated as zeros.
    """
    matches = re.findall(r"\d+", version_str or "")
    while len(matches) < 3:
        matches.append("0")
    try:
        return cast(
            Tuple[int, int, int],
            tuple(int(segment) for segment in matches[:3]),
        )
    except ValueError:
        return 0, 0, 0


def _bleak_supports_connected_fallback() -> bool:
    """
    Check whether the installed bleak version meets the minimum required version for the connected-device fallback.
    """
    from meshtastic.interfaces.ble.constants import (
        BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION,
        BLEAK_VERSION,
    )

    return (
        _parse_version_triplet(BLEAK_VERSION)
        >= BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION
    )
