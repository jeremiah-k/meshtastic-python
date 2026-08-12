"""Tests for owned BLE management synchronization state."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from meshtastic.interfaces.ble.management_state import (
    BLEManagementState,
    _management_state_for,
)


@pytest.mark.unit
def test_management_state_tracks_and_notifies_idle_waiters() -> None:
    """Management state should own inflight accounting and idle notification."""
    state = BLEManagementState()
    notify_all = MagicMock(wraps=state.condition.notify_all)
    condition = SimpleNamespace(wait=state.condition.wait, notify_all=notify_all)
    state.replace_condition(condition)  # type: ignore[arg-type]

    with state.lock:
        state.begin_locked()
        state.begin_locked()
    assert state.inflight == 2

    assert state.finish() is True
    assert state.inflight == 1
    notify_all.assert_not_called()

    assert state.finish() is True
    assert state.inflight == 0
    notify_all.assert_called_once_with()


@pytest.mark.unit
def test_management_state_normalizes_underflow_and_notifies() -> None:
    """Finishing without active work should reset to idle and signal waiters."""
    state = BLEManagementState()
    notify_all = MagicMock()
    condition = SimpleNamespace(
        wait=lambda *_args, **_kwargs: True,
        notify_all=notify_all,
    )
    state.replace_condition(condition)  # type: ignore[arg-type]
    state.inflight = -3

    assert state.finish() is False
    assert state.inflight == 0
    notify_all.assert_called_once_with()


@pytest.mark.unit
def test_management_state_lock_replacement_rebuilds_default_condition() -> None:
    """Compatibility lock replacement should keep condition ownership coherent."""
    state = BLEManagementState()
    replacement = threading.RLock()

    state.replace_lock(replacement)  # type: ignore[arg-type]

    assert state.lock is replacement
    with state.lock:
        state.begin_locked()
    assert state.inflight == 1
    assert state.finish() is True


@pytest.mark.unit
def test_management_state_for_preserves_legacy_collaborator_members() -> None:
    """Legacy partial collaborators should remain usable through an adapter."""
    lock = threading.RLock()
    condition = threading.Condition(lock)
    target = SimpleNamespace(
        _management_lock=lock,
        _management_idle_condition=condition,
        _management_inflight=0,
    )
    state = _management_state_for(target)

    with state.lock:
        state.begin_locked()
    assert target._management_inflight == 1
    assert state.finish() is True
    assert target._management_inflight == 0
