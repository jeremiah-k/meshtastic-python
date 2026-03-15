"""Integration scenario tests for BLE to strengthen confidence in changed code paths."""

from threading import RLock
from unittest.mock import MagicMock

import pytest

try:
    from meshtastic.interfaces.ble.connection import ClientManager, ConnectionValidator
    from meshtastic.interfaces.ble.constants import BLEConfig
    from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
except ImportError:
    pytest.skip("BLE dependencies not available", allow_module_level=True)

MAX_REASONABLE_JOIN_TIMEOUT_SECONDS = 10


@pytest.mark.unit
def test_state_manager_full_connection_lifecycle() -> None:
    """Test complete connection state machine lifecycle from DISCONNECTED to DISCONNECTED."""
    state_manager = BLEStateManager()

    # Initial state
    assert state_manager._current_state == ConnectionState.DISCONNECTED
    assert not state_manager._is_closing
    assert state_manager._can_connect

    # Connect flow
    assert state_manager._transition_to(ConnectionState.CONNECTING)
    assert not state_manager._can_connect  # Can't start another connection
    assert not state_manager._is_closing

    assert state_manager._transition_to(ConnectionState.CONNECTED)
    assert not state_manager._can_connect
    assert not state_manager._is_closing

    # Disconnect flow
    assert state_manager._transition_to(ConnectionState.DISCONNECTING)
    assert state_manager._is_closing
    assert not state_manager._can_connect

    assert state_manager._transition_to(ConnectionState.DISCONNECTED)
    assert not state_manager._is_closing
    assert state_manager._can_connect


@pytest.mark.unit
def test_state_manager_error_recovery_path() -> None:
    """Test state machine can recover from ERROR state to DISCONNECTED."""
    state_manager = BLEStateManager()

    # Simulate connection attempt that fails
    assert state_manager._transition_to(ConnectionState.CONNECTING)
    assert state_manager._transition_to(ConnectionState.ERROR)

    # Should be able to recover
    assert state_manager._transition_to(ConnectionState.DISCONNECTING)
    assert state_manager._transition_to(ConnectionState.DISCONNECTED)

    # Should be able to try again
    assert state_manager._can_connect


@pytest.mark.unit
def test_validator_and_state_manager_integration() -> None:
    """Test ConnectionValidator correctly interprets BLEStateManager states."""
    state_manager = BLEStateManager()
    lock = RLock()

    class TestError(Exception):
        pass

    validator = ConnectionValidator(state_manager, lock, TestError)

    # Can validate when disconnected
    validator._validate_connection_request()

    # Block during connecting
    state_manager._transition_to(ConnectionState.CONNECTING)
    with pytest.raises(TestError, match="Already connected or connection in progress"):
        validator._validate_connection_request()

    # Block during connected
    state_manager._transition_to(ConnectionState.CONNECTED)
    with pytest.raises(TestError, match="Already connected or connection in progress"):
        validator._validate_connection_request()

    # Block during closing
    state_manager._transition_to(ConnectionState.DISCONNECTING)
    with pytest.raises(TestError, match="Cannot connect while interface is closing"):
        validator._validate_connection_request()

    # Can validate again after disconnect
    state_manager._transition_to(ConnectionState.DISCONNECTED)
    validator._validate_connection_request()


@pytest.mark.unit
def test_constants_getattr_integration_with_bleconfig() -> None:
    """Test that constants module __getattr__ correctly delegates to BLEConfig."""
    # Test accessing a new config value through __getattr__
    # This simulates the scenario where BLEConfig gains a new attribute
    # but the module-level alias wasn't created yet

    # Patch BLEConfig to add a test attribute
    original_value = getattr(BLEConfig, "RUNNER_IDLE_WAKE_INTERVAL_SECONDS", None)
    assert original_value is not None  # Should exist in actual config

    # Access through module should work via __getattr__
    from meshtastic.interfaces.ble import constants

    value = constants.RUNNER_IDLE_WAKE_INTERVAL_SECONDS
    assert value == BLEConfig.RUNNER_IDLE_WAKE_INTERVAL_SECONDS


@pytest.mark.unit
def test_client_manager_handles_concurrent_updates() -> None:
    """Test ClientManager handles rapid client updates safely."""
    state_manager = BLEStateManager()
    lock = RLock()
    thread_coordinator = MagicMock()

    def _started_thread() -> MagicMock:
        thread = MagicMock()
        thread.ident = 1
        thread.is_alive.return_value = True
        return thread

    thread_coordinator._create_thread.side_effect = lambda **_kwargs: _started_thread()
    thread_coordinator._start_thread.side_effect = lambda _thread: None
    error_handler = MagicMock()

    manager = ClientManager(state_manager, lock, thread_coordinator, error_handler)

    # Simulate rapid client replacements
    clients = [MagicMock() for _ in range(5)]

    for i in range(1, len(clients)):
        manager._update_client_reference(clients[i], clients[i - 1])

    # Should have scheduled close for each old client
    assert thread_coordinator._create_thread.call_count == 4
    assert thread_coordinator._start_thread.call_count == 4


