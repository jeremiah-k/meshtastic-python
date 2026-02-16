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
import time
import weakref
from contextlib import contextmanager
from threading import RLock
from typing import Any, Dict, Generator, Optional, Set

from meshtastic.interfaces.ble.constants import BLEConfig
from meshtastic.interfaces.ble.utils import sanitize_address

logger = logging.getLogger("meshtastic.ble")

_REGISTRY_LOCK = RLock()
_ADDR_LOCKS: Dict[str, RLock] = {}
_CONNECTED_ADDRS: Set[str] = set()
# Optional owner references for active connected-address claims.
_CONNECTED_OWNERS: Dict[str, Optional["weakref.ReferenceType[Any]"]] = {}
_CONNECTED_OWNER_IDS: Dict[str, Optional[int]] = {}
_CONNECTED_MARKED_AT: Dict[str, float] = {}
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

    This function ensures a per-address RLock exists (created lazily) and increments
    its internal holder count to prevent premature cleanup. If `key` is None, the
    global registry lock is returned.

    .. warning::
        Callers that obtain the lock object but do not acquire it MUST call
        `_release_addr_lock()` when finished to decrement the holder count.
        Failure to do so will prevent cleanup of the per-address lock.

        **Prefer using `addr_lock_context()` instead** - it handles holder count
        management automatically via try/finally.

    Parameters
    ----------
        key (Optional[str]): A raw address or already-normalized address key;
            `None` selects the global registry lock.

    Returns
    -------
        RLock: The per-address lock for the normalized key, or the global
            registry lock if `key` is None.

    """
    key = _addr_key(key)
    return _get_addr_lock_by_key(key)


def _get_addr_lock_by_key(key: Optional[str]) -> RLock:
    """
    Get the process-wide RLock for an already-normalized address key.

    Parameters
    ----------
        key (Optional[str]): Normalized address key; `None` selects the global lock.

    Returns
    -------
        RLock: Per-address lock for `key`, or `_REGISTRY_LOCK` when `key` is None.

    """
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
    _release_addr_lock_by_key(key)


def _release_addr_lock_by_key(key: Optional[str]) -> None:
    """Release holder bookkeeping for an already-normalized address key."""
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

    Parameters
    ----------
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


def _owner_ref(owner: Optional[Any]) -> Optional["weakref.ReferenceType[Any]"]:
    """
    Return a weak reference for an owner object when possible.

    Parameters
    ----------
        owner (Optional[Any]): Arbitrary owner object to weak-reference.

    Returns
    -------
        Optional[weakref.ReferenceType[Any]]: Weak reference to owner, or `None`
        when owner is None or does not support weak references.

    """
    if owner is None:
        return None
    try:
        return weakref.ref(owner)
    except TypeError:
        return None


def _owner_connected_state(owner: Any) -> Optional[bool]:
    """
    Read owner connection state when exposed as either a bool attribute or callable.

    Parameters
    ----------
        owner (Any): Connected-claim owner instance.

    Returns
    -------
        Optional[bool]: Boolean connection state when available, else `None`.

    """
    connection_state = getattr(owner, "is_connection_connected", None)
    if callable(connection_state):
        try:
            connection_state = connection_state()
        except Exception:
            logger.debug(
                "Failed to read owner connection state; preserving gate claim.",
                exc_info=True,
            )
            return None

    if isinstance(connection_state, bool):
        return connection_state
    return None


def _remove_connected_record_locked(key: str) -> None:
    """
    Remove all connected-state bookkeeping for a normalized address key.

    Must be called while `_REGISTRY_LOCK` is held.
    """
    _CONNECTED_ADDRS.discard(key)
    _CONNECTED_OWNERS.pop(key, None)
    _CONNECTED_OWNER_IDS.pop(key, None)
    _CONNECTED_MARKED_AT.pop(key, None)


def _mark_connected(addr: Optional[str], owner: Optional[Any] = None) -> None:
    """
    Track that the given address is currently connected.

    The address is normalized internally using _addr_key.
    When an owner is provided, a weak reference is recorded so stale claims can
    be pruned automatically if the owner goes out of scope.

    Note: This function only marks the address as connected. The caller is
    responsible for managing the lock holder count, typically via the
    addr_lock_context() context manager which handles it automatically.
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        _CONNECTED_ADDRS.add(key)
        _CONNECTED_OWNERS[key] = _owner_ref(owner)
        _CONNECTED_OWNER_IDS[key] = id(owner) if owner is not None else None
        _CONNECTED_MARKED_AT[key] = time.monotonic()


