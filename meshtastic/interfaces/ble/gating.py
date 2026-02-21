"""Process-wide BLE connection gating utilities.

Lock Ordering Note:
    When acquiring multiple locks, always acquire in this order:
    1. _REGISTRY_LOCK (global registry lock)
    2. Per-address locks from _ADDR_LOCKS (via _addr_lock_context)
    3. Interface connect lock (_connect_lock)
    4. Interface state lock (_state_lock)
    5. Interface disconnect lock (_disconnect_lock)

    This ordering prevents deadlocks in concurrent connection scenarios.

Context Manager Usage:
    Prefer using _addr_lock_context() over manual _get_addr_lock()/_release_addr_lock()
    calls to ensure proper holder count management:

        with _addr_lock_context(address) as lock:
            with lock:
                # Connection logic here
                # On success: _mark_connected(address) records the address in _CONNECTED_ADDRS
                # (it does not touch _LOCK_HOLDERS), which prevents _release_addr_lock_by_key
                # from removing the lock entry when the context exits.
                # On failure/exception: context manager releases holder count
"""

import logging
import time
import weakref
from contextlib import contextmanager
from threading import RLock
from typing import Any, Generator

from meshtastic.interfaces.ble.constants import BLEConfig
from meshtastic.interfaces.ble.utils import sanitize_address

logger = logging.getLogger("meshtastic.ble")

_REGISTRY_LOCK = RLock()
_ADDR_LOCKS: dict[str, RLock] = {}
_CONNECTED_ADDRS: set[str] = set()
# Optional owner references for active connected-address claims.
_CONNECTED_OWNERS: dict[str, weakref.ReferenceType[Any] | None] = {}
_CONNECTED_OWNER_IDS: dict[str, int | None] = {}
_CONNECTED_MARKED_AT: dict[str, float] = {}
# Track locks that are currently held to prevent premature cleanup
_LOCK_HOLDERS: dict[str, int] = {}  # key -> count of holders


def _clear_all_registries() -> None:
    """Reset all gating registries (internal helper)."""
    with _REGISTRY_LOCK:
        _ADDR_LOCKS.clear()
        _CONNECTED_ADDRS.clear()
        _CONNECTED_MARKED_AT.clear()
        _CONNECTED_OWNER_IDS.clear()
        _CONNECTED_OWNERS.clear()
        _LOCK_HOLDERS.clear()


def clearAllRegistries() -> None:
    """Public alias for _clear_all_registries (used in tests)."""
    _clear_all_registries()


def _addr_key(addr: str | None) -> str | None:
    """
    Internal helper: Normalize a BLE address into a registry key.

    Parameters
    ----------
        addr (str | None): The BLE address to normalize; may be None, empty, or whitespace.

    Returns
    -------
        str | None: The sanitized address string, or None if the input is None, empty, or contains only whitespace.

    """
    sanitized = sanitize_address(addr)
    return sanitized if sanitized else None


def _get_addr_lock(key: str | None) -> RLock:
    """
    Get the process-wide reentrant lock for a normalized address key.

    If `key` is None the global registry lock is returned. When a per-address lock is returned,
    its internal holder count is incremented to prevent premature cleanup; callers that obtain
    the lock object but do not acquire it must call `_release_addr_lock()` when finished.

    Parameters
    ----------
        key (str | None): A raw or already-normalized address key; `None` selects the global registry lock.

    Returns
    -------
        RLock: The per-address reentrant lock for `key`, or the global registry lock if `key` is None.

    """
    key = _addr_key(key)
    return _get_addr_lock_by_key(key)


def _get_addr_lock_by_key(key: str | None) -> RLock:
    """
    Get the process-wide reentrant lock for a normalized address key.

    If `key` is None, returns the global registry lock. For per-address locks, ensures a lock exists and increments an internal holder count to prevent premature cleanup.

    Parameters
    ----------
        key (str | None): Normalized address key; `None` selects the global registry lock.

    Returns
    -------
        RLock: The reentrant lock for `key`, or the global registry lock when `key` is `None`.

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


def _release_addr_lock(key: str | None) -> None:
    """
    Decrement the holder count for the per-address lock identified by key.

    If key is None, operates on the global registry lock. The input is normalized to a registry key; when the holder count reaches zero and the address is not marked connected, the per-address lock entry may be removed by cleanup logic.

    Parameters
    ----------
        key (str | None): An address or already-normalized key identifying the per-address lock; use None for the registry.

    """
    key = _addr_key(key)
    _release_addr_lock_by_key(key)


def _release_addr_lock_by_key(key: str | None) -> None:
    """
    Decrement the holder count for a normalized address key and remove its lock when unused.

    If `key` is None this is a no-op. Decrements the internal holder count (never below zero); if the count reaches zero and the address is not marked connected, removes the per-address lock and its holder bookkeeping entry.

    Parameters
    ----------
        key (str | None): Normalized address registry key (or None).

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


