"""Process-wide BLE connection gating utilities.

Lock Ordering Note:
    When acquiring multiple locks, always acquire in this order:
    1. _REGISTRY_LOCK (global registry lock)
    2. Per-address locks from _ADDR_LOCKS
    3. Interface-level locks (_state_lock, _connect_lock, _disconnect_lock)

    This ordering prevents deadlocks in concurrent connection scenarios.
"""

import logging
from threading import RLock
from typing import Dict, Optional, Set

from meshtastic.interfaces.ble.utils import sanitize_address

logger = logging.getLogger("meshtastic.ble")

_REGISTRY_LOCK = RLock()
_ADDR_LOCKS: Dict[str, RLock] = {}
_CONNECTED_ADDRS: Set[str] = set()
# Track locks that are currently held to prevent premature cleanup
_LOCK_HOLDERS: Dict[str, int] = {}  # key -> count of holders


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

    The lock is reference-counted to prevent premature cleanup while in use.
    Callers must call _release_addr_lock() when done with the lock if they
    don't actually acquire it, or ensure _mark_disconnected() is called after
    the connection attempt completes.
    """
    key = _addr_key(key)
    if key is None:
        return _REGISTRY_LOCK
    with _REGISTRY_LOCK:
        lock = _ADDR_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _ADDR_LOCKS[key] = lock
            _LOCK_HOLDERS[key] = 0
        # Increment holder count to prevent premature cleanup
        _LOCK_HOLDERS[key] = _LOCK_HOLDERS.get(key, 0) + 1
        return lock


def _release_addr_lock(key: Optional[str]) -> None:
    """
    Decrement the holder count for an address lock.

    This should be called when a caller obtained a lock via _get_addr_lock()
    but did not actually acquire it (e.g., early return due to already connected).
    """
    key = _addr_key(key)
    if key is None:
        return
    with _REGISTRY_LOCK:
        if key in _LOCK_HOLDERS:
            _LOCK_HOLDERS[key] = max(0, _LOCK_HOLDERS[key] - 1)


def _cleanup_addr_lock(key: Optional[str]) -> None:
    """
    Remove the lock for the given address from the registry if no holders remain.

    This prevents unbounded lock accumulation in long-running processes.
    The lock is only removed when:
    1. The address is marked as disconnected
    2. No threads are currently holding the lock

    This prevents the race condition where a lock is removed while another
    thread is waiting to acquire it, which would allow duplicate connections.
    """
    if key is None:
        return
    with _REGISTRY_LOCK:
        # Only cleanup if no holders remain
        holder_count = _LOCK_HOLDERS.get(key, 0)
        if holder_count <= 0:
            _ADDR_LOCKS.pop(key, None)
            _LOCK_HOLDERS.pop(key, None)
            logger.debug("Cleaned up address lock for %s", key)
        else:
            logger.debug(
                "Skipping cleanup of address lock for %s (holders: %d)",
                key,
                holder_count,
            )


def _mark_connected(addr: Optional[str]) -> None:
    """
    Track that the given address is currently connected.

    The address is normalized internally using _addr_key.
    Also decrements the lock holder count since the connection attempt is complete.
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        _CONNECTED_ADDRS.add(key)
        # Decrement holder count since connection attempt is complete
        if key in _LOCK_HOLDERS:
            _LOCK_HOLDERS[key] = max(0, _LOCK_HOLDERS[key] - 1)


def _mark_disconnected(addr: Optional[str]) -> None:
    """
    Track that the given address has disconnected.

    The address is normalized internally using _addr_key.
    Also attempts to clean up the address lock if no holders remain.
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        _CONNECTED_ADDRS.discard(key)
        # Decrement holder count if not already done
        if key in _LOCK_HOLDERS:
            _LOCK_HOLDERS[key] = max(0, _LOCK_HOLDERS[key] - 1)
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
