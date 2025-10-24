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


class TestBLEInterfaceStateIntegration:
    """Test integration between BLEStateManager and BLEInterface (Phase 2)."""
    
    def test_state_manager_standalone_initialization(self):
        """Test BLEStateManager initialization without full BLEInterface."""
        from meshtastic.ble_interface import BLEStateManager
        
        # Test state manager can be created and used independently
        manager = BLEStateManager()
        
        # Check initial state
        assert manager.state == ConnectionState.DISCONNECTED
        assert manager.can_connect
        assert not manager.is_connected
        assert not manager.is_closing
        assert manager.client is None
        
        # Test state transitions work
        assert manager.transition_to(ConnectionState.CONNECTING)
        assert manager.state == ConnectionState.CONNECTING
        assert not manager.can_connect
        
    def test_state_based_property_usage(self):
        """Test that state-based properties work as expected for Phase 2 migration."""
        from meshtastic.ble_interface import BLEStateManager
        
        manager = BLEStateManager()
        
        # Test initial state properties
        assert manager.state == ConnectionState.DISCONNECTED
        assert not manager.is_connected
        assert not manager.is_closing
        assert manager.can_connect
        
        # Test transitioning through states
        manager.transition_to(ConnectionState.CONNECTING)
        assert manager.state == ConnectionState.CONNECTING
        assert not manager.is_connected
        assert not manager.is_closing
        assert not manager.can_connect
        
        manager.transition_to(ConnectionState.CONNECTED)
        assert manager.state == ConnectionState.CONNECTED
        assert manager.is_connected
        assert not manager.is_closing
        assert not manager.can_connect
        
        manager.transition_to(ConnectionState.DISCONNECTING)
        assert manager.state == ConnectionState.DISCONNECTING
        assert not manager.is_connected
        assert manager.is_closing
        assert not manager.can_connect
        
        # Test ERROR state also counts as closing
        manager.transition_to(ConnectionState.ERROR)
        assert manager.state == ConnectionState.ERROR
        assert not manager.is_connected
        assert manager.is_closing
        assert not manager.can_connect
    
    def test_client_management_with_states(self):
        """Test client management works correctly with state transitions."""
        from meshtastic.ble_interface import BLEStateManager
        from unittest.mock import MagicMock
        
        manager = BLEStateManager()
        mock_client = MagicMock()
        
        # Should be able to set client when transitioning to CONNECTED (via CONNECTING)
        assert manager.transition_to(ConnectionState.CONNECTING)
        assert manager.transition_to(ConnectionState.CONNECTED, client=mock_client)
        assert manager.client is mock_client
        assert manager.is_connected
        
        # Client should be cleared when transitioning to DISCONNECTED
        assert manager.transition_to(ConnectionState.DISCONNECTED)
        assert manager.client is None
        assert manager.state == ConnectionState.DISCONNECTED
        
        # Should not be able to set client for invalid states
        assert not manager.transition_to(ConnectionState.DISCONNECTED, client=mock_client)
        assert manager.client is None
    
    def test_state_transition_validation(self):
        """Test state transition validation for connection scenarios."""
        from meshtastic.ble_interface import BLEStateManager
        
        manager = BLEStateManager()
        
        # Valid transitions from DISCONNECTED
        assert manager.transition_to(ConnectionState.CONNECTING)
        assert manager.transition_to(ConnectionState.DISCONNECTING)  # Can close without connecting
        
        # Reset to DISCONNECTED
        manager.transition_to(ConnectionState.DISCONNECTED)
        
        # Invalid transitions from DISCONNECTED
        assert not manager.transition_to(ConnectionState.CONNECTED)  # Must go through CONNECTING first
        assert not manager.transition_to(ConnectionState.RECONNECTING)  # Must have been connected first
        # Note: ERROR state is actually allowed from DISCONNECTED in current implementation


