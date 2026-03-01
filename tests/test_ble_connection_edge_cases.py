"""Edge case tests for BLE connection management."""

from threading import Event, RLock
from unittest.mock import MagicMock

import pytest

try:
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
