"""Utility helpers shared across BLE interface modules."""

from __future__ import annotations

import asyncio
from typing import Optional

from .config import ERROR_TIMEOUT
from .errors import BLEError


def sanitize_address(address: Optional[str]) -> Optional[str]:
    """Normalize a BLE address by removing separators and lowercasing."""
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


async def with_timeout(awaitable, timeout: Optional[float], label: str):
    """Await `awaitable`, enforcing `timeout` seconds if provided."""
    if timeout is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise BLEError(ERROR_TIMEOUT.format(label, timeout)) from exc


__all__ = ["sanitize_address", "with_timeout"]
