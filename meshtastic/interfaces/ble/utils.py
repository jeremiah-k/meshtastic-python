"""Utility functions for BLE operations."""

import asyncio
import importlib
import time
from types import ModuleType
from typing import Awaitable, Callable, TypeVar
from unittest.mock import DEFAULT, Mock, NonCallableMock

T = TypeVar("T")


def _is_unconfigured_mock_member(candidate: object) -> bool:
    """Return whether a candidate is an unconfigured auto-generated mock member.

    Parameters
    ----------
    candidate : object
        Candidate object to inspect.

    Returns
    -------
    bool
        True when the object is a mock member with default return behavior,
        no side effect, and no recorded calls.
    """
    if not isinstance(candidate, (Mock, NonCallableMock)):
        return False
    return (
        getattr(candidate, "_mock_return_value", DEFAULT) is DEFAULT
        and getattr(candidate, "_mock_wraps", None) is None
        and getattr(candidate, "side_effect", None) is None
        and not getattr(candidate, "call_args_list", [])
    )


def _is_unconfigured_mock_callable(candidate: object) -> bool:
    """Return whether a candidate is an unconfigured callable auto-generated mock.

    Parameters
    ----------
    candidate : object
        Candidate object to inspect.

    Returns
    -------
    bool
        True when candidate is callable and also matches
        ``_is_unconfigured_mock_member``.
    """
    return callable(candidate) and _is_unconfigured_mock_member(candidate)


def _thread_start_probe(thread: object) -> tuple[int | None, bool]:
    """Return normalized thread start indicators for compatibility-safe checks.

    Parameters
    ----------
    thread : object
        Thread-like object exposing optional ``ident`` and ``is_alive`` members.

    Returns
    -------
    tuple[int | None, bool]
        Normalized ``(ident, is_alive)`` where ``ident`` is ``None`` unless it
        is an ``int``, and ``is_alive`` is ``False`` unless ``is_alive()``
        returns a real ``bool``.
    """
    thread_ident = getattr(thread, "ident", None)
    if thread_ident is not None and not isinstance(thread_ident, int):
        thread_ident = None
    is_alive = getattr(thread, "is_alive", None)
    alive_result = is_alive() if callable(is_alive) else False
    thread_is_alive = alive_result if isinstance(alive_result, bool) else False
    return thread_ident, thread_is_alive


def sanitize_address(address: str | None) -> str | None:
    """Normalize a BLE address or identifier by removing common separators and converting to lowercase.

    Parameters
    ----------
    address : str | None
        Address or identifier to normalize.

    Returns
    -------
    str | None
        Normalized address string with separators removed and lowercased,
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
    """Pause execution for the specified number of seconds.

    Parameters
    ----------
    delay : float
        Number of seconds to pause; may be fractional (e.g., 0.5).
    """
    time.sleep(delay)


async def with_timeout(
    awaitable: Awaitable[T],
    timeout: float | None,
    label: str,
    timeout_error_factory: Callable[[str, float], Exception] | None = None,
) -> T:
    """Run an awaitable with an optional timeout.

    Parameters
    ----------
    awaitable : Awaitable[T]
        The awaitable to execute.
    timeout : float | None
        Maximum seconds to wait; None means wait indefinitely.
    label : str
        Short operation label passed to the timeout_error_factory when a timeout occurs.
    timeout_error_factory : Callable[[str, float], Exception] | None
        Optional factory that receives (label, timeout)
        and returns the exception to raise when the operation times out. (Default value = None)

    Returns
    -------
    T
        The result produced by the awaitable.

    Raises
    ------
    asyncio.TimeoutError
        If the timeout elapses and no timeout_error_factory is provided.
    Exception
        The exception returned by timeout_error_factory(label, timeout) when a timeout occurs.
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
    """Locate and return the first available BLE-related module for the package.

    Checks for available modules in priority order and returns the first successfully imported module.

    Returns
    -------
    ModuleType | None
        The imported BLE module if found, otherwise `None`.
    """
    for module_name in (
        "meshtastic.interfaces.ble",
        "meshtastic.ble_interface",
    ):
        try:
            return importlib.import_module(module_name)
        except ImportError as exc:
            # Continue when the target module (or one of its parent packages)
            # is missing; re-raise transitive import failures from inside it.
            missing_name = getattr(exc, "name", None)
            if missing_name is None or (
                missing_name != module_name
                and not module_name.startswith(f"{missing_name}.")
            ):
                raise
            continue
    return None
