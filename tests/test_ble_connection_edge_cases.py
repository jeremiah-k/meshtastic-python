"""Edge case tests for BLE connection management."""

import logging
from types import SimpleNamespace
from threading import Event, RLock
from unittest.mock import ANY, MagicMock

import pytest

try:
    from bleak.exc import BleakDBusError, BleakDeviceNotFoundError
    from meshtastic.interfaces.ble.connection import (
        AWAIT_TIMEOUT_BUFFER_SECONDS,
        DIRECT_CONNECT_TIMEOUT_SECONDS,
        ClientManager,
        ConnectionOrchestrator,
        ConnectionValidator,
    )
    from meshtastic.interfaces.ble.constants import BLEConfig
    from meshtastic.interfaces.ble.reconnection import ReconnectWorker
    from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
except ImportError:
    pytest.skip("BLE dependencies not available", allow_module_level=True)


class MockBLEError(Exception):
    """Mock BLE error for connection validator tests."""


@pytest.mark.unit
def test_connection_validator_allows_connect_when_disconnected() -> None:
    """ConnectionValidator should allow connection when state is DISCONNECTED."""
    state_manager = BLEStateManager()
    lock = RLock()

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    # Should not raise when disconnected
    validator._validate_connection_request()


@pytest.mark.unit
def test_connection_validator_blocks_when_closing() -> None:
    """ConnectionValidator should block connection when interface is closing."""
    state_manager = BLEStateManager()
    lock = RLock()

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    # Transition to DISCONNECTING state
    state_manager._transition_to(ConnectionState.CONNECTING)
    state_manager._transition_to(ConnectionState.CONNECTED)
    state_manager._transition_to(ConnectionState.DISCONNECTING)

    # Should raise when closing
    with pytest.raises(MockBLEError, match="Cannot connect while interface is closing"):
        validator._validate_connection_request()


@pytest.mark.unit
def test_connection_validator_blocks_when_already_connecting() -> None:
    """ConnectionValidator should block when already connecting."""
    state_manager = BLEStateManager()
    lock = RLock()

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    # Transition to CONNECTING state
    state_manager._transition_to(ConnectionState.CONNECTING)

    # Should raise when already connecting
    with pytest.raises(
        MockBLEError, match="Already connected or connection in progress"
    ):
        validator._validate_connection_request()


@pytest.mark.unit
def test_connection_validator_check_existing_client_none_request() -> None:
    """check_existing_client should accept any connected client when request is None."""
    state_manager = BLEStateManager()
    lock = RLock()

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    # Create mock client
    mock_client = MagicMock()
    mock_client.isConnected.return_value = True

    # Should return True for None request
    assert validator._check_existing_client(mock_client, None, None)


@pytest.mark.unit
def test_connection_validator_check_existing_client_disconnected() -> None:
    """check_existing_client should return False for disconnected client."""
    state_manager = BLEStateManager()
    lock = RLock()

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    mock_client = MagicMock()
    mock_client.isConnected.return_value = False

    assert not validator._check_existing_client(mock_client, "address", None)


@pytest.mark.unit
def test_connection_validator_check_existing_client_none_client() -> None:
    """check_existing_client should return False when client is None."""
    state_manager = BLEStateManager()
    lock = RLock()

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    assert not validator._check_existing_client(None, "address", None)


@pytest.mark.unit
def test_client_manager_initialization() -> None:
    """ClientManager should initialize with required dependencies."""
    state_manager = BLEStateManager()
    lock = RLock()
    thread_coordinator = MagicMock()
    error_handler = MagicMock()

    manager = ClientManager(state_manager, lock, thread_coordinator, error_handler)

    assert manager.state_manager is state_manager
    assert manager.state_lock is lock
    assert manager.thread_coordinator is thread_coordinator
    assert manager.error_handler is error_handler


@pytest.mark.unit
def test_direct_connect_timeout_is_reasonable() -> None:
    """DIRECT_CONNECT_TIMEOUT_SECONDS should be shorter than CONNECTION_TIMEOUT."""
    assert DIRECT_CONNECT_TIMEOUT_SECONDS < BLEConfig.CONNECTION_TIMEOUT
    assert DIRECT_CONNECT_TIMEOUT_SECONDS > 0


