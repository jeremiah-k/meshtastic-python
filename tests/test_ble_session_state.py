"""Tests for owned BLE session-state compatibility views."""

import threading
from types import SimpleNamespace

from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.interfaces.ble.lifecycle_controller_runtime import BLELifecycleController
from meshtastic.interfaces.ble.session_state import BLESessionState, _session_state_for
from meshtastic.interfaces.ble.state import BLEStateManager


def _bare_interface() -> BLEInterface:
    """Create an uninitialized interface with only owned state collaborators."""
    iface = BLEInterface.__new__(BLEInterface)
    state_manager = BLEStateManager()
    iface._state_manager = state_manager
    iface._session_state = BLESessionState(lock=state_manager.lock)
    return iface


def test_interface_lifecycle_fields_delegate_to_session_state() -> None:
    """Historical private fields should remain writable views of owned state."""
    iface = _bare_interface()

    iface._closed = True
    iface._disconnect_notified = True
    iface._client_publish_pending = True
    iface._last_disconnect_source = "test"
    iface._connection_session_epoch = 7
    iface._want_receive = False
    iface._receive_start_pending = True
    iface._receive_start_pending_since = 12.5

    assert iface._state_lock is iface._state_manager.lock
    assert iface._session_state.closed is True
    assert iface._session_state.disconnect_notified is True
    assert iface._session_state.client_publish_pending is True
    assert iface._session_state.last_disconnect_source == "test"
    assert iface._session_state.connection_session_epoch == 7
    assert iface._session_state.want_receive is False
    assert iface._session_state.receive_start_pending is True
    assert iface._session_state.receive_start_pending_since == 12.5


def test_session_state_retry_reset_helpers() -> None:
    """Retry/recovery bookkeeping resets should be explicit and complete."""
    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock)
    state.read_retry_count = 3
    state.last_empty_read_warning = 9.5
    state.suppressed_empty_read_warnings = 4
    state.receive_recovery_attempts = 2
    state.last_recovery_time = 11.0

    state.reset_receive_retry_state()
    state.reset_recovery_state()

    assert state.read_retry_count == 0
    assert state.last_empty_read_warning == 0.0
    assert state.suppressed_empty_read_warnings == 0
    assert state.receive_recovery_attempts == 0
    assert state.last_recovery_time == 0.0


def test_lifecycle_controller_shares_owned_session_state() -> None:
    """Lifecycle coordinators should share the interface's single state owner."""
    iface = _bare_interface()

    controller = BLELifecycleController(iface)

    assert controller._receive._session is iface._session_state
    assert controller._disconnect._session is iface._session_state
    assert controller._connection_ownership._session is iface._session_state
    assert controller._shutdown._session is iface._session_state


def test_lazy_session_state_ignores_dynamically_synthesized_lock() -> None:
    """Lazy state creation should only reuse an explicitly declared state lock."""

    class DynamicStateManager:
        def __getattr__(self, _name: str) -> object:
            return self

        def acquire(self) -> None:
            raise AssertionError("dynamic lock should not be used")

        def release(self) -> None:
            raise AssertionError("dynamic lock should not be used")

    iface = BLEInterface.__new__(BLEInterface)
    dynamic_manager = DynamicStateManager()
    iface._state_manager = dynamic_manager

    state = iface._get_session_state()

    assert state.lock is not dynamic_manager
    assert state.lock.acquire(blocking=False)
    state.lock.release()


def test_legacy_session_state_adapter_implements_reset_contract() -> None:
    """Legacy interfaces should satisfy the complete session-state reset port."""
    legacy = SimpleNamespace(
        _state_lock=threading.RLock(),
        _read_retry_count=5,
        _last_empty_read_warning=7.5,
        _suppressed_empty_read_warnings=3,
        _receive_recovery_attempts=4,
        _last_recovery_time=12.0,
    )

    state = _session_state_for(legacy)
    assert _session_state_for(legacy) is state
    assert state.lock is state.lock

    state.reset_read_retry_count()
    assert legacy._read_retry_count == 0

    legacy._read_retry_count = 2
    state.reset_receive_retry_state()
    assert legacy._read_retry_count == 0
    assert legacy._last_empty_read_warning == 0.0
    assert legacy._suppressed_empty_read_warnings == 0

    state.reset_recovery_state()
    assert legacy._receive_recovery_attempts == 0
    assert legacy._last_recovery_time == 0.0


def test_receive_lifecycle_uses_explicit_session_pending_markers() -> None:
    """Explicit session state must govern stale/pending receive-start decisions."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        BLEReceiveLifecycleCoordinator,
    )

    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock)
    pending_thread = SimpleNamespace(name="PendingReceive", ident=None, is_alive=lambda: False)
    state.receive_thread = pending_thread
    state.receive_start_pending = True
    state.receive_start_pending_since = 10**12  # safely in the future for this probe

    # Deliberately contradictory legacy fields prove the coordinator reads the
    # explicit session owner rather than compatibility fields on the interface.
    iface = SimpleNamespace(
        _receive_start_pending=False,
        _receive_start_pending_since=None,
    )
    coordinator = BLEReceiveLifecycleCoordinator(iface, session_state=state)  # type: ignore[arg-type]

    def _unexpected_create(**_kwargs: object) -> object:
        raise AssertionError("pending session state should suppress thread creation")

    created, recovery_attempts = coordinator._check_receive_start_conditions(  # noqa: SLF001
        name="PendingReceive",
        reset_recovery=False,
        create_runtime_thread=_unexpected_create,  # type: ignore[arg-type]
    )

    assert created is None
    assert recovery_attempts is None
    assert state.receive_start_pending is True


def test_receive_controller_reads_ever_connected_from_explicit_session() -> None:
    """Receive reconnect detection should use the shared session owner."""
    from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryController

    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock, ever_connected=True)
    iface = SimpleNamespace(_ever_connected=False)

    controller = BLEReceiveRecoveryController(iface, session_state=state)  # type: ignore[arg-type]

    assert controller._has_ever_connected_session() is True  # noqa: SLF001
