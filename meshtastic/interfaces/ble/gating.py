"""Process-wide BLE connection gating utilities.

Lock Ordering Note:
    When acquiring multiple locks, always acquire in this order:
    1. _REGISTRY_LOCK (global registry lock)
    2. Per-address locks from _ADDR_LOCKS
    3. Interface-level locks (_state_lock, _connect_lock, _disconnect_lock)

    This ordering prevents deadlocks in concurrent connection scenarios.

Context Manager Usage:
    Prefer using addr_lock_context() over manual _get_addr_lock()/_release_addr_lock()
    calls to ensure proper holder count management:

        with addr_lock_context(address) as lock:
            with lock:
                # Connection logic here
                # On success: _mark_connected(address) handles holder count
                # On failure/exception: context manager releases holder count
"""

import logging
from contextlib import contextmanager
from threading import RLock
from typing import Dict, Generator, Optional, Set

from meshtastic.interfaces.ble.utils import sanitize_address

logger = logging.getLogger("meshtastic.ble")

_REGISTRY_LOCK = RLock()
_ADDR_LOCKS: Dict[str, RLock] = {}
_CONNECTED_ADDRS: Set[str] = set()
# Track locks that are currently held to prevent premature cleanup
_LOCK_HOLDERS: Dict[str, int] = {}  # key -> count of holders


def _addr_key(addr: Optional[str]) -> Optional[str]:
    """
    Normalize a BLE address into a registry key.
    
    Returns:
        The normalized address string, or `None` if `addr` is `None`, empty, or contains only whitespace.
    """
    sanitized = sanitize_address(addr)
    return sanitized if sanitized else None


def _get_addr_lock(key: Optional[str]) -> RLock:
    """
    Get the process-wide RLock associated with the given normalized address.
    
    This function ensures a per-address RLock exists (created lazily) and increments its internal holder count to prevent premature cleanup. If `key` is None, the global registry lock is returned. Callers that obtain the lock object but do not acquire it must call `_release_addr_lock()` when finished to decrement the holder count.
    
    Parameters:
        key (Optional[str]): A raw address or already-normalized address key; `None` selects the global registry lock.
    
    Returns:
        RLock: The per-address lock for the normalized key, or the global registry lock if `key` is None.
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
    If the holder count reaches zero and the address is not currently marked
    connected, the per-address lock entry is removed immediately.
    """
    key = _addr_key(key)
    if key is None:
        return
    with _REGISTRY_LOCK:
        if key in _LOCK_HOLDERS:
            _LOCK_HOLDERS[key] = max(0, _LOCK_HOLDERS[key] - 1)
            if _LOCK_HOLDERS[key] <= 0 and key not in _CONNECTED_ADDRS:
                _ADDR_LOCKS.pop(key, None)
                _LOCK_HOLDERS.pop(key, None)
                logger.debug("Cleaned up address lock for %s", key)


def _cleanup_addr_lock(key: Optional[str]) -> None:
    """
    Remove the per-address lock from the registry when there are no remaining holders.
    
    If `key` is None this function is a no-op. When `key` is provided, the registry entry
    for that address (both the lock and its holder count) is removed only if the tracked
    holder count is less than or equal to zero; otherwise the lock is left in place.
    
    Parameters:
        key (Optional[str]): Normalized address key identifying the per-address lock,
            or `None` to indicate no target (no action).
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

    Note: This function only marks the address as connected. The caller is
    responsible for managing the lock holder count, typically via the
    addr_lock_context() context manager which handles it automatically.
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
    Also attempts to clean up the address lock if no holders remain.

    Note: This function does NOT decrement the holder count. The caller is
    responsible for managing the lock holder count, typically via the
    addr_lock_context() context manager which handles it automatically.
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        _CONNECTED_ADDRS.discard(key)
        _cleanup_addr_lock(key)


@contextmanager
def addr_lock_context(addr: Optional[str]) -> Generator[RLock, None, None]:
    """
    Provide a context-managed per-address lock while ensuring holder-count bookkeeping.
    
    Yields the RLock associated with the normalized address without acquiring it; on context exit the holder count is decremented even if an exception or early return occurs.
    
    Parameters:
        addr (Optional[str]): BLE address or pre-normalized address key.
    
    Yields:
        RLock: The per-address re-entrant lock for the given address (not acquired).
    """
    lock = _get_addr_lock(addr)
    try:
        # NOTE: We intentionally yield WITHOUT acquiring the lock.
        # The caller must explicitly acquire it with `with lock:` to ensure
        # proper lock ordering discipline is visible in the code.
        yield lock
    finally:
        _release_addr_lock(addr)


def _is_currently_connected_elsewhere(addr: Optional[str]) -> bool:
    """
    Determine whether the given BLE address is currently marked as connected by any interface.
    
    Parameters:
        addr (Optional[str]): BLE address to check; it will be normalized via _addr_key. If normalization yields None/empty, the function returns False.
    
    Returns:
        True if the normalized address is marked connected, False otherwise.
    """
    key = _addr_key(addr)
    if key is None:
        return False
    with _REGISTRY_LOCK:
        return key in _CONNECTED_ADDRS
