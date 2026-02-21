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

    Parameters
    ----------
        address (str | None): Address or identifier to normalize.

    Returns
    -------
        str | None: Normalized address string with hyphens, underscores, colons, and spaces removed and lowercased, or None if `address` is None or empty after stripping.

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
    Block execution for the specified number of seconds.

    Parameters
    ----------
        delay (float): Time to sleep in seconds; may be fractional.

    """
    time.sleep(delay)


async def with_timeout(
    awaitable: Awaitable[T],
    timeout: float | None,
    label: str,
    timeout_error_factory: Callable[[str, float], Exception] | None = None,
) -> T:
    """
    Await an awaitable with an optional timeout.

    Parameters
    ----------
        awaitable (Awaitable[T]): Awaitable to run.
        timeout (float | None): Maximum seconds to wait; if None, wait indefinitely.
        label (str): Short operation label used by timeout_error_factory.
        timeout_error_factory (Callable[[str, float], Exception] | None): Optional factory
            used to map timeout to a specific exception type.

    Returns
    -------
        T: The awaitable result.

    Raises
    ------
        Exception: Raises asyncio.TimeoutError on timeout unless timeout_error_factory is supplied.

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
    Locate the first available BLE module for the package.

    Attempts to import "meshtastic.interfaces.ble" then "meshtastic.ble_interface" in order and returns the first module that can be imported.

    Returns
    -------
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
