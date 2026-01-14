"""Process-wide BLE connection gating utilities."""

from threading import RLock
from typing import Dict, Optional, Set

from meshtastic.interfaces.ble.utils import sanitize_address

_REGISTRY_LOCK = RLock()
_ADDR_LOCKS: Dict[str, RLock] = {}
_CONNECTED_ADDRS: Set[str] = set()


def _addr_key(addr: Optional[str]) -> str:
    """
    Normalize a BLE address for registry lookups.
    """
    return sanitize_address(addr) or ""


def _get_addr_lock(key: str) -> RLock:
    """
    Return a process-wide lock associated with the given normalized address.
    """
    with _REGISTRY_LOCK:
        lock = _ADDR_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _ADDR_LOCKS[key] = lock
        return lock


def _mark_connected(key: str) -> None:
    """
    Track that the given normalized address is currently connected.
    """
    with _REGISTRY_LOCK:
        _CONNECTED_ADDRS.add(key)


def _mark_disconnected(key: str) -> None:
    """
    Track that the given normalized address has disconnected.
    """
    with _REGISTRY_LOCK:
        _CONNECTED_ADDRS.discard(key)


def _is_currently_connected_elsewhere(key: str) -> bool:
    """
    Return True when the address is currently connected by another interface.
    """
    with _REGISTRY_LOCK:
        return key in _CONNECTED_ADDRS
