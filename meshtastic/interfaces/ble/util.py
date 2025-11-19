"""BLE utility functions."""

import asyncio
import inspect
import re
from typing import Any, Dict, Optional, Tuple, cast

from bleak import BLEDevice

from .config import BLEAK_VERSION, BLEConfig
from .exceptions import BLEError


ERROR_TIMEOUT = "{0} timed out after {1:.1f} seconds"


def _parse_version_triplet(version_str: str) -> Tuple[int, int, int]:
    """
    Extract a three-part integer version tuple from version_str.
    
    Non-numeric segments are ignored; the first three numeric groups are converted to integers and missing components are treated as zeros. If conversion fails, returns (0, 0, 0).
    
    Parameters:
        version_str (str): Version string to parse.
    
    Returns:
        Tuple[int, int, int]: A (major, minor, patch) integer triplet parsed from the input.
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


def bleak_supports_connected_fallback() -> bool:
    """
    Determine if the installed Bleak version meets the minimum required for the connected-device fallback.
    
    Returns:
        `true` if the current Bleak version is greater than or equal to BLEConfig.BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION, `false` otherwise.
    """
    return (
        _parse_version_triplet(BLEAK_VERSION)
        >= BLEConfig.BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION
    )


async def with_timeout(awaitable, timeout: Optional[float], label: str):
    """
    Await an awaitable and enforce a timeout if provided.
    
    Parameters:
        awaitable: The awaitable to await.
        timeout (Optional[float]): Maximum time in seconds to wait; if None, wait indefinitely.
        label (str): Identifier inserted into the timeout error message.
    
    Returns:
        The result produced by the awaitable.
    
    Raises:
        BLEError: if the awaitable does not complete within `timeout` seconds.
    """
    if timeout is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise BLEError(ERROR_TIMEOUT.format(label, timeout)) from exc


def sanitize_address(address: Optional[str]) -> Optional[str]:
    """
    Normalize a BLE address or identifier by removing common separators and lowercasing it.
    
    Parameters:
        address (Optional[str]): A BLE address or identifier; may be None or whitespace-only.
    
    Returns:
        Optional[str]: The address trimmed, with all '-', '_', ':', and spaces removed and lowercased, or `None` if `address` is None or contains only whitespace.
    """
    if address is None or not address.strip():
        return None
    return (
        address.strip()
        .replace("-", "")
        .replace("_", "")
        .replace(":", "")
        .replace(" ", "")
        .lower()
    )


_BLE_DEVICE_SIGNATURE = inspect.signature(BLEDevice.__init__)


def build_ble_device(
    address: str, name: Optional[str], details: Dict[str, Any], rssi: int
) -> BLEDevice:
    """
    Create a BLEDevice instance using whichever constructor parameters the installed bleak supports.
    
    Parameters:
        address (str): Device address or identifier.
        name (Optional[str]): Device name.
        details (Dict[str, Any]): Backend-specific details; included only if the installed bleak's BLEDevice accepts `details`.
        rssi (int): Signal strength; included only if the installed bleak's BLEDevice accepts `rssi`.
    
    Returns:
        BLEDevice: A BLEDevice instance constructed with the available fields based on the bleak BLEDevice constructor signature.
    """
    params: Dict[str, Any] = {"address": address, "name": name}
    if "details" in _BLE_DEVICE_SIGNATURE.parameters:
        params["details"] = details
    if "rssi" in _BLE_DEVICE_SIGNATURE.parameters:
        params["rssi"] = rssi
    return BLEDevice(**params)