@pytest.mark.unit
def test_await_timeout_buffer_is_positive() -> None:
    """AWAIT_TIMEOUT_BUFFER_SECONDS should be positive to allow timeout margin."""
    assert AWAIT_TIMEOUT_BUFFER_SECONDS > 0
    assert isinstance(AWAIT_TIMEOUT_BUFFER_SECONDS, (int, float))


@pytest.mark.unit
def test_connection_validator_check_existing_client_matching_address() -> None:
    """check_existing_client should return True when addresses match."""
    state_manager = BLEStateManager()
    lock = RLock()

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    mock_client = MagicMock()
    mock_client.isConnected.return_value = True
    mock_bleak_client = MagicMock()
    mock_bleak_client.address = "AA:BB:CC:DD:EE:FF"
    mock_client.bleak_client = mock_bleak_client

    # Should return True when addresses match (case-insensitive, punctuation-agnostic)
    assert validator._check_existing_client(
        mock_client,
        "aabbccddeeff",  # normalized request
        "aabbccddeeff",  # last connection request
    )


@pytest.mark.unit
def test_client_manager_safe_close_client_already_closed() -> None:
    """_safe_close_client should return cleanly for an already-closed client."""

    state_manager = BLEStateManager()
    lock = RLock()
    thread_coordinator = MagicMock()
    error_handler = MagicMock()

    manager = ClientManager(state_manager, lock, thread_coordinator, error_handler)

    # Create a mock client that's already closed
    mock_client = MagicMock()
    mock_client._closed = True
    mock_client.bleak_client = None
    event = Event()

    # Should not raise
    manager._safe_close_client(mock_client, event)
    assert event.is_set()


@pytest.mark.unit
def test_client_manager_update_client_reference_same_client() -> None:
    """_update_client_reference should not close when old and new are same."""
    state_manager = BLEStateManager()
    lock = RLock()
    thread_coordinator = MagicMock()
    error_handler = MagicMock()

    manager = ClientManager(state_manager, lock, thread_coordinator, error_handler)

    mock_client = MagicMock()

    # Should not create thread when old and new are the same
    manager._update_client_reference(mock_client, mock_client)
    thread_coordinator._create_thread.assert_not_called()


@pytest.mark.unit
def test_client_manager_update_client_reference_schedules_close() -> None:
    """_update_client_reference should schedule background close for old client."""
    state_manager = BLEStateManager()
    lock = RLock()
    thread_coordinator = MagicMock()
    error_handler = MagicMock()

    manager = ClientManager(state_manager, lock, thread_coordinator, error_handler)

    old_client = MagicMock()
    new_client = MagicMock()

    manager._update_client_reference(new_client, old_client)

    # Should have created and started a thread
    thread_coordinator._create_thread.assert_called_once()
    thread_coordinator._start_thread.assert_called_once()


@pytest.mark.unit
def test_client_manager_connect_client_refreshes_services_on_non_callable_probe() -> (
    None
):
    """_connect_client should refresh services when get_characteristic is non-callable."""
    state_manager = BLEStateManager()
    state_lock = RLock()
    manager = ClientManager(
        state_manager=state_manager,
        state_lock=state_lock,
        thread_coordinator=MagicMock(),
        error_handler=MagicMock(),
    )

    services = MagicMock()
    services.get_characteristic = object()
    bleak_client = MagicMock()
    bleak_client.services = services

    client = MagicMock()
    client.bleak_client = bleak_client

    manager._connect_client(client, timeout=1.0)

    client.connect.assert_called_once()
    client._get_services.assert_called_once()


@pytest.mark.unit
def test_reconnect_worker_call_policy_resolves_snake_to_legacy_camelcase() -> None:
    """_call_policy should resolve snake_case requests to legacy camelCase methods."""

    class LegacyCamelPolicy:
        """Policy stub exposing only legacy camelCase methods."""

        def nextAttempt(self) -> tuple[float, bool]:
            """Return a retry delay and continuation flag."""
            return 0.25, False

        def getAttemptCount(self) -> int:
            """Return a stable retry attempt count for assertions."""
            return 7

    policy = LegacyCamelPolicy()
    worker = ReconnectWorker(MagicMock(), policy)  # type: ignore[arg-type]

    assert worker._call_policy("next_attempt") == (0.25, False)
    assert worker._call_policy("get_attempt_count") == 7


