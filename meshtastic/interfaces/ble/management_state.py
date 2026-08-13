"""Owned synchronization and accounting state for BLE management operations."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol, cast

from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_callable,
    _get_declared_lock,
    _get_declared_member,
)
from meshtastic.interfaces.ble.ports import _LockPort


class _ManagementConditionPort(Protocol):
    """Condition operations required by management coordination."""

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for a management-state change."""

    def notify_all(self) -> None:
        """Wake all waiters observing management-state changes."""


class _BLEManagementStatePort(Protocol):
    """State operations required by BLE management command coordination."""

    @property
    def lock(self) -> _LockPort:
        """Return the management coordination lock."""

    @property
    def condition(self) -> _ManagementConditionPort:
        """Return the condition notified when management work becomes idle."""

    @property
    def inflight(self) -> int:
        """Return the number of in-flight management operations."""

    @inflight.setter
    def inflight(self, value: int) -> None:
        """Replace the in-flight count for compatibility/testing seams."""

    def _begin_locked(self) -> None:
        """Increment the in-flight count while the caller holds ``lock``."""

    def finish(self) -> bool:
        """Finish one operation and return whether accounting remained valid."""


class _ContextLockCondition:
    """Condition adapter for locks that expose only context-manager semantics."""

    def __init__(self, lock: _LockPort) -> None:
        self._lock = lock
        self._waiters_lock = threading.Lock()
        self._waiters: list[threading.Event] = []
        enter_lock = _get_declared_callable(lock, "__enter__")
        exit_lock = _get_declared_callable(lock, "__exit__")
        if enter_lock is None or exit_lock is None:
            raise TypeError("compatibility lock must implement context-manager methods")
        self._enter_lock: Callable[..., Any] = enter_lock
        self._exit_lock: Callable[..., Any] = exit_lock

    def wait(self, timeout: float | None = None) -> bool:
        """Release the compatibility lock while waiting, then reacquire it."""
        waiter = threading.Event()
        with self._waiters_lock:
            self._waiters.append(waiter)

        self._exit_lock(None, None, None)  # pylint: disable=not-callable
        try:
            return waiter.wait(timeout)
        finally:
            with self._waiters_lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
            self._enter_lock()  # pylint: disable=not-callable

    def notify_all(self) -> None:
        """Wake every waiter registered before this notification."""
        with self._waiters_lock:
            waiters = tuple(self._waiters)
            self._waiters.clear()
        for waiter in waiters:
            waiter.set()


def _condition_for_lock(lock: _LockPort) -> _ManagementConditionPort:
    """Create a condition compatible with either full or context-only locks."""
    if (
        _get_declared_callable(lock, "acquire") is not None
        and _get_declared_callable(lock, "release") is not None
    ):
        return cast(
            _ManagementConditionPort,
            threading.Condition(cast(Any, lock)),
        )
    return _ContextLockCondition(lock)


_LEGACY_ADAPTER_ATTR = "_ble_management_state_adapter"
_LEGACY_ADAPTER_CACHE_LOCK = threading.RLock()


def _persist_legacy_member(target: object, name: str, value: object) -> None:
    """Persist a compatibility synchronization member when the target allows it."""
    try:
        vars(target)[name] = value
        return
    except TypeError:
        pass
    try:
        setattr(target, name, value)
    except (AttributeError, TypeError):
        # Some legacy test doubles/proxies intentionally reject new attributes.
        # The adapter still works for that single resolution; callers with writable
        # compatibility members converge on one shared synchronization primitive.
        pass


