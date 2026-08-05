"""Tests for owned BLE session-state compatibility views."""

import threading
from types import SimpleNamespace, TracebackType

import pytest

from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.interfaces.ble.lifecycle_controller_runtime import (
    BLELifecycleController,
)
from meshtastic.interfaces.ble.session_state import (
    BLESessionState,
    LEGACY_SESSION_STATE_CACHE_ERROR,
    _LegacyBLESessionStateAdapter,
    _session_state_for,
)
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState

pytestmark = pytest.mark.unit


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

    state._reset_receive_retry_state()
    state._reset_recovery_state()

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
    assert state.lock is legacy._state_lock

    state._reset_read_retry_count()
    assert legacy._read_retry_count == 0

    legacy._read_retry_count = 2
    state._reset_receive_retry_state()
    assert legacy._read_retry_count == 0
    assert legacy._last_empty_read_warning == 0.0
    assert legacy._suppressed_empty_read_warnings == 0

    state._reset_recovery_state()
    assert legacy._receive_recovery_attempts == 0
    assert legacy._last_recovery_time == 0.0


def test_legacy_session_state_adapter_tracks_replaced_declared_lock() -> None:
    """Legacy adapters should follow the interface's current declared state lock."""
    first_lock = threading.RLock()
    replacement_lock = threading.RLock()
    legacy = SimpleNamespace(_state_lock=first_lock)

    state = _session_state_for(legacy)
    assert state.lock is first_lock

    legacy._state_lock = replacement_lock

    assert state.lock is replacement_lock


def test_partial_interface_resolves_mixin_session_owner_directly() -> None:
    """Partial BLE interfaces should not create a competing legacy adapter."""
    iface = BLEInterface.__new__(BLEInterface)

    state = _session_state_for(iface)

    assert isinstance(state, BLESessionState)
    assert state is iface._get_session_state()
    assert state.lock is iface._state_lock


def test_mixin_promotion_preserves_cached_adapter_lock() -> None:
    """Fallback adapter promotion must retain one shared, replaceable state lock."""
    iface = BLEInterface.__new__(BLEInterface)
    adapter = _LegacyBLESessionStateAdapter(iface)
    iface.__dict__["_session_state"] = adapter

    state = iface._get_session_state()

    assert isinstance(state, BLESessionState)
    assert state.lock is adapter.lock
    assert iface._state_lock is adapter.lock

    replacement_lock = threading.RLock()
    iface._state_lock = replacement_lock

    assert state.lock is replacement_lock
    assert adapter.lock is replacement_lock


def test_uncacheable_legacy_collaborator_requires_explicit_session() -> None:
    """Slot-only legacy collaborators must not create competing implicit owners."""

    class _SlotLegacy:
        __slots__ = ("_closed", "_receiveThread")

        def __init__(self) -> None:
            self._closed = False
            self._receiveThread = None

    legacy = _SlotLegacy()
    explicit = BLESessionState(lock=threading.RLock())

    with pytest.raises(TypeError) as exc_info:
        _session_state_for(legacy)

    assert str(exc_info.value) == LEGACY_SESSION_STATE_CACHE_ERROR
    assert _session_state_for(legacy, explicit) is explicit


def test_disconnect_reconnect_scheduler_missing_on_partial_interface() -> None:
    """Partial interfaces should fail with the stable missing-scheduler contract."""
    from threading import Event, RLock

    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )
    from meshtastic.interfaces.ble.lifecycle_primitives import (
        RECONNECT_SCHEDULER_MISSING_MSG,
    )

    state = BLESessionState(lock=RLock())
    iface = SimpleNamespace(auto_reconnect=True, _shutdown_event=Event())
    coordinator = BLEDisconnectLifecycleCoordinator(  # type: ignore[arg-type]
        iface, session_state=state
    )

    with pytest.raises(AttributeError) as exc_info:
        coordinator.schedule_auto_reconnect(is_closing_getter=lambda: False)

    assert str(exc_info.value) == RECONNECT_SCHEDULER_MISSING_MSG