@pytest.mark.unit
def test_connection_orchestrator_interrupt_resets_state_and_closes_client() -> None:
    """Interrupts during connect should reset state to DISCONNECTED and close client."""
    state_manager = BLEStateManager()
    state_lock = RLock()
    validator = ConnectionValidator(state_manager, state_lock, MockBLEError)

    client_manager = MagicMock()
    mock_client = MagicMock()
    client_manager._create_client.return_value = mock_client
    client_manager._connect_client.side_effect = KeyboardInterrupt()

    interface = MagicMock()
    interface.BLEError = MockBLEError

    orchestrator = ConnectionOrchestrator(
        interface=interface,
        validator=validator,
        client_manager=client_manager,
        discovery_manager=MagicMock(),
        state_manager=state_manager,
        state_lock=state_lock,
        thread_coordinator=MagicMock(),
    )

    with pytest.raises(KeyboardInterrupt):
        orchestrator._establish_connection(
            address="AA:BB:CC:DD:EE:FF",
            current_address=None,
            register_notifications_func=lambda _client: None,
            on_connected_func=lambda: None,
            on_disconnect_func=lambda _client: None,
        )

    assert state_manager._current_state == ConnectionState.DISCONNECTED
    client_manager._safe_close_client.assert_called_once_with(mock_client)


@pytest.mark.unit
def test_connection_orchestrator_aborts_fallback_when_interface_closing() -> None:
    """Shutdown during direct-connect failure should abort discovery fallback."""
    state_manager = BLEStateManager()
    state_lock = RLock()
    validator = ConnectionValidator(state_manager, state_lock, MockBLEError)

    client_manager = MagicMock()
    direct_client = MagicMock()
    client_manager._create_client.return_value = direct_client

    interface = MagicMock()
    interface.BLEError = MockBLEError
    interface._closed = False
    interface.findDevice.return_value = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")

    connect_attempts = 0

    def _connect_side_effect(*_args, **_kwargs) -> None:
        nonlocal connect_attempts
        connect_attempts += 1
        if connect_attempts == 1:
            interface._closed = True
            raise TimeoutError("direct connect timed out")
        raise AssertionError("fallback connect should not run while closing")

    client_manager._connect_client.side_effect = _connect_side_effect

    orchestrator = ConnectionOrchestrator(
        interface=interface,
        validator=validator,
        client_manager=client_manager,
        discovery_manager=MagicMock(),
        state_manager=state_manager,
        state_lock=state_lock,
        thread_coordinator=MagicMock(),
    )

    with pytest.raises(MockBLEError, match="Cannot connect while interface is closing"):
        orchestrator._establish_connection(
            address="AA:BB:CC:DD:EE:FF",
            current_address=None,
            register_notifications_func=lambda _client: None,
            on_connected_func=lambda: None,
            on_disconnect_func=lambda _client: None,
        )

    assert connect_attempts == 1
    interface.findDevice.assert_not_called()
    client_manager._safe_close_client.assert_called_once_with(direct_client)
    assert state_manager._current_state == ConnectionState.DISCONNECTED


@pytest.mark.unit
def test_transition_failure_to_disconnected_forces_reset_when_transitions_reject() -> None:
    """_transition_failure_to_disconnected should force reset if transition calls fail."""
    state_manager = MagicMock()
    state_manager._current_state = ConnectionState.CONNECTING
    state_manager._transition_to.return_value = False
    state_lock = RLock()
    validator = ConnectionValidator(BLEStateManager(), state_lock, MockBLEError)
    orchestrator = ConnectionOrchestrator(
        interface=MagicMock(BLEError=MockBLEError),
        validator=validator,
        client_manager=MagicMock(),
        discovery_manager=MagicMock(),
        state_manager=state_manager,  # type: ignore[arg-type]
        state_lock=state_lock,
        thread_coordinator=MagicMock(),
    )

    orchestrator._transition_failure_to_disconnected("unit-test")

    state_manager._reset_to_disconnected.assert_called_once_with()


