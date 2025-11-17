"""BLE utility functions."""

import asyncio
import re
from typing import Optional, Tuple, cast

from .config import BLEAK_VERSION, BLEConfig
from .exceptions import BLEError


ERROR_TIMEOUT = "{0} timed out after {1:.1f} seconds"


def _parse_version_triplet(version_str: str) -> Tuple[int, int, int]:
    """
    Extract a three-part integer version tuple from `version_str`.

    This helper is intentionally permissive — non-numeric segments are ignored and
    missing components are treated as zeros.
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
    Determine whether the installed bleak version supports the connected-device fallback.
    """
    return (
        _parse_version_triplet(BLEAK_VERSION)
        >= BLEConfig.BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION
    )


async def with_timeout(awaitable, timeout: Optional[float], label: str):
    """
    Await `awaitable`, enforcing `timeout` seconds if provided.

    Raises
    ------
        BLEError: when the awaitable does not finish before the timeout elapses.

    """
    if timeout is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise BLEError(ERROR_TIMEOUT.format(label, timeout)) from exc


def sanitize_address(address: Optional[str]) -> Optional[str]:
    """
    Normalize a BLE address by removing common separators and converting to lowercase.

    Args:
    ----
        address (Optional[str]): BLE address or identifier; may be None or empty/whitespace.

    Returns:
    -------
        Optional[str]: The normalized address with all "-", "_", ":" removed, trimmed of surrounding whitespace,
            and lowercased, or `None` if `address` is None or contains only whitespace.

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