def _cleanup_addr_lock(key: str | None) -> None:
    """
    Remove a per-address lock from the registry when there are no remaining holders.

    If `key` is None this function does nothing. When `key` is provided, the registry entries for that address (the lock and its holder count) are removed only if the tracked holder count is less than or equal to zero; otherwise the entries are left intact.

    Parameters
    ----------
        key (str | None): Normalized address key identifying the per-address lock, or `None` to indicate no action.

    """
    if key is None:
        return
    with _REGISTRY_LOCK:
        # Only cleanup if no holders remain
        holder_count = _LOCK_HOLDERS.get(key, 0)
        if holder_count <= 0 and key not in _CONNECTED_ADDRS:
            _ADDR_LOCKS.pop(key, None)
            _LOCK_HOLDERS.pop(key, None)
            logger.debug("Cleaned up address lock for %s", key)
        else:
            logger.debug(
                "Skipping cleanup of address lock for %s (holders: %d)",
                key,
                holder_count,
            )


def _owner_ref(owner: Any | None) -> weakref.ReferenceType[Any] | None:
    """
    Return a weak reference to `owner` if it is provided and supports weak references.

    Returns
    -------
        weakref.ReferenceType[Any] | None: A weak reference to `owner`, or `None` if `owner` is `None` or not weak-referenceable.

    """
    if owner is None:
        return None
    try:
        return weakref.ref(owner)
    except TypeError:
        logger.debug(
            "Owner is not weak-referenceable; using id() fallback for gate ownership: %r",
            owner,
        )
        return None


def _owner_connected_state(owner: Any) -> bool | None:
    """
    Obtain an owner's connection state exposed via an `_is_connection_connected` attribute or callable.

    If the owner exposes `_is_connection_connected` as a callable it will be invoked; exceptions during invocation result in `None`. If the attribute is a boolean, that value is returned.

    Parameters
    ----------
        owner (Any): Object that may expose an `_is_connection_connected` boolean or callable.

    Returns
    -------
        bool | None: `True` if the owner reports connected, `False` if it reports disconnected, or `None` if not available or on error invoking the callable.

    """
    connection_state = getattr(owner, "_is_connection_connected", None)
    if callable(connection_state):
        try:
            connection_state = connection_state()
        except (AttributeError, TypeError, RuntimeError):
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
    Remove all connected-state bookkeeping associated with the normalized address key.

    Parameters
    ----------
        key (str): Normalized address key whose connected-state records should be removed.

    Notes
    -----
        Must be called while holding the module-level `_REGISTRY_LOCK`.

    """
    _CONNECTED_ADDRS.discard(key)
    _CONNECTED_OWNERS.pop(key, None)
    _CONNECTED_OWNER_IDS.pop(key, None)
    _CONNECTED_MARKED_AT.pop(key, None)


def _mark_connected(addr: str | None, owner: Any | None = None) -> None:
    """
    Record that a BLE address is connected and capture optional owner metadata.

    If `addr` is None, empty, or whitespace-only this function is a no-op. When a valid
    address is provided it is normalized to an internal key and the registry is updated
    to mark the address as connected, store a weak reference to `owner` when possible,
    store the owner's identity (`id(owner)`) when provided, and record the monotonic
    timestamp when the connection was marked.

    Parameters
    ----------
        addr (str | None): The BLE address to mark connected; may be None or empty (no-op).
        owner (Any | None): Optional owner object associated with the connection; a weak reference
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