@pytest.mark.unit
def test_finalize_connection_sets_reconnected_event_and_logs_normalized_address() -> (
    None
):
    """_finalize_connection should call on_connected and set reconnected_event for prior sessions."""
    state_manager = BLEStateManager()
    state_lock = RLock()
    assert state_manager._transition_to(ConnectionState.CONNECTING)
    validator = ConnectionValidator(state_manager, state_lock, MockBLEError)
    interface = MagicMock()
    interface.BLEError = MockBLEError
    interface._ever_connected = True
    thread_coordinator = MagicMock()
    orchestrator = ConnectionOrchestrator(
        interface=interface,
        validator=validator,
        client_manager=MagicMock(),
        discovery_manager=MagicMock(),
        state_manager=state_manager,
        state_lock=state_lock,
        thread_coordinator=thread_coordinator,
    )
    client = MagicMock()
    client.isConnected.return_value = True
    on_connected = MagicMock()

    with pytest.MonkeyPatch.context() as patch_ctx:
        patch_ctx.setattr(
            "meshtastic.interfaces.ble.connection.sanitize_address",
            lambda _address: "aa:bb:cc:dd:ee:ff",
        )
        orchestrator._finalize_connection(
            client=client,
            device_address="AA-BB-CC-DD-EE-FF",
            register_notifications_func=lambda _client: None,
            on_connected_func=on_connected,
        )

    on_connected.assert_called_once_with()
    thread_coordinator._set_event.assert_called_once_with("reconnected_event")


@pytest.mark.unit
def test_establish_connection_rejects_whitespace_target_address() -> None:
    """_establish_connection should fail fast for empty/whitespace target addresses."""
    state_manager = BLEStateManager()
    state_lock = RLock()
    validator = ConnectionValidator(state_manager, state_lock, MockBLEError)
    interface = MagicMock()
    interface.BLEError = MockBLEError
    orchestrator = ConnectionOrchestrator(
        interface=interface,
        validator=validator,
        client_manager=MagicMock(),
        discovery_manager=MagicMock(),
        state_manager=state_manager,
        state_lock=state_lock,
        thread_coordinator=MagicMock(),
    )

    with pytest.raises(MockBLEError):
        orchestrator._establish_connection(
            address="   ",
            current_address=None,
            register_notifications_func=lambda _client: None,
            on_connected_func=lambda: None,
            on_disconnect_func=lambda _client: None,
        )


@pytest.mark.unit
def test_connection_orchestrator_rejects_direct_connect_when_interface_already_closed() -> None:
    """_establish_connection should fail before creating a client when shutdown is in progress."""
    state_manager = BLEStateManager()
    state_lock = RLock()
    validator = ConnectionValidator(state_manager, state_lock, MockBLEError)
    client_manager = MagicMock()
    interface = MagicMock()
    interface.BLEError = MockBLEError
    interface._closed = True
    orchestrator = ConnectionOrchestrator(
        interface=interface,
        validator=validator,
        client_manager=client_manager,
        discovery_manager=MagicMock(),
        state_manager=state_manager,
        state_lock=state_lock,
        thread_coordinator=MagicMock(),
    )

    with pytest.raises(MockBLEError, match="Cannot connect while interface is closing"):
        orchestrator._establish_connection(
            address="AA:BB:CC:DD:EE:FF",
            current_address=None,
            register_notifications_func=lambda _client: None,
            on_connected_func=lambda: None,
            on_disconnect_func=lambda _client: None,
        )

    client_manager._create_client.assert_not_called()
    client_manager._connect_client.assert_not_called()
    interface.findDevice.assert_not_called()


