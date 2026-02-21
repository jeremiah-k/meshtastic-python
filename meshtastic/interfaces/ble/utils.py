"""Utility functions for BLE operations."""

import asyncio
import importlib
import time
from types import ModuleType
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


def sanitize_address(address: str | None) -> str | None:
    """
    Normalize a BLE address or identifier by removing common separators and converting to lowercase.

    Args:
        address (str | None): Address or identifier to normalize.

    Returns:
        str | None: Normalized address string with separators removed and lowercased,
            or `None` if `address` is `None` or empty after stripping.
    """
    if address is None:
        return None
    stripped = address.strip()
    if not stripped:
        return None
    cleaned = (
        stripped.replace("-", "")
        .replace("_", "")
        .replace(":", "")
        .replace(" ", "")
        .lower()
    )
    return cleaned if cleaned else None


def _sleep(delay: float) -> None:
    """
    Pause execution for the specified number of seconds.

    Parameters:
        delay (float): Number of seconds to pause; may be fractional (e.g., 0.5).
    """
    time.sleep(delay)


async def with_timeout(
    awaitable: Awaitable[T],
    timeout: float | None,
    label: str,
    timeout_error_factory: Callable[[str, float], Exception] | None = None,
) -> T:
    """
    Run an awaitable with an optional timeout.

    Parameters:
        awaitable (Awaitable[T]): The awaitable to execute.
        timeout (float | None): Maximum seconds to wait; None means wait indefinitely.
        label (str): Short operation label passed to the timeout_error_factory when a timeout occurs.
        timeout_error_factory (Callable[[str, float], Exception] | None): Optional factory that receives (label, timeout)
            and returns the exception to raise when the operation times out.

    Returns:
        T: The result produced by the awaitable.

    Raises:
        asyncio.TimeoutError: If the timeout elapses and no timeout_error_factory is provided.
        Exception: The exception returned by timeout_error_factory(label, timeout) when a timeout occurs.
    """
    if timeout is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        if timeout_error_factory is None:
            raise
        raise timeout_error_factory(label, timeout) from exc


def resolve_ble_module() -> ModuleType | None:
    """
    Locate and return the first available BLE-related module for the package.

    Checks for available modules in priority order and returns the first successfully imported module.

    Returns:
        ModuleType | None: The imported BLE module if found, otherwise `None`.
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