@pytest.mark.unit
def test_connection_timeout_configuration_consistency() -> None:
    """Verify timeout constants are consistent and reasonable across the stack."""
    from meshtastic.interfaces.ble.constants import (
        AWAIT_TIMEOUT_BUFFER_SECONDS,
        DIRECT_CONNECT_TIMEOUT_SECONDS,
        DISCONNECT_TIMEOUT_SECONDS,
        EVENT_THREAD_JOIN_TIMEOUT,
        RECEIVE_THREAD_JOIN_TIMEOUT,
    )

    # Basic sanity checks
    assert BLEConfig.CONNECTION_TIMEOUT > DIRECT_CONNECT_TIMEOUT_SECONDS
    assert BLEConfig.GATT_IO_TIMEOUT > 0
    assert DISCONNECT_TIMEOUT_SECONDS > 0
    assert RECEIVE_THREAD_JOIN_TIMEOUT > 0
    assert EVENT_THREAD_JOIN_TIMEOUT > 0
    assert AWAIT_TIMEOUT_BUFFER_SECONDS > 0

    # Await buffer should be smaller than connection timeout
    assert AWAIT_TIMEOUT_BUFFER_SECONDS < BLEConfig.CONNECTION_TIMEOUT

    # Join timeouts should be reasonable (not too long)
    assert RECEIVE_THREAD_JOIN_TIMEOUT <= MAX_REASONABLE_JOIN_TIMEOUT_SECONDS
    assert EVENT_THREAD_JOIN_TIMEOUT <= MAX_REASONABLE_JOIN_TIMEOUT_SECONDS


@pytest.mark.unit
def test_state_manager_invalid_transition_rejection() -> None:
    """Test that invalid state transitions are properly rejected."""
    state_manager = BLEStateManager()

    # Can't disconnect from disconnected
    assert not state_manager._transition_to(ConnectionState.DISCONNECTING)

    # Can't go directly to connected without connecting first
    assert not state_manager._transition_to(ConnectionState.CONNECTED)

    # Must go through proper flow
    assert state_manager._transition_to(ConnectionState.CONNECTING)
    assert state_manager._transition_to(ConnectionState.CONNECTED)


@pytest.mark.unit
def test_backwards_compatible_constant_access() -> None:
    """Test that legacy constant access patterns still work."""
    from meshtastic.interfaces.ble import constants

    # Module-level aliases should match class attributes
    module_timeout = constants.BLE_SCAN_TIMEOUT
    class_timeout = constants.BLEConfig.BLE_SCAN_TIMEOUT
    assert module_timeout == class_timeout

    # Error messages should be accessible
    assert constants.ERROR_TIMEOUT
    assert constants.ERROR_READING_BLE
    assert constants.ERROR_WRITING_BLE

    # UUIDs should be accessible
    assert constants.SERVICE_UUID
    assert constants.TORADIO_UUID
    assert constants.FROMRADIO_UUID


@pytest.mark.unit
def test_connection_validator_with_normalized_addresses() -> None:
    """Test ConnectionValidator handles address normalization correctly."""
    state_manager = BLEStateManager()
    lock = RLock()

    class TestError(Exception):
        pass

    validator = ConnectionValidator(state_manager, lock, TestError)

    # Create mock client with various address formats
    mock_client = MagicMock()
    mock_client.isConnected.return_value = True
    mock_bleak = MagicMock()
    mock_bleak.address = "AA:BB:CC:DD:EE:FF"
    mock_client.bleak_client = mock_bleak

    # Should match regardless of formatting
    variants = [
        "AA:BB:CC:DD:EE:FF",
        "aa:bb:cc:dd:ee:ff",
        "AA-BB-CC-DD-EE-FF",
        "AABBCCDDEEFF",
    ]

    for variant in variants:
        result = validator._check_existing_client(mock_client, variant, None)
        assert result, f"Address variant '{variant}' should match when normalized"


@pytest.mark.unit
def test_ble_config_can_be_modified_at_runtime() -> None:
    """Test that BLEConfig attributes can be modified; module-level aliases are snapshots."""
    original_timeout = BLEConfig.CONNECTION_TIMEOUT

    try:
        # Should be able to modify
        BLEConfig.CONNECTION_TIMEOUT = 120.0
        assert BLEConfig.CONNECTION_TIMEOUT == 120.0

        # Module-level alias won't change (it's an import-time snapshot).
        from meshtastic.interfaces.ble import constants

        module_alias_snapshot = constants.CONNECTION_TIMEOUT
        assert module_alias_snapshot == original_timeout
    finally:
        # Restore
        BLEConfig.CONNECTION_TIMEOUT = original_timeout


@pytest.mark.unit
def test_error_state_allows_transition_to_disconnecting() -> None:
    """Regression test: ERROR state should allow transition to DISCONNECTING for cleanup."""
    state_manager = BLEStateManager()

    # Get to ERROR state
    assert state_manager._transition_to(ConnectionState.CONNECTING)
    assert state_manager._transition_to(ConnectionState.ERROR)

    # Critical: Should allow DISCONNECTING from ERROR for cleanup
    assert state_manager._transition_to(ConnectionState.DISCONNECTING)
    assert state_manager._is_closing

    # Complete cleanup
    assert state_manager._transition_to(ConnectionState.DISCONNECTED)
    assert not state_manager._is_closing