@pytest.mark.unit
def test_connection_orchestrator_returns_after_successful_direct_connect() -> None:
    """_establish_connection should return direct client when initial connect succeeds."""
    state_manager = BLEStateManager()
    state_lock = RLock()
    validator = ConnectionValidator(state_manager, state_lock, MockBLEError)
    client_manager = MagicMock()
    direct_client = MagicMock()
    client_manager._create_client.return_value = direct_client

    interface = MagicMock()
    interface.BLEError = MockBLEError
    interface._closed = False

    orchestrator = ConnectionOrchestrator(
        interface=interface,
        validator=validator,
        client_manager=client_manager,
        discovery_manager=MagicMock(),
        state_manager=state_manager,
        state_lock=state_lock,
        thread_coordinator=MagicMock(),
    )
    orchestrator._finalize_connection = MagicMock()  # type: ignore[method-assign]

    result = orchestrator._establish_connection(
        address="AA:BB:CC:DD:EE:FF",
        current_address=None,
        register_notifications_func=lambda _client: None,
        on_connected_func=lambda: None,
        on_disconnect_func=lambda _client: None,
    )

    assert result is direct_client
    interface.findDevice.assert_not_called()
    orchestrator._finalize_connection.assert_called_once()


@pytest.mark.unit
def test_connection_orchestrator_skips_scan_after_direct_device_not_found_for_explicit_address() -> (
    None
):
    """Explicit-address direct device-not-found should skip discovery scan fallback."""
    state_manager = BLEStateManager()
    state_lock = RLock()
    validator = ConnectionValidator(state_manager, state_lock, MockBLEError)
    client_manager = MagicMock()
    direct_client = MagicMock()
    fallback_client = MagicMock()
    client_manager._create_client.side_effect = [direct_client, fallback_client]
    client_manager._connect_client.side_effect = [
        BleakDeviceNotFoundError("AA:BB:CC:DD:EE:FF", "not found"),
        None,
    ]

    interface = MagicMock()
    interface.BLEError = MockBLEError
    interface._closed = False

    orchestrator = ConnectionOrchestrator(
        interface=interface,
        validator=validator,
        client_manager=client_manager,
        discovery_manager=MagicMock(),
        state_manager=state_manager,
        state_lock=state_lock,
        thread_coordinator=MagicMock(),
    )
    orchestrator._finalize_connection = MagicMock()  # type: ignore[method-assign]

    result = orchestrator._establish_connection(
        address="AA:BB:CC:DD:EE:FF",
        current_address=None,
        register_notifications_func=lambda _client: None,
        on_connected_func=lambda: None,
        on_disconnect_func=lambda _client: None,
    )

    assert result is fallback_client
    interface.findDevice.assert_not_called()
    assert client_manager._connect_client.call_count == 2
    orchestrator._finalize_connection.assert_called_once_with(
        fallback_client,
        "AA:BB:CC:DD:EE:FF",
        ANY,
        ANY,
    )


@pytest.mark.unit
def test_connection_orchestrator_uses_discovery_for_non_address_identifier_after_direct_failure() -> (
    None
):
    """Non-address identifiers should still use discovery fallback after direct failures."""
    state_manager = BLEStateManager()
    state_lock = RLock()
    validator = ConnectionValidator(state_manager, state_lock, MockBLEError)
    client_manager = MagicMock()
    direct_client = MagicMock()
    discovered_client = MagicMock()
    client_manager._create_client.side_effect = [direct_client, discovered_client]
    client_manager._connect_client.side_effect = [
        BleakDeviceNotFoundError("mesh-node", "not found"),
        None,
    ]

    interface = MagicMock()
    interface.BLEError = MockBLEError
    interface._closed = False
    interface.findDevice.return_value = SimpleNamespace(address="11:22:33:44:55:66")

    orchestrator = ConnectionOrchestrator(
        interface=interface,
        validator=validator,
        client_manager=client_manager,
        discovery_manager=MagicMock(),
        state_manager=state_manager,
        state_lock=state_lock,
        thread_coordinator=MagicMock(),
    )
    orchestrator._finalize_connection = MagicMock()  # type: ignore[method-assign]

    result = orchestrator._establish_connection(
        address="mesh-node",
        current_address=None,
        register_notifications_func=lambda _client: None,
        on_connected_func=lambda: None,
        on_disconnect_func=lambda _client: None,
    )

    assert result is discovered_client
    interface.findDevice.assert_called_once_with("mesh-node")
    assert client_manager._connect_client.call_count == 2
    orchestrator._finalize_connection.assert_called_once_with(
        discovered_client,
        "11:22:33:44:55:66",
        ANY,
        ANY,
    )


