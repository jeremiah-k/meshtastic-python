"""Tests for BLEStateManager state machine functionality."""

import pytest
import threading
import time
from unittest.mock import Mock

from meshtastic.ble_interface import ConnectionState, BLEStateManager


class TestBLEStateManager:
    """Test cases for BLEStateManager class."""

    def test_initial_state(self):
        """Test that state manager starts in DISCONNECTED state."""
        manager = BLEStateManager()
        assert manager.state == ConnectionState.DISCONNECTED
        assert not manager.is_connected
        assert not manager.is_closing
        assert manager.can_connect
        assert manager.client is None

    def test_state_properties(self):
        """Test state-based property methods."""
        manager = BLEStateManager()
        
        # Test DISCONNECTED state
        manager._state = ConnectionState.DISCONNECTED
        assert not manager.is_connected
        assert not manager.is_closing
        assert manager.can_connect
        
        # Test CONNECTED state
        manager._state = ConnectionState.CONNECTED
        assert manager.is_connected
        assert not manager.is_closing
        assert not manager.can_connect
        
        # Test DISCONNECTING state
        manager._state = ConnectionState.DISCONNECTING
        assert not manager.is_connected
        assert manager.is_closing
        assert not manager.can_connect
        
        # Test ERROR state
        manager._state = ConnectionState.ERROR
        assert not manager.is_connected
        assert manager.is_closing
        assert not manager.can_connect

    def test_valid_transitions(self):
        """Test all valid state transitions."""
        manager = BLEStateManager()
        
        # DISCONNECTED → CONNECTING
        assert manager.transition_to(ConnectionState.CONNECTING)
        assert manager.state == ConnectionState.CONNECTING
        
        # CONNECTING → CONNECTED
        assert manager.transition_to(ConnectionState.CONNECTED)
        assert manager.state == ConnectionState.CONNECTED
        
        # CONNECTED → DISCONNECTING
        assert manager.transition_to(ConnectionState.DISCONNECTING)
        assert manager.state == ConnectionState.DISCONNECTING
        
        # DISCONNECTING → DISCONNECTED
        assert manager.transition_to(ConnectionState.DISCONNECTED)
        assert manager.state == ConnectionState.DISCONNECTED
        
        # DISCONNECTED → ERROR
        assert manager.transition_to(ConnectionState.ERROR)
        assert manager.state == ConnectionState.ERROR
        
        # ERROR → CONNECTING
        assert manager.transition_to(ConnectionState.CONNECTING)
        assert manager.state == ConnectionState.CONNECTING

    def test_invalid_transitions(self):
        """Test that invalid transitions are rejected."""
        manager = BLEStateManager()
        
        # Try to transition to same state (should be invalid)
        assert not manager.transition_to(ConnectionState.DISCONNECTED)
        assert manager.state == ConnectionState.DISCONNECTED
        
        # Try invalid transition from DISCONNECTED
        assert not manager.transition_to(ConnectionState.CONNECTED)
        assert manager.state == ConnectionState.DISCONNECTED
        
        # Move to CONNECTED and try invalid transition
        assert manager.transition_to(ConnectionState.CONNECTING)
        assert manager.transition_to(ConnectionState.CONNECTED)
        assert not manager.transition_to(ConnectionState.CONNECTING)  # Can't go back to CONNECTING
        assert manager.state == ConnectionState.CONNECTED

    def test_client_management(self):
        """Test client reference management during transitions."""
        manager = BLEStateManager()
        mock_client = Mock()
        
        # Client should be set when provided in transition
        assert manager.transition_to(ConnectionState.CONNECTING, mock_client)
        assert manager.client == mock_client
        
        # Client should be preserved through transitions
        assert manager.transition_to(ConnectionState.CONNECTED)
        assert manager.client == mock_client
        
        # Client should be cleared when transitioning to DISCONNECTED
        assert manager.transition_to(ConnectionState.DISCONNECTED)
        assert manager.client is None

    def test_thread_safety(self):
        """Test concurrent state access is thread-safe."""
        manager = BLEStateManager()
        results = []
        errors = []
        
        def worker(worker_id):
            try:
                for i in range(100):
                    # Try to transition states concurrently
                    if i % 2 == 0:
                        success = manager.transition_to(ConnectionState.CONNECTING)
                    else:
                        success = manager.transition_to(ConnectionState.DISCONNECTED)
                    results.append((worker_id, i, success, manager.state.value))
                    time.sleep(0.001)  # Small delay to increase contention
            except Exception as e:
                errors.append((worker_id, str(e)))
        
        # Start multiple threads
        threads = []
        for i in range(5):
            thread = threading.Thread(target=worker, args=(i,))
            threads.append(thread)
            thread.start()
        
        # Wait for all threads to complete
        for thread in threads:
            thread.join()
        
        # Verify no errors occurred
        assert len(errors) == 0, f"Errors occurred: {errors}"
        
        # Verify final state is valid
        assert manager.state in ConnectionState
        
        # Verify all transitions were either successful or failed gracefully
        assert len(results) == 500  # 5 workers * 100 iterations each

    def test_state_transition_logging(self, caplog):
        """Test that state transitions are properly logged."""
        manager = BLEStateManager()
        
        with caplog.at_level("DEBUG"):
            manager.transition_to(ConnectionState.CONNECTING)
            manager.transition_to(ConnectionState.CONNECTED)
        
        # Check that transitions were logged
        assert "State transition: disconnected → connecting" in caplog.text
        assert "State transition: connecting → connected" in caplog.text

    def test_invalid_transition_logging(self, caplog):
        """Test that invalid transitions are logged as warnings."""
        manager = BLEStateManager()
        
        with caplog.at_level("WARNING"):
            manager.transition_to(ConnectionState.CONNECTED)  # Invalid from DISCONNECTED
        
        # Check that invalid transition was logged as warning
        assert "Invalid state transition: disconnected → connected" in caplog.text

    def test_reentrant_lock_behavior(self):
        """Test that reentrant lock allows nested acquisitions."""
        manager = BLEStateManager()
        
        def nested_operation():
            # This should work with reentrant lock
            with manager._state_lock:
                with manager._state_lock:
                    return manager.state
        
        # Should not deadlock
        assert nested_operation() == ConnectionState.DISCONNECTED

    def test_all_state_values(self):
        """Test that all ConnectionState enum values are accessible."""
        expected_states = {
            "disconnected": ConnectionState.DISCONNECTED,
            "connecting": ConnectionState.CONNECTING,
            "connected": ConnectionState.CONNECTED,
            "disconnecting": ConnectionState.DISCONNECTING,
            "reconnecting": ConnectionState.RECONNECTING,
            "error": ConnectionState.ERROR
        }
        
        for value, state in expected_states.items():
            assert state.value == value
            assert isinstance(state, ConnectionState)

    def test_state_consistency_after_error(self):
        """Test state consistency after error transitions."""
        manager = BLEStateManager()
        
        # Simulate error during connection
        manager.transition_to(ConnectionState.CONNECTING)
        manager.transition_to(ConnectionState.ERROR)
        
        assert manager.state == ConnectionState.ERROR
        assert not manager.is_connected
        assert manager.is_closing
        assert not manager.can_connect
        
        # Should be able to recover from error
        manager.transition_to(ConnectionState.DISCONNECTED)
        assert manager.state == ConnectionState.DISCONNECTED
        assert manager.can_connect