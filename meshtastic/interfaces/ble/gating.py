"""Process-wide BLE connection gating utilities.

Lock Ordering Note:
    - Never hold `_REGISTRY_LOCK` while waiting to acquire a per-address lock.
    - `_REGISTRY_LOCK` is for short registry updates only.
    - Per-address locks may be held while calling `_mark_connected()` /
      `_mark_disconnected()`, which each acquire `_REGISTRY_LOCK` internally.
      This prevents registry<->address lock inversion in concurrent
      connect/disconnect paths.

Context Manager Usage:
    Prefer using _addr_lock_context() over manual _get_addr_lock()/_release_addr_lock()
    or _get_addr_lock_by_key()/_release_addr_lock_by_key() calls to ensure
    proper holder-count management:

        with _addr_lock_context(address) as lock:
            with lock:
                # Connection logic here
                # On success: _mark_connected(address) records the address in _CONNECTED_ADDRS
                # (it does not touch _LOCK_HOLDERS), which prevents _release_addr_lock_by_key
                # from removing the lock entry when the context exits.
                # On failure/exception: context manager releases holder count

    Note: _addr_lock_context() yields the per-address RLock handle but does not
    acquire it; callers must explicitly acquire it with `with lock:`.
"""

import logging
import time
import weakref
from collections.abc import Generator
from contextlib import contextmanager
from threading import RLock
from typing import Any

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
    """Reset all internal BLE gating registries to their empty initial state.

    Acquires the module-wide registry lock to perform a thread-safe clear of:
    per-address lock mapping, connected-address set, connection timestamps,
    owner id and weak-ref mappings, and per-address lock holder counts.
    """
    with _REGISTRY_LOCK:
        _ADDR_LOCKS.clear()
        _CONNECTED_ADDRS.clear()
        _CONNECTED_MARKED_AT.clear()
        _CONNECTED_OWNER_IDS.clear()
        _CONNECTED_OWNERS.clear()
        _LOCK_HOLDERS.clear()


def _addr_key(addr: str | None) -> str | None:
    """Normalize a BLE address into a registry key.

    Parameters
    ----------
    addr : str | None
        Raw BLE address to normalize.

    Returns
    -------
    str | None
        The sanitized address string, or None if the input is None, empty, or contains only whitespace.
    """
    sanitized = sanitize_address(addr)
    return sanitized if sanitized else None


def _get_addr_lock(key: str | None) -> RLock:
    """Get the process-wide reentrant lock for a normalized address key.

    If `key` is None the global registry lock is returned. When a per-address lock is returned,
    its internal holder count is incremented to prevent premature cleanup; callers that obtain
    the lock object but do not acquire it must call `_release_addr_lock()` when finished.

    Parameters
    ----------
    key : str | None
        A raw or already-normalized address key; `None` selects the global registry lock.

    Returns
    -------
    RLock
        The per-address reentrant lock for `key`, or the global registry lock if `key` is None.
    """
    key = _addr_key(key)
    return _get_addr_lock_by_key(key)


