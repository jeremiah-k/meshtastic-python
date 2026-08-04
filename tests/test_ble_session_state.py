"""Tests for owned BLE session-state compatibility views."""

from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.interfaces.ble.lifecycle_controller_runtime import BLELifecycleController
from meshtastic.interfaces.ble.session_state import BLESessionState
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
