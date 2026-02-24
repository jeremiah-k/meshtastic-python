"""Edge case tests for BLE connection management."""

from threading import RLock
from unittest.mock import MagicMock
import pytest

try:
    from meshtastic.interfaces.ble.connection import (
        AWAIT_TIMEOUT_BUFFER_SECONDS,
        ClientManager,
        ConnectionValidator,
        DIRECT_CONNECT_TIMEOUT_SECONDS,
    )
    from meshtastic.interfaces.ble.constants import BLEConfig
    from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
except ImportError:
    pytest.skip("BLE dependencies not available", allow_module_level=True)


@pytest.mark.unit
def test_connection_validator_allows_connect_when_disconnected():
    """ConnectionValidator should allow connection when state is DISCONNECTED."""
    state_manager = BLEStateManager()
    lock = RLock()

    class MockBLEError(Exception):
        pass

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    # Should not raise when disconnected
    validator._validate_connection_request()


@pytest.mark.unit
def test_connection_validator_blocks_when_closing():
    """ConnectionValidator should block connection when interface is closing."""
    state_manager = BLEStateManager()
    lock = RLock()

    class MockBLEError(Exception):
        pass

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    # Transition to DISCONNECTING state
    state_manager._transition_to(ConnectionState.CONNECTING)
    state_manager._transition_to(ConnectionState.CONNECTED)
    state_manager._transition_to(ConnectionState.DISCONNECTING)

    # Should raise when closing
    with pytest.raises(MockBLEError, match="Cannot connect while interface is closing"):
        validator._validate_connection_request()


@pytest.mark.unit
def test_connection_validator_blocks_when_already_connecting():
    """ConnectionValidator should block when already connecting."""
    state_manager = BLEStateManager()
    lock = RLock()

    class MockBLEError(Exception):
        pass

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    # Transition to CONNECTING state
    state_manager._transition_to(ConnectionState.CONNECTING)

    # Should raise when already connecting
    with pytest.raises(
        MockBLEError, match="Already connected or connection in progress"
    ):
        validator._validate_connection_request()


@pytest.mark.unit
def test_connection_validator_check_existing_client_none_request():
    """check_existing_client should accept any connected client when request is None."""
    state_manager = BLEStateManager()
    lock = RLock()

    class MockBLEError(Exception):
        pass

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    # Create mock client
    mock_client = MagicMock()
    mock_client.isConnected.return_value = True

    # Should return True for None request
    assert validator._check_existing_client(mock_client, None, None, None)


@pytest.mark.unit
def test_connection_validator_check_existing_client_disconnected():
    """check_existing_client should return False for disconnected client."""
    state_manager = BLEStateManager()
    lock = RLock()

    class MockBLEError(Exception):
        pass

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    mock_client = MagicMock()
    mock_client.isConnected.return_value = False

    assert not validator._check_existing_client(mock_client, "address", None, None)


@pytest.mark.unit
def test_connection_validator_check_existing_client_none_client():
    """check_existing_client should return False when client is None."""
    state_manager = BLEStateManager()
    lock = RLock()

    class MockBLEError(Exception):
        pass

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    assert not validator._check_existing_client(None, "address", None, None)


@pytest.mark.unit
def test_client_manager_initialization():
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
def test_direct_connect_timeout_is_reasonable():
    """DIRECT_CONNECT_TIMEOUT_SECONDS should be shorter than CONNECTION_TIMEOUT."""
    assert DIRECT_CONNECT_TIMEOUT_SECONDS < BLEConfig.CONNECTION_TIMEOUT
    assert DIRECT_CONNECT_TIMEOUT_SECONDS > 0


@pytest.mark.unit
def test_await_timeout_buffer_is_positive():
    """AWAIT_TIMEOUT_BUFFER_SECONDS should be positive to allow timeout margin."""
    assert AWAIT_TIMEOUT_BUFFER_SECONDS > 0
    assert isinstance(AWAIT_TIMEOUT_BUFFER_SECONDS, float)


@pytest.mark.unit
def test_connection_validator_check_existing_client_matching_address():
    """check_existing_client should return True when addresses match."""
    state_manager = BLEStateManager()
    lock = RLock()

    class MockBLEError(Exception):
        pass

    validator = ConnectionValidator(state_manager, lock, MockBLEError)

    mock_client = MagicMock()
    mock_client.isConnected.return_value = True
    mock_bleak_client = MagicMock()
    mock_bleak_client.address = "AA:BB:CC:DD:EE:FF"
    mock_client.bleak_client = mock_bleak_client

    # Should return True when addresses match (case-insensitive, punctuation-agnostic)
    assert validator._check_existing_client(
        mock_client,
        "aabbccddeeff",  # normalized
        "aabbccddeeff",  # last connection request
        "AA:BB:CC:DD:EE:FF",  # original address
    )


@pytest.mark.unit
def test_client_manager_safe_close_client_handles_none():
    """_safe_close_client should handle None client gracefully."""
    from threading import Event

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
def test_client_manager_update_client_reference_same_client():
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
def test_client_manager_update_client_reference_schedules_close():
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