def _get_addr_lock_by_key(key: str | None) -> RLock:
    """Get the process-wide reentrant lock for a normalized address key.

    If `key` is None, returns the global registry lock. For per-address locks, ensures a lock exists and increments an internal holder count to prevent premature cleanup.

    Parameters
    ----------
    key : str | None
        Normalized address key; `None` selects the global registry lock.

    Returns
    -------
    RLock
        The reentrant lock for `key`, or the global registry lock when `key` is `None`.
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


def _maybe_remove_addr_lock_entry(key: str) -> None:
    """Remove per-address lock bookkeeping when no holders remain and key is disconnected.

    Must be called while holding `_REGISTRY_LOCK`.

    Parameters
    ----------
    key : str
        Normalized address key to evaluate.
    """
    if _LOCK_HOLDERS.get(key, 0) <= 0 and key not in _CONNECTED_ADDRS:
        _ADDR_LOCKS.pop(key, None)
        _LOCK_HOLDERS.pop(key, None)
        logger.debug("Cleaned up address lock for %s", key)


def _release_addr_lock(key: str | None) -> None:
    """Decrement the holder count for the per-address lock identified by key.

    If key is None, operates on the global registry lock. The input is normalized to a registry key; when the holder count reaches zero and the address is not marked connected, the per-address lock entry may be removed by cleanup logic.

    Parameters
    ----------
    key : str | None
        An address or already-normalized key identifying the per-address lock; use None for the registry.
    """
    key = _addr_key(key)
    _release_addr_lock_by_key(key)


def _release_addr_lock_by_key(key: str | None) -> None:
    """Decrement the holder count for a normalized address key and remove its lock when unused.

    If `key` is None this is a no-op. Decrements the internal holder count (never below zero); if the count reaches zero and the address is not marked connected, removes the per-address lock and its holder bookkeeping entry.

    Parameters
    ----------
    key : str | None
        Normalized address registry key (or None).
    """
    if key is None:
        return
    with _REGISTRY_LOCK:
        if key in _LOCK_HOLDERS:
            _LOCK_HOLDERS[key] = max(0, _LOCK_HOLDERS[key] - 1)
            _maybe_remove_addr_lock_entry(key)


def _cleanup_addr_lock(key: str | None) -> None:
    """Remove a per-address lock from the registry when there are no remaining holders.

    If `key` is None this function does nothing. When `key` is provided, the registry entries for that address (the lock and its holder count) are removed only if the tracked holder count is less than or equal to zero; otherwise the entries are left intact.

    Parameters
    ----------
    key : str | None
        Normalized address key identifying the per-address lock, or `None` to indicate no action.
    """
    if key is None:
        return
    with _REGISTRY_LOCK:
        holder_count = _LOCK_HOLDERS.get(key, 0)
        if holder_count > 0 or key in _CONNECTED_ADDRS:
            logger.debug(
                "Skipping cleanup of address lock for %s (holders: %d)",
                key,
                holder_count,
            )
            return
        _maybe_remove_addr_lock_entry(key)


def _owner_ref(owner: Any | None) -> weakref.ReferenceType[Any] | None:
    """Create a weak reference to `owner` when possible.

    If `owner` is None or does not support weak references, returns None.

    Parameters
    ----------
    owner : Any | None
        Owner object for which to create a weak reference.

    Returns
    -------
    weakref.ReferenceType[Any] | None
        A weak reference to `owner`, or `None` if `owner` is `None` or not weak-referenceable.
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
    """Return the connection state reported by an owner via its `_is_connection_connected` attribute.

    If the owner exposes `_is_connection_connected` as a callable, it will be invoked; if it is a boolean, that value is used. If the attribute is missing, not a boolean, or invoking the callable raises an exception, `None` is returned.

    Parameters
    ----------
    owner : Any
        Object that may expose an `_is_connection_connected` boolean or callable.

    Returns
    -------
    bool | None
        `True` if the owner reports connected, `False` if it reports disconnected, or `None` if not available or an error occurred while reading it.
    """
    connection_state = getattr(owner, "_is_connection_connected", None)
    if callable(connection_state):
        try:
            connection_state = connection_state()
        except Exception:  # noqa: BLE001 - defensive gate-state probe
            logger.debug(
                "Failed to read owner connection state; preserving gate claim.",
                exc_info=True,
            )
            return None

    if isinstance(connection_state, bool):
        return connection_state
    return None


def _remove_connected_record_locked(key: str) -> None:
    """Remove all connected-state bookkeeping associated with the normalized address key.

    Parameters
    ----------
    key : str
        Normalized address key whose connected-state records should be removed.

    Notes
    -----
    Must be called while holding the module-level `_REGISTRY_LOCK`.
    """
    _CONNECTED_ADDRS.discard(key)
    _CONNECTED_OWNERS.pop(key, None)
    _CONNECTED_OWNER_IDS.pop(key, None)
    _CONNECTED_MARKED_AT.pop(key, None)


def _prune_stale_unowned_claim_locked(key: str) -> bool:
    """Prune an unowned claim when its marker timestamp is missing or stale.

    Parameters
    ----------
    key : str
        Normalized address key to evaluate.

    Returns
    -------
    bool
        `True` when a stale claim was removed, `False` when claim remains valid.

    Notes
    -----
    Must be called while holding the reentrant `_REGISTRY_LOCK` (`RLock`).
    This function may call `_cleanup_addr_lock()`, which also acquires
    `_REGISTRY_LOCK`; nested acquisition is intentional and safe because the
    lock is reentrant.
    """
    marked_at = _CONNECTED_MARKED_AT.get(key)
    if marked_at is None or (
        time.monotonic() - marked_at > BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS
    ):
        _remove_connected_record_locked(key)
        is_owned = getattr(_REGISTRY_LOCK, "_is_owned", None)
        if callable(is_owned):
            assert (
                is_owned()
            )  # Nested lock acquisition below relies on RLock ownership.
        # Intentional nested acquisition: _cleanup_addr_lock() also takes
        # _REGISTRY_LOCK, and _REGISTRY_LOCK is a reentrant RLock.
        _cleanup_addr_lock(key)
        return True
    return False