def test_receive_lifecycle_uses_explicit_session_pending_markers() -> None:
    """Explicit session state must govern stale/pending receive-start decisions."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        BLEReceiveLifecycleCoordinator,
    )

    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock)
    pending_thread = SimpleNamespace(
        name="PendingReceive", ident=None, is_alive=lambda: False
    )
    state.receive_thread = pending_thread
    state.receive_start_pending = True
    state.receive_start_pending_since = 10**12  # safely in the future for this probe

    # Deliberately contradictory legacy fields prove the coordinator reads the
    # explicit session owner rather than compatibility fields on the interface.
    iface = SimpleNamespace(
        _receive_start_pending=False,
        _receive_start_pending_since=None,
    )
    coordinator = BLEReceiveLifecycleCoordinator(  # type: ignore[arg-type]
        iface, session_state=state
    )

    def _unexpected_create(**_kwargs: object) -> object:
        raise AssertionError("pending session state should suppress thread creation")

    created, recovery_attempts = (
        coordinator._check_receive_start_conditions(  # noqa: SLF001
            name="PendingReceive",
            reset_recovery=False,
            create_runtime_thread=_unexpected_create,  # type: ignore[arg-type]
        )
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


def test_receive_controller_invalid_lifecycle_results_fall_back_to_session() -> None:
    """Non-bool lifecycle probes must not override authoritative session state."""
    from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryController

    state_manager = BLEStateManager()
    state = BLESessionState(lock=state_manager.lock, ever_connected=True, closed=True)
    lifecycle = SimpleNamespace(
        _has_ever_connected_session=lambda: None,
        _is_connection_closing=lambda: object(),
    )
    iface = SimpleNamespace(
        _get_lifecycle_controller=lambda: lifecycle,
        _state_manager=None,
        _is_connection_closing=False,
    )
    controller = BLEReceiveRecoveryController(iface, session_state=state)  # type: ignore[arg-type]

    assert controller._has_ever_connected_session() is True  # noqa: SLF001
    assert controller._is_connection_closing() is True  # noqa: SLF001


def test_receive_lifecycle_schedules_inconclusive_restart_outside_session_lock() -> None:
    """Deferred restart scheduling must not begin while the session lock is held."""
    from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
        BLEReceiveLifecycleCoordinator,
    )

    class _TrackingLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()
            self._depth = 0

        @property
        def held(self) -> bool:
            return self._depth > 0

        def __enter__(self) -> "_TrackingLock":
            self._lock.acquire()
            self._depth += 1
            return self

        def __exit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc_value: BaseException | None,
            _traceback: TracebackType | None,
        ) -> None:
            self._depth -= 1
            self._lock.release()

    lock = _TrackingLock()
    state = BLESessionState(lock=lock)
    existing = SimpleNamespace(
        name="InconclusiveReceive", ident=None, is_alive=lambda: False
    )
    state.receive_thread = existing  # type: ignore[assignment]
    iface = SimpleNamespace(_receive_from_radio_impl=lambda: None)
    coordinator = BLEReceiveLifecycleCoordinator(  # type: ignore[arg-type]
        iface, session_state=state
    )
    scheduled: list[tuple[object, bool]] = []

    def _capture_schedule(**kwargs: object) -> None:
        assert lock.held is False
        scheduled.append(
            (kwargs["existing_thread"], bool(kwargs["enforce_pending_timeout"]))
        )

    coordinator._schedule_deferred_receive_restart = (  # type: ignore[method-assign]
        _capture_schedule
    )

    created, recovery_attempts = coordinator._check_receive_start_conditions(  # noqa: SLF001
        name="InconclusiveReceive",
        reset_recovery=False,
        create_runtime_thread=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("inconclusive liveness must defer thread creation")
        ),
    )

    assert created is None
    assert recovery_attempts is None
    assert scheduled == [(existing, True)]
    assert state.receive_start_pending is True


def test_disconnect_plan_reads_epoch_from_explicit_session_state() -> None:
    """Disconnect staleness must use the coordinator's owned session epoch."""
    from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
        BLEDisconnectLifecycleCoordinator,
    )

    state_manager = BLEStateManager()
    state = BLESessionState(
        lock=state_manager.lock,
        client_publish_pending=True,
        connection_session_epoch=9,
    )
    iface = SimpleNamespace(
        auto_reconnect=False,
        client=None,
        address="AA:BB:CC:DD:EE:FF",
        _connection_session_epoch=1,
        _shutdown_event=threading.Event(),
        _extract_client_address=lambda _client: None,
        _sorted_address_keys=lambda *keys: [key for key in keys if key],
    )
    coordinator = BLEDisconnectLifecycleCoordinator(
        iface, session_state=state  # type: ignore[arg-type]
    )

    plan = coordinator._resolve_disconnect_target(  # noqa: SLF001
        "explicit-session",
        client=None,
        bleak_client=None,
        current_state_getter=lambda: ConnectionState.CONNECTED,
        is_closing_getter=lambda: False,
        transition_to_disconnected=lambda: True,
        reset_to_disconnected=lambda: True,
    )

    assert plan.session_epoch == 9
    assert iface._connection_session_epoch == 1


def test_ownership_cleanup_reads_explicit_session_state() -> None:
    """Publish cleanup must not consult contradictory interface compatibility fields."""
    from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
        BLEConnectionOwnershipLifecycleCoordinator,
    )

    client = object()
    state_manager = BLEStateManager()
    state = BLESessionState(
        lock=state_manager.lock,
        client_publish_pending=True,
        connected_publish_inflight_client=client,  # type: ignore[arg-type]
        connection_session_epoch=9,
    )
    iface = SimpleNamespace(
        client=None,
        address="legacy-address",
        _last_connection_request="legacy-request",
        _client_publish_pending=False,
        _connected_publish_inflight_client=None,
        _connection_session_epoch=1,
    )
    coordinator = BLEConnectionOwnershipLifecycleCoordinator(
        iface, session_state=state  # type: ignore[arg-type]
    )

    coordinator._discard_invalidated_connected_client(  # noqa: SLF001
        client,  # type: ignore[arg-type]
        restore_address="AA:BB:CC:DD:EE:FF",
        restore_last_connection_request="restored",
        is_closing_getter=lambda: False,
        reset_to_disconnected=lambda: True,
        current_state_getter=lambda: ConnectionState.DISCONNECTED,
        transition_to_disconnected=lambda: True,
        safe_cleanup=lambda _cleanup, _name: None,
    )

    assert state.client_publish_pending is False
    assert state.connected_publish_inflight_client is None
    assert state.connection_session_epoch == 9
    assert iface._client_publish_pending is False
    assert iface._connected_publish_inflight_client is None
    assert iface._connection_session_epoch == 1