@pytest.mark.unit
def test_connection_orchestrator_handles_bleak_dbus_error_during_connect() -> None:
    """_establish_connection should reset state and re-raise BleakDBusError."""
    state_manager = BLEStateManager()
    state_lock = RLock()
    validator = ConnectionValidator(state_manager, state_lock, MockBLEError)
    client_manager = MagicMock()
    direct_client = MagicMock()
    client_manager._create_client.return_value = direct_client
    client_manager._connect_client.side_effect = BleakDBusError(
        "org.bluez.Error.Failed", []
    )

    interface = MagicMock()
    interface.BLEError = MockBLEError
    interface._closed = False

    orchestrator = ConnectionOrchestrator(
        interface=interface,
        validator=validator,
        client_manager=client_manager,
        discovery_manager=MagicMock(),
        state_manager=state_manager,
        state_lock=state_lock,
        thread_coordinator=MagicMock(),
    )

    with pytest.raises(BleakDBusError):
        orchestrator._establish_connection(
            address="AA:BB:CC:DD:EE:FF",
            current_address=None,
            register_notifications_func=lambda _client: None,
            on_connected_func=lambda: None,
            on_disconnect_func=lambda _client: None,
        )

    assert state_manager._current_state == ConnectionState.DISCONNECTED
    assert client_manager._safe_close_client.call_count == 2


@pytest.mark.unit
def test_reconnect_worker_returns_when_should_abort_is_true() -> None:
    """_attempt_reconnect_loop should exit immediately when _should_abort_reconnect is true."""

    class _Policy:
        @staticmethod
        def _reset() -> None:
            pass

        @staticmethod
        def _get_attempt_count() -> int:
            return 0

    interface = MagicMock()
    interface.auto_reconnect = True
    interface._is_connection_closing = False
    worker = ReconnectWorker(interface, _Policy())  # type: ignore[arg-type]
    worker._should_abort_reconnect = MagicMock(return_value=True)  # type: ignore[method-assign]
    on_exit = MagicMock()

    worker._attempt_reconnect_loop(Event(), on_exit=on_exit)

    on_exit.assert_called_once_with()


@pytest.mark.unit
def test_reconnect_worker_returns_when_attempt_count_is_none() -> None:
    """_attempt_reconnect_loop should exit when policy get_attempt_count returns None."""

    class _Policy:
        @staticmethod
        def _reset() -> None:
            pass

        @staticmethod
        def _get_attempt_count() -> None:
            return None

    interface = MagicMock()
    interface.auto_reconnect = True
    interface._is_connection_closing = False
    interface._is_connection_connected = False
    worker = ReconnectWorker(interface, _Policy())  # type: ignore[arg-type]
    on_exit = MagicMock()

    worker._attempt_reconnect_loop(Event(), on_exit=on_exit)

    on_exit.assert_called_once_with()


@pytest.mark.unit
def test_reconnect_worker_logs_and_returns_when_attempt_count_is_non_int(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_attempt_reconnect_loop should log and exit on non-int attempt counts."""

    class _Policy:
        @staticmethod
        def _reset() -> None:
            pass

        @staticmethod
        def _get_attempt_count() -> float:
            return 1.5

    interface = MagicMock()
    interface.auto_reconnect = True
    interface._is_connection_closing = False
    interface._is_connection_connected = False
    worker = ReconnectWorker(interface, _Policy())  # type: ignore[arg-type]
    on_exit = MagicMock()

    with caplog.at_level(logging.ERROR):
        worker._attempt_reconnect_loop(Event(), on_exit=on_exit)

    assert "returned non-int value" in caplog.text
    on_exit.assert_called_once_with()


@pytest.mark.unit
def test_reconnect_worker_returns_when_interface_already_connected() -> None:
    """_attempt_reconnect_loop should stop when interface is already connected."""

    class _Policy:
        @staticmethod
        def _reset() -> None:
            pass

        @staticmethod
        def _get_attempt_count() -> int:
            return 0

    interface = MagicMock()
    interface.auto_reconnect = True
    interface._is_connection_closing = False
    interface._is_connection_connected = True
    worker = ReconnectWorker(interface, _Policy())  # type: ignore[arg-type]
    on_exit = MagicMock()

    worker._attempt_reconnect_loop(Event(), on_exit=on_exit)

    on_exit.assert_called_once_with()