def _mark_connected(addr: str | None, owner: Any | None = None) -> None:
    """Mark a BLE address as connected and record optional owner metadata.

    If `addr` is None, empty, or whitespace-only this function does nothing. For a valid
    address it normalizes the address to an internal key and, while holding the registry
    lock, adds the key to the connected set, stores a weak reference to `owner` when
    possible, records `id(owner)` (or `None`), and records the monotonic timestamp when
    the connection was marked.

    Parameters
    ----------
    addr : str | None
        The BLE address to mark connected; may be None or empty (no-op).
    owner : Any | None
        Optional owner object associated with the connection; a weak
        reference is stored when the object supports weak references and the owner's
        `id` is recorded. (Default value = None)
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
    """Mark an address as disconnected and clear its connection bookkeeping.

    This normalizes `addr` and is a no-op for `None` or empty addresses. If `owner` is provided, the disconnect is applied only when the recorded live owner matches `owner` (by identity or, when weak references are unavailable, by a stored owner id); otherwise the call is ignored. When applied, the function removes connected-state records for the address and triggers per-address lock cleanup. It does not decrement per-address lock holder counts — callers remain responsible for holder bookkeeping.

    Parameters
    ----------
    addr : str | None
        Address to mark disconnected; will be normalized internally.
    owner : Any | None
        Optional owner object that must match the recorded owner for the disconnect to proceed. (Default value = None)
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
                # CPython can reuse ids after GC; this _mark_disconnected fallback is
                # best-effort when _CONNECTED_OWNER_IDS must compare id(owner).
                # The race window is narrow because callers still hold owner live.
                if stored_id is None or stored_id != id(owner):
                    logger.debug(
                        "Ignoring disconnect mark for %s from non-owner instance.",
                        key,
                    )
                    return
        _remove_connected_record_locked(key)
        _cleanup_addr_lock(key)


@contextmanager
def _addr_lock_context(addr: str | None) -> Generator[RLock, None, None]:
    """Provide a per-address reentrant lock handle while managing the lock's internal holder count.

    Parameters
    ----------
    addr : str | None
        BLE address or already-normalized address key. If None, the global registry lock is selected.

    Yields
    ------
    RLock
        The per-address reentrant lock for the normalized address key. The lock is yielded but not acquired by this context manager.
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
    """Return whether a BLE address is recorded as connected by a different owner.

    Normalizes the provided address via _addr_key; if normalization yields None the function returns False. While checking, stale records are pruned when the recorded owner has been garbage-collected, reports disconnected, or an unowned claim has expired.

    Parameters
    ----------
    addr : str | None
        BLE address to check; will be normalized via _addr_key.
    owner : Any | None
        Caller's owner instance; if it matches the recorded owner by identity or recorded id, the address is not considered connected elsewhere. (Default value = None)

    Returns
    -------
    bool
        True if the normalized address is marked connected by a different owner, False otherwise.
    """
    key = _addr_key(addr)
    if key is None:
        return False

    owner_to_check: Any | None = None
    # Intentional two-phase TOCTOU trade-off:
    # we must not hold _REGISTRY_LOCK while calling into owner state because
    # owner callbacks can touch interface locks. We therefore sample owner
    # state outside the registry lock and then re-check ownership/claim under
    # _REGISTRY_LOCK before applying that sample.
    # Phase 1: Check under registry lock.
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
            if _prune_stale_unowned_claim_locked(key):
                return False

            return True

    # owner_to_check is guaranteed non-None here: all current_owner-is-None
    # paths return from inside the first _REGISTRY_LOCK block above.
    connection_state = _owner_connected_state(owner_to_check)

    # Phase 2: Verify owner connection state; re-check under registry lock to close the TOCTOU window.
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

        if _prune_stale_unowned_claim_locked(key):
            return False

        return True
