# ruff: noqa: RUF022  # __all__ is intentionally grouped, not sorted

"""Backwards compatibility layer for BLE interface.

This module provides a stable public API for the BLE interface.
Internal implementation classes are available from meshtastic.interfaces.ble
but should not be considered part of the stable public API.
"""

# Historical module-level imports retained for compatibility with code that
# imported Bleak symbols from meshtastic.ble_interface in the pre-refactor API.
# COMPAT_STABLE_SHIM
try:
    from bleak import (  # type: ignore[attr-defined]  # noqa: F401  # pylint: disable=unused-import
        BleakClient,
        BleakScanner,
        BLEDevice,
    )
    from bleak.exc import (  # noqa: F401  # pylint: disable=unused-import
        BleakDBusError,
        BleakError,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
    if exc.name != "bleak":
        raise
    raise ImportError(  # noqa: TRY003
        "BLE support requires the 'bleak' package, but it is missing. "
        "Your Meshtastic installation appears incomplete; reinstall dependencies "
        "with `poetry install` (or `pip install --upgrade meshtastic`)."
    ) from exc

# Public API re-export: import the canonical BLE facade once and mirror its
# declared public surface from __all__ into this shim namespace.
from meshtastic.interfaces import ble as _ble
from meshtastic.interfaces.ble import *  # noqa: F403  # pylint: disable=wildcard-import,unused-wildcard-import

_BLE_PUBLIC_ALL = tuple(getattr(_ble, "__all__", ()))

# Stable public API delegates to the canonical BLE facade export list.
__all__ = list(_BLE_PUBLIC_ALL)
