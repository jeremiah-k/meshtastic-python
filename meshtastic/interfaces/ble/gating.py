"""Process-wide BLE connection gating utilities."""

from threading import RLock
from typing import Dict, Optional, Set

from meshtastic.interfaces.ble.utils import sanitize_address

_REGISTRY_LOCK = RLock()
_ADDR_LOCKS: Dict[str, RLock] = {}
_CONNECTED_ADDRS: Set[str] = set()


def _addr_key(addr: Optional[str]) -> Optional[str]:
    """
    Normalize a BLE address for registry lookups.

    Returns None for empty, None, or whitespace-only addresses to prevent
    unrelated connection attempts from sharing the same registry key.
    """
    sanitized = sanitize_address(addr)
    return sanitized if sanitized else None


def _get_addr_lock(key: Optional[str]) -> RLock:
    """
    Return a process-wide lock associated with the given normalized address.
    """
    key = _addr_key(key)
    if key is None:
        return _REGISTRY_LOCK
    with _REGISTRY_LOCK:
        lock = _ADDR_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _ADDR_LOCKS[key] = lock
        return lock


def _cleanup_addr_lock(key: Optional[str]) -> None:
    """
    Remove the lock for the given address from the registry.

    This prevents unbounded lock accumulation in long-running processes.
    The lock is removed when the address is marked as disconnected.
    """
    if key is None:
        return
    with _REGISTRY_LOCK:
        _ADDR_LOCKS.pop(key, None)


def _mark_connected(addr: Optional[str]) -> None:
    """
    Track that the given address is currently connected.

    The address is normalized internally using _addr_key.
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        _CONNECTED_ADDRS.add(key)


def _mark_disconnected(addr: Optional[str]) -> None:
    """
    Track that the given address has disconnected.

    The address is normalized internally using _addr_key.
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        _CONNECTED_ADDRS.discard(key)
        _cleanup_addr_lock(key)


def _is_currently_connected_elsewhere(addr: Optional[str]) -> bool:
    """
    Return True when the address is currently connected by another interface.

    The address is normalized internally using _addr_key.
    """
    key = _addr_key(addr)
    if key is None:
        return False
    with _REGISTRY_LOCK:
        return key in _CONNECTED_ADDRS