def _mark_disconnected(addr: Optional[str], owner: Optional[Any] = None) -> None:
    """
    Track that the given address has disconnected.

    The address is normalized internally using _addr_key.
    Also attempts to clean up the address lock if no holders remain.
    If owner is provided and the connected claim belongs to a different live
    owner, the request is ignored to avoid clearing another interface's claim.

    Note: This function does NOT decrement the holder count. The caller is
    responsible for managing the lock holder count, typically via the
    addr_lock_context() context manager which handles it automatically.
    """
    key = _addr_key(addr)
    if key is None:
        return
    with _REGISTRY_LOCK:
        if owner is not None:
            owner_ref = _CONNECTED_OWNERS.get(key)
            current_owner = owner_ref() if owner_ref is not None else None
            if current_owner is not None and current_owner is not owner:
                logger.debug(
                    "Ignoring disconnect mark for %s from non-owner instance.",
                    key,
                )
                return
        _remove_connected_record_locked(key)
        _cleanup_addr_lock(key)


@contextmanager
def addr_lock_context(addr: Optional[str]) -> Generator[RLock, None, None]:
    """
    Provide a context-managed per-address lock while ensuring holder-count bookkeeping.

    Yields the RLock associated with the normalized address without acquiring it; on context exit the holder count is decremented even if an exception or early return occurs.

    Parameters
    ----------
        addr (Optional[str]): BLE address or pre-normalized address key.

    Yields
    ------
        RLock: The per-address re-entrant lock for the given address (not acquired).

    """
    key = _addr_key(addr)
    lock = _get_addr_lock_by_key(key)
    try:
        # NOTE: We intentionally yield WITHOUT acquiring the lock.
        # The caller must explicitly acquire it with `with lock:` to ensure
        # proper lock ordering discipline is visible in the code.
        yield lock
    finally:
        _release_addr_lock_by_key(key)


def _is_currently_connected_elsewhere(
    addr: Optional[str], owner: Optional[Any] = None
) -> bool:
    """
    Determine whether the given BLE address is currently marked as connected by another interface.

    Parameters
    ----------
        addr (Optional[str]): BLE address to check; it will be normalized via
            _addr_key. If normalization yields None/empty, the function returns
            False.
        owner (Optional[Any]): Current caller/owner instance. If the connected
            claim belongs to this owner, returns False (not "elsewhere").

    Returns
    -------
        True if the normalized address is marked connected by a different owner,
        False otherwise. Stale claims may be pruned during this check (owner GC,
        owner-reported disconnected state, or expired unowned claim window).

    """
    key = _addr_key(addr)
    if key is None:
        return False
    with _REGISTRY_LOCK:
        if key not in _CONNECTED_ADDRS:
            return False

        owner_ref = _CONNECTED_OWNERS.get(key)
        current_owner = owner_ref() if owner_ref is not None else None
        current_owner_id = _CONNECTED_OWNER_IDS.get(key)

        # Same owner is not "connected elsewhere".
        if owner is not None and (
            current_owner is owner
            or (current_owner_id is not None and current_owner_id == id(owner))
        ):
            return False

        # Prune stale claims when owner object was garbage collected.
        if owner_ref is not None and current_owner is None:
            _remove_connected_record_locked(key)
            _cleanup_addr_lock(key)
            return False

        # If owner exposes connection state and no longer appears connected,
        # treat the claim as stale and remove it.
        if current_owner is not None:
            connection_state = _owner_connected_state(current_owner)
            if connection_state is False:
                _remove_connected_record_locked(key)
                _cleanup_addr_lock(key)
                return False
            return True

        # Fallback for unowned claims: prune after a safety window.
        marked_at = _CONNECTED_MARKED_AT.get(key, 0.0)
        if marked_at and (
            time.monotonic() - marked_at
            > BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS
        ):
            _remove_connected_record_locked(key)
            _cleanup_addr_lock(key)
            return False

        return True