def _mark_disconnected(addr: str | None, owner: Any | None = None) -> None:
    """
    Mark an address as disconnected and remove its connection bookkeeping.

    Normalizes `addr` and, if valid, under the registry lock clears the recorded connected state and triggers per-address lock cleanup. If `owner` is provided, the disconnect only proceeds when the live recorded owner matches `owner`; otherwise the disconnect is ignored. This function does not decrement per-address lock holder counts - callers are responsible for holder bookkeeping.

    Parameters
    ----------
        addr (str | None): Address to mark disconnected; will be normalized internally. If `None` or empty, the call is a no-op.
        owner (Any | None): Optional owner object; when provided, the recorded live owner must be the same object for the disconnect to be applied.

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
            # Also check owner ID when weakref is unavailable (non-weakrefable objects)
            if current_owner is None:
                stored_id = _CONNECTED_OWNER_IDS.get(key)
                # CPython can reuse ids after GC; this fallback path is only used
                # when weakref tracking is unavailable and remains best-effort.
                if stored_id is not None and stored_id != id(owner):
                    logger.debug(
                        "Ignoring disconnect mark for %s from non-owner instance.",
                        key,
                    )
                    return
        _remove_connected_record_locked(key)
        _cleanup_addr_lock(key)


@contextmanager
def _addr_lock_context(addr: str | None) -> Generator[RLock, None, None]:
    """
    Provide the per-address reentrant lock for a normalized BLE address key while managing its internal holder count.

    The context yields the per-address RLock without acquiring it; on context exit the holder count is decremented so the lock can be cleaned up when no longer used.

    Parameters
    ----------
        addr (str | None): BLE address or already-normalized address key; None selects the global registry lock.

    Yields
    ------
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
    addr: str | None, owner: Any | None = None
) -> bool:
    """
    Internal helper: Determine whether a BLE address is recorded as connected by a different owner.

    Normalizes the provided address via _addr_key; if normalization yields None the function returns False. While checking, stale records are pruned when the recorded owner has been garbage-collected, reports disconnected, or an unowned claim has expired.

    Parameters
    ----------
        addr (str | None): BLE address to check; will be normalized via _addr_key.
        owner (Any | None): Caller's owner instance; if it matches the recorded owner by identity or recorded id, the address is not considered connected elsewhere.

    Returns
    -------
        True if the normalized address is marked connected by a different owner, False otherwise.

    """
    key = _addr_key(addr)
    if key is None:
        return False

    owner_to_check: Any | None = None
    with _REGISTRY_LOCK:
        if key not in _CONNECTED_ADDRS:
            return False

        owner_ref = _CONNECTED_OWNERS.get(key)
        current_owner = owner_ref() if owner_ref is not None else None
        current_owner_id = _CONNECTED_OWNER_IDS.get(key)

        # Same owner is not "connected elsewhere".
        # CPython can reuse id() values after GC; stale _CONNECTED_OWNER_IDS matches
        # are mitigated by immediate weakref-dead pruning via
        # _remove_connected_record_locked/_cleanup_addr_lock, connection-state
        # validation via _owner_connected_state, and timed stale-claim cleanup
        # using _CONNECTED_MARKED_AT + BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS.
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

        if current_owner is not None:
            # Evaluate owner connection state outside _REGISTRY_LOCK to avoid
            # crossing into interface state locks while holding the registry lock.
            owner_to_check = current_owner
        else:
            # Fallback for unowned claims: prune after a safety window.
            # NOTE: If _CONNECTED_MARKED_AT lacks the key (out-of-sync records),
            # marked_at defaults to 0.0 which is falsy, causing the timed-prune check
            # to be skipped. This intentionally keeps the claim alive to avoid
            # premature removal when records are inconsistent, trading off immediate
            # cleanup for safety.
            marked_at = _CONNECTED_MARKED_AT.get(key, 0.0)
            if marked_at and (
                time.monotonic() - marked_at
                > BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS
            ):
                _remove_connected_record_locked(key)
                _cleanup_addr_lock(key)
                return False

            return True

    # owner_to_check is guaranteed non-None here: all current_owner-is-None
    # paths return from inside the first _REGISTRY_LOCK block above.
    connection_state = _owner_connected_state(owner_to_check)

    with _REGISTRY_LOCK:
        if key not in _CONNECTED_ADDRS:
            return False

        owner_ref = _CONNECTED_OWNERS.get(key)
        current_owner = owner_ref() if owner_ref is not None else None
        current_owner_id = _CONNECTED_OWNER_IDS.get(key)

        if owner is not None and (
            current_owner is owner
            or (current_owner_id is not None and current_owner_id == id(owner))
        ):
            return False

        if owner_ref is not None and current_owner is None:
            _remove_connected_record_locked(key)
            _cleanup_addr_lock(key)
            return False

        # Only apply the sampled owner state if the claim still belongs to the
        # same owner object we checked outside _REGISTRY_LOCK.
        if current_owner is owner_to_check and connection_state is False:
            _remove_connected_record_locked(key)
            _cleanup_addr_lock(key)
            return False

        if current_owner is not None:
            return True

        marked_at = _CONNECTED_MARKED_AT.get(key, 0.0)
        if marked_at and (
            time.monotonic() - marked_at
            > BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS
        ):
            _remove_connected_record_locked(key)
            _cleanup_addr_lock(key)
            return False

        return True