class BLEManagementState:
    """Own management-operation lock, condition, and in-flight accounting."""

    def __init__(self, lock: _LockPort | None = None) -> None:
        """Create management state around one stable re-entrant lock.

        Parameters
        ----------
        lock : _LockPort | None, optional
            Lock used to serialize management accounting. A new ``RLock`` is
            created when omitted.
        """
        self._lock = lock or cast(_LockPort, threading.RLock())
        self._condition = _condition_for_lock(self._lock)
        self._inflight = 0

    @property
    def lock(self) -> _LockPort:
        """Return the owned management lock."""
        return self._lock

    @property
    def condition(self) -> _ManagementConditionPort:
        """Return the condition associated with the management lock."""
        return self._condition

    @property
    def inflight(self) -> int:
        """Return the current in-flight operation count."""
        return self._inflight

    @inflight.setter
    def inflight(self, value: int) -> None:
        """Replace the current in-flight operation count."""
        self._inflight = int(value)

    def _replace_lock(self, lock: _LockPort) -> None:
        """Replace the compatibility lock and rebuild its default condition.

        Parameters
        ----------
        lock : _LockPort
            Replacement synchronization lock.
        """
        self._lock = lock
        self._condition = _condition_for_lock(lock)

    def _replace_condition(self, condition: _ManagementConditionPort) -> None:
        """Replace the compatibility condition object.

        Parameters
        ----------
        condition : _ManagementConditionPort
            Condition associated with the currently configured management lock.
        """
        self._condition = condition

    def _begin_locked(self) -> None:
        """Increment in-flight accounting while the caller holds ``lock``."""
        self._inflight += 1

    def finish(self) -> bool:
        """Finish one operation and notify idle waiters.

        Returns
        -------
        bool
            ``True`` when a positive in-flight count was decremented normally;
            ``False`` when an underflow was detected and normalized to zero.
        """
        with self._lock:
            if self._inflight <= 0:
                self._inflight = 0
                self._condition.notify_all()
                return False
            self._inflight -= 1
            if self._inflight == 0:
                self._condition.notify_all()
            return True


class _LegacyBLEManagementStateAdapter:
    """Compatibility adapter for partial collaborators without owned state."""

    def __init__(self, target: object) -> None:
        self._target = target
        declared_lock = _get_declared_lock(target, "_management_lock")
        self._lock = declared_lock or cast(_LockPort, threading.RLock())
        if declared_lock is None:
            _persist_legacy_member(target, "_management_lock", self._lock)

        declared_condition = _get_declared_member(target, "_management_idle_condition")
        if declared_condition is not None:
            self._condition = cast(_ManagementConditionPort, declared_condition)
        else:
            self._condition = _condition_for_lock(self._lock)
            _persist_legacy_member(
                target,
                "_management_idle_condition",
                self._condition,
            )

    @property
    def lock(self) -> _LockPort:
        """Return the management lock exposed by the legacy target."""
        return self._lock

    @property
    def condition(self) -> _ManagementConditionPort:
        """Return the management-idle condition exposed by the legacy target."""
        return self._condition

    @property
    def inflight(self) -> int:
        """Return the legacy target's current management-operation count."""
        value = _get_declared_member(self._target, "_management_inflight", 0)
        return int(value) if isinstance(value, int) else 0

    @inflight.setter
    def inflight(self, value: int) -> None:
        cast(Any, self._target)._management_inflight = int(value)

    def _begin_locked(self) -> None:
        """Increment legacy inflight accounting while the caller owns ``lock``."""
        self.inflight = self.inflight + 1

    def finish(self) -> bool:
        """Retire one legacy inflight operation and signal idle at zero."""
        with self.lock:
            current = self.inflight
            if current <= 0:
                self.inflight = 0
                self.condition.notify_all()
                return False
            self.inflight = current - 1
            if self.inflight == 0:
                self.condition.notify_all()
            return True


def _management_state_for(
    target: object,
    state: _BLEManagementStatePort | None = None,
) -> _BLEManagementStatePort:
    """Return explicit/owned management state or a legacy compatibility adapter.

    Parameters
    ----------
    target : object
        Interface-like object that may expose ``_management_state`` or legacy
        management lock/count members.
    state : _BLEManagementStatePort | None, optional
        Explicit state supplied by the composition root.

    Returns
    -------
    _BLEManagementStatePort
        State owner used by management collaborators.
    """
    if state is not None:
        return state
    declared = _get_declared_member(target, "_management_state")
    if isinstance(declared, BLEManagementState):
        return declared
    with _LEGACY_ADAPTER_CACHE_LOCK:
        cached = _get_declared_member(target, _LEGACY_ADAPTER_ATTR)
        if isinstance(cached, _LegacyBLEManagementStateAdapter):
            return cached
        adapter = _LegacyBLEManagementStateAdapter(target)
        try:
            namespace = vars(target)
        except TypeError:
            return adapter
        namespace[_LEGACY_ADAPTER_ATTR] = adapter
        return adapter