class TestPhase3LockConsolidation:
    """Test Phase 3 lock consolidation and concurrent scenarios."""
    
    def test_unified_lock_thread_safety(self):
        """Test that unified state lock provides thread safety for concurrent operations."""
        from meshtastic.ble_interface import BLEStateManager
        from unittest.mock import MagicMock
        import threading
        import time
        
        manager = BLEStateManager()
        results = []
        errors = []
        
        def worker(worker_id):
            try:
                for i in range(10):
                    # Simulate state transitions
                    if i % 3 == 0:
                        success = manager.transition_to(ConnectionState.CONNECTING)
                    elif i % 3 == 1:
                        mock_client = MagicMock()
                        success = manager.transition_to(ConnectionState.CONNECTED, client=mock_client)
                    else:
                        success = manager.transition_to(ConnectionState.DISCONNECTED)
                    
                    results.append((worker_id, i, manager.state, success))
                    time.sleep(0.001)  # Small delay to encourage interleaving
            except Exception as e:
                errors.append((worker_id, str(e)))
        
        # Create multiple threads
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
        
        # Verify all operations completed
        assert len(results) == 50, f"Expected 50 operations, got {len(results)}"
        
        # Verify final state is valid
        assert manager.state in ConnectionState
        
        # Verify client consistency
        if manager.state == ConnectionState.CONNECTED:
            assert manager.client is not None
        else:
            assert manager.client is None
    
    def test_reentrant_lock_behavior(self):
        """Test that unified lock supports reentrancy for nested operations."""
        from meshtastic.ble_interface import BLEStateManager
        from unittest.mock import MagicMock
        
        manager = BLEStateManager()
        mock_client = MagicMock()
        
        # Test nested lock acquisition through state manager methods
        def nested_operation():
            # This should work without deadlock due to reentrant lock
            assert manager.transition_to(ConnectionState.CONNECTING)
            assert manager.transition_to(ConnectionState.CONNECTED, client=mock_client)
            assert manager.client is mock_client
            assert manager.is_connected
        
        # Acquire lock manually then call nested operation
        with manager._state_lock:
            nested_operation()
        
        # Verify state is consistent
        assert manager.state == ConnectionState.CONNECTED
        assert manager.client is mock_client
    
    def test_lock_contention_resolution(self):
        """Test that lock contention is handled gracefully."""
        from meshtastic.ble_interface import BLEStateManager
        import threading
        import time
        
        manager = BLEStateManager()
        contention_count = [0]
        
        def contending_worker():
            try:
                # Try to acquire lock and perform operation
                with manager._state_lock:
                    # Simulate some work
                    time.sleep(0.01)
                    manager.transition_to(ConnectionState.CONNECTING)
                    manager.transition_to(ConnectionState.DISCONNECTED)
            except:
                contention_count[0] += 1
        
        # Create multiple contending threads
        threads = []
        for i in range(10):
            thread = threading.Thread(target=contending_worker)
            threads.append(thread)
            thread.start()
        
        # Wait for completion
        for thread in threads:
            thread.join()
        
        # Verify no contention errors
        assert contention_count[0] == 0
        
        # Verify final state is consistent
        assert manager.state == ConnectionState.DISCONNECTED


