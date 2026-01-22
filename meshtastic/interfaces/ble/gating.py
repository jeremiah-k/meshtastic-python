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
    if key is None:
        return _REGISTRY_LOCK
    with _REGISTRY_LOCK:
        lock = _ADDR_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _ADDR_LOCKS[key] = lock
        return lock


def _cleanup_addr_lock(key: str) -> None:
    """
    Remove the lock for the given address from the registry.

    This prevents unbounded lock accumulation in long-running processes.
    The lock is removed when the address is marked as disconnected.
    """
    with _REGISTRY_LOCK:
        _ADDR_LOCKS.pop(key, None)


def _mark_connected(key: Optional[str]) -> None:
    """
    Track that the given normalized address is currently connected.
    """
    if key is None:
        return
    with _REGISTRY_LOCK:
        _CONNECTED_ADDRS.add(key)


def _mark_disconnected(key: Optional[str]) -> None:
    """
    Track that the given normalized address has disconnected.
    """
    if key is None:
        return
    with _REGISTRY_LOCK:
        _CONNECTED_ADDRS.discard(key)
        _cleanup_addr_lock(key)


def _is_currently_connected_elsewhere(key: Optional[str]) -> bool:
    """
    Return True when the address is currently connected by another interface.
    """
    if key is None:
        return False
    with _REGISTRY_LOCK:
        return key in _CONNECTED_ADDRS
