"""Process-wide BLE connection gating utilities.

Lock Ordering Note:
    When acquiring multiple locks, always acquire in this order:
    1. _REGISTRY_LOCK (global registry lock)
    2. Per-address locks from _ADDR_LOCKS
    3. Interface-level locks (_state_lock, _connect_lock, _disconnect_lock)

    This ordering prevents deadlocks in concurrent connection scenarios.

Context Manager Usage:
    Prefer using _addr_lock_context() over manual _get_addr_lock()/_release_addr_lock()
    calls to ensure proper holder count management:

        with _addr_lock_context(address) as lock:
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
    
    Parameters:
        addr (Optional[str]): The BLE address to normalize; may be None, empty, or whitespace.
    
    Returns:
        Optional[str]: The sanitized address string, or None if the input is None, empty, or contains only whitespace.
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

        **Prefer using `_addr_lock_context()` instead** - it handles holder count
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
    Return the process-wide reentrant lock associated with a normalized address key.
    
    If `key` is None, the global registry lock is returned. For per-address locks, this function increments an internal holder count for the returned lock to prevent its premature cleanup.
    
    Parameters:
        key (Optional[str]): Normalized address key; `None` selects the global registry lock.
    
    Returns:
        RLock: The per-address reentrant lock for `key`, or the global registry lock when `key` is None.
    """
    if key is None:
        # The registry lock is a process-wide singleton and is not ref-counted.
        return _REGISTRY_LOCK
    with _REGISTRY_LOCK:
        lock = _ADDR_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _ADDR_LOCKS[key] = lock
        # Increment holder count to prevent premature cleanup
        _LOCK_HOLDERS[key] = _LOCK_HOLDERS.get(key, 0) + 1
        return lock


def _release_addr_lock(key: Optional[str]) -> None:
    """
    Decrement the holder count for the per-address lock identified by key.
    
    The input is normalized to a registry key; passing None refers to the registry-level lock. If the holder count reaches zero and the address is not currently marked connected, the per-address lock entry may be removed as part of cleanup.
    Parameters:
        key (Optional[str]): An address or already-normalized key identifying the per-address lock; use None for the registry.
    """
    key = _addr_key(key)
    _release_addr_lock_by_key(key)


def _release_addr_lock_by_key(key: Optional[str]) -> None:
    """
    Decrement the holder count for a normalized address key and remove its lock when unused.

    If `key` is None this is a no-op. Decrements the internal holder count (never below zero); if the count reaches zero and the address is not marked connected, removes the per-address lock and its holder bookkeeping entry.

    Parameters
    ----------
        key (Optional[str]): Normalized address registry key (or None).

    """
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
    Create a weak reference to the given owner if supported.
    
    Parameters:
        owner (Optional[Any]): Object to create a weak reference for.
    
    Returns:
        Optional[weakref.ReferenceType[Any]]: A weak reference to `owner`, or `None` if `owner` is `None` or does not support weak references.
    """
    if owner is None:
        return None
    try:
        return weakref.ref(owner)
    except TypeError:
        return None


def _owner_connected_state(owner: Any) -> Optional[bool]:
    """
    Obtain an owner's connection state exposed via an `is_connection_connected` attribute or callable.

    If the owner exposes `is_connection_connected` as a callable it will be invoked; exceptions during invocation result in `None`. If the attribute is a boolean, that value is returned.

    Parameters
    ----------
        owner (Any): Object that may expose an `is_connection_connected` boolean or callable.

    Returns
    -------
        Optional[bool]: `True` if the owner reports connected, `False` if it reports disconnected, or `None` if not available or on error invoking the callable.

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
    Record that a BLE address is connected and capture optional owner metadata.
    
    If `addr` is None, empty, or whitespace-only this function is a no-op. When a valid
    address is provided it is normalized to an internal key and the registry is updated
    to mark the address as connected, store a weak reference to `owner` when possible,
    store the owner's identity (`id(owner)`) when provided, and record the monotonic
    timestamp when the connection was marked.
    
    Parameters:
        addr (Optional[str]): The BLE address to mark connected; may be None or empty (no-op).
        owner (Optional[Any]): Optional owner object associated with the connection; a weak reference
            is stored when the object supports weak references and the owner's `id` is recorded.
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
    Mark an address as disconnected and remove its connection bookkeeping.
    
    Normalizes `addr` and, if valid, under the registry lock clears the recorded connected state and triggers per-address lock cleanup. If `owner` is provided, the disconnect only proceeds when the live recorded owner matches `owner`; otherwise the disconnect is ignored. This function does not decrement per-address lock holder counts — callers are responsible for holder bookkeeping.
    
    Parameters:
        addr (Optional[str]): Address to mark disconnected; will be normalized internally. If `None` or empty, the call is a no-op.
        owner (Optional[Any]): Optional owner object; when provided, the recorded live owner must be the same object for the disconnect to be applied.
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
def _addr_lock_context(addr: Optional[str]) -> Generator[RLock, None, None]:
    """
    Provide the per-address reentrant lock for a normalized BLE address key while managing its internal holder count.
    
    The context yields the per-address RLock without acquiring it; on context exit the holder count is decremented so the lock can be cleaned up when no longer used.
    
    Parameters:
        addr (Optional[str]): BLE address or already-normalized address key; None selects the global registry lock.
    
    Yields:
        RLock: The per-address reentrant lock for the given address (not acquired).
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
    Check whether a BLE address is currently recorded as connected by a different owner.

    Normalizes the provided address; if normalization yields None the function returns False.
    If an owner is provided and matches the recorded owner (by identity or recorded id), the address is not considered "connected elsewhere". Stale claims are pruned during the check when the recorded owner has been garbage-collected, reports it is disconnected, or an unowned claim has expired.

    Parameters
    ----------
        addr (Optional[str]): BLE address to check; will be normalized via _addr_key.
        owner (Optional[Any]): The caller's owner instance used to determine whether a recorded claim belongs to the caller.

    Returns
    -------
        True if the normalized address is marked connected by a different owner, False otherwise.

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