class TestPhase4PerformanceOptimization:
    """Test Phase 4 performance optimization and validation."""
    
    def test_state_transition_performance(self):
        """Test that state transitions are performant."""
        from meshtastic.ble_interface import BLEStateManager
        from unittest.mock import MagicMock
        import time
        
        manager = BLEStateManager()
        mock_client = MagicMock()
        
        # Measure state transition performance
        iterations = 1000
        start_time = time.perf_counter()
        
        for i in range(iterations):
            # Cycle through states
            manager.transition_to(ConnectionState.CONNECTING)
            manager.transition_to(ConnectionState.CONNECTED, client=mock_client)
            manager.transition_to(ConnectionState.DISCONNECTED)
        
        end_time = time.perf_counter()
        elapsed = end_time - start_time
        
        # Should complete 3000 transitions quickly (less than 1 second)
        assert elapsed < 1.0, f"State transitions too slow: {elapsed:.3f}s for {iterations * 3} transitions"
        
        # Calculate average transition time
        avg_time = elapsed / (iterations * 3)
        assert avg_time < 0.0001, f"Average transition time too high: {avg_time:.6f}s"
        
        print(f"Performance: {iterations * 3} transitions in {elapsed:.3f}s, avg: {avg_time:.6f}s")
    
    def test_lock_contention_performance(self):
        """Test performance under lock contention."""
        from meshtastic.ble_interface import BLEStateManager
        import threading
        import time
        
        manager = BLEStateManager()
        results = []
        
        def worker(worker_id):
            start_time = time.perf_counter()
            operations = 0
            
            for i in range(100):
                # Simulate realistic state operations
                if manager.transition_to(ConnectionState.CONNECTING):
                    operations += 1
                if manager.transition_to(ConnectionState.CONNECTED):
                    operations += 1
                if manager.transition_to(ConnectionState.DISCONNECTED):
                    operations += 1
                
                # Small delay to simulate real work
                time.sleep(0.001)
            
            end_time = time.perf_counter()
            results.append({
                'worker_id': worker_id,
                'operations': operations,
                'time': end_time - start_time
            })
        
        # Create multiple contending threads
        threads = []
        start_time = time.perf_counter()
        
        for i in range(5):
            thread = threading.Thread(target=worker, args=(i,))
            threads.append(thread)
            thread.start()
        
        # Wait for completion
        for thread in threads:
            thread.join()
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        # Verify reasonable performance under contention
        assert total_time < 5.0, f"Contention test too slow: {total_time:.3f}s"
        
        # Verify all operations completed
        total_operations = sum(r['operations'] for r in results)
        expected_operations = 5 * 100 * 3  # 5 workers * 100 iterations * 3 operations
        assert total_operations >= expected_operations * 0.8, f"Too many failed operations: {total_operations}/{expected_operations}"
        
        print(f"Contention performance: {total_operations} operations in {total_time:.3f}s")
    
    def test_memory_efficiency(self):
        """Test that state manager doesn't leak memory."""
        from meshtastic.ble_interface import BLEStateManager
        from unittest.mock import MagicMock
        import gc
        import sys
        
        # Force garbage collection
        gc.collect()
        initial_objects = len(gc.get_objects())
        
        # Create and destroy many state managers
        for i in range(100):
            manager = BLEStateManager()
            mock_client = MagicMock()
            
            # Perform state operations
            manager.transition_to(ConnectionState.CONNECTING)
            manager.transition_to(ConnectionState.CONNECTED, client=mock_client)
            manager.transition_to(ConnectionState.DISCONNECTED)
            
            # Delete reference
            del manager
        
        # Force garbage collection
        gc.collect()
        final_objects = len(gc.get_objects())
        
        # Should not have significant memory growth
        object_growth = final_objects - initial_objects
        assert object_growth < 1000, f"Potential memory leak: {object_growth} objects created"
        
        print(f"Memory efficiency: {object_growth} objects created for 100 state managers")
    
    def test_property_access_performance(self):
        """Test that state property access is fast."""
        from meshtastic.ble_interface import BLEStateManager
        import time
        
        manager = BLEStateManager()
        
        # Measure property access performance
        iterations = 10000
        start_time = time.perf_counter()
        
        for i in range(iterations):
            # Access all properties
            _ = manager.state
            _ = manager.is_connected
            _ = manager.is_closing
            _ = manager.can_connect
            _ = manager.client
        
        end_time = time.perf_counter()
        elapsed = end_time - start_time
        
        # Property access should be very fast
        avg_time = elapsed / (iterations * 5)  # 5 properties per iteration
        assert avg_time < 0.000001, f"Property access too slow: {avg_time:.9f}s"
        
        print(f"Property access: {iterations * 5} accesses in {elapsed:.3f}s, avg: {avg_time:.9f}s")