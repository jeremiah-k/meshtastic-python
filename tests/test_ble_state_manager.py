"""Tests for BLEStateManager state machine functionality."""

import threading
import time
from unittest.mock import MagicMock, Mock

from meshtastic.ble_interface import (  # type: ignore[import-untyped]
    BLEStateManager,
    ConnectionState,
)


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
        assert not manager.transition_to(
            ConnectionState.CONNECTING
        )  # Can't go back to CONNECTING
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
            """
            Perform repeated state transition attempts in a worker thread and record outcomes.

            This worker runs 100 iterations; on even iterations it attempts to transition the shared
            manager to ConnectionState.CONNECTING, and on odd iterations to ConnectionState.DISCONNECTED.
            Each attempt's result is appended to the shared `results` list as a tuple
            (worker_id, iteration_index, success, current_state_value). Any exception raised during
            execution is appended to the shared `errors` list as (worker_id, error_message).

            Parameters
            ----------
                worker_id (int): Identifier for this worker used when recording results and errors.

            """
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
        """
        Verify that attempting an invalid state transition emits a warning log.

        Specifically, transitioning from DISCONNECTED to CONNECTED should produce a WARNING log message: 
        "Invalid state transition: disconnected → connected".
        """
        manager = BLEStateManager()

        with caplog.at_level("WARNING"):
            manager.transition_to(
                ConnectionState.CONNECTED
            )  # Invalid from DISCONNECTED

        # Check that invalid transition was logged as warning
        assert "Invalid state transition: disconnected → connected" in caplog.text

    def test_reentrant_lock_behavior(self):
        """Test that reentrant lock allows nested acquisitions."""
        manager = BLEStateManager()

        def nested_operation():
            # This should work with reentrant lock
            """
            Read the manager's current connection state while exercising the manager's reentrant state lock.

            Returns:
                ConnectionState: The current connection state.

            """
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
            "error": ConnectionState.ERROR,
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
        """
        Verify BLEStateManager can be instantiated standalone and exhibits expected initial properties and basic transition behavior.

        Checks that a newly created manager starts in DISCONNECTED with can_connect True, is_connected False, is_closing False, 
        and no client, and that transitioning to CONNECTING succeeds and updates the state and can_connect flag.
        """

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
        assert not manager.transition_to(
            ConnectionState.DISCONNECTED, client=mock_client
        )
        assert manager.client is None

    def test_state_transition_validation(self):
        """Test state transition validation for connection scenarios."""

        manager = BLEStateManager()

        # Valid transitions from DISCONNECTED
        assert manager.transition_to(ConnectionState.CONNECTING)
        assert manager.transition_to(
            ConnectionState.DISCONNECTING
        )  # Can close without connecting

        # Reset to DISCONNECTED
        manager.transition_to(ConnectionState.DISCONNECTED)

        # Invalid transitions from DISCONNECTED
        assert not manager.transition_to(
            ConnectionState.CONNECTED
        )  # Must go through CONNECTING first
        assert not manager.transition_to(
            ConnectionState.RECONNECTING
        )  # Must have been connected first
        # Note: ERROR state is actually allowed from DISCONNECTED in current implementation


class TestPhase3LockConsolidation:
    """Test Phase 3 lock consolidation and concurrent scenarios."""

    def test_unified_lock_thread_safety(self):
        """Test that unified state lock provides thread safety for concurrent operations."""
        manager = BLEStateManager()
        results = []
        errors = []

        def worker(worker_id):
            """
            Perform a sequence of state transitions against the shared BLEStateManager to exercise concurrent behavior.

            This worker runs a short loop that alternates requests to transition the manager through CONNECTING, CONNECTED (with a mock client), and DISCONNECTED states, recording each attempt's outcome by appending tuples to a shared `results` list and recording exceptions to a shared `errors` list. A small sleep between iterations encourages thread interleaving.

            Parameters
            ----------
                worker_id (int | str): Identifier used in recorded result entries to distinguish this worker's operations.

            """
            try:
                for i in range(10):
                    # Simulate state transitions
                    if i % 3 == 0:
                        success = manager.transition_to(ConnectionState.CONNECTING)
                    elif i % 3 == 1:
                        mock_client = MagicMock()
                        success = manager.transition_to(
                            ConnectionState.CONNECTED, client=mock_client
                        )
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
        from unittest.mock import MagicMock

        manager = BLEStateManager()
        mock_client = MagicMock()

        # Test nested lock acquisition through state manager methods
        def nested_operation():
            # This should work without deadlock due to reentrant lock
            """
            Exercise a nested sequence of state transitions under the manager's reentrant lock to verify no deadlock occurs and state/client consistency is preserved.

            Performs CONNECTING → CONNECTED (with a mock client), asserts the client is set and the manager reports connected.
            """
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
        """
        Ensure BLEStateManager's state lock handles concurrent contention without raising exceptions and leaves the manager in DISCONNECTED.

        Spawns multiple threads that each acquire the manager's state lock and perform state transitions; the test asserts no contention-related exceptions occurred and that the final manager.state equals ConnectionState.DISCONNECTED.
        """

        manager = BLEStateManager()
        contention_count = [0]

        def contending_worker():
            """
            Worker used in contention tests: acquires the manager's internal state lock, sleeps briefly to simulate work, then transitions the manager through CONNECTING to DISCONNECTED. If an exception occurs, increments the shared `contention_count[0]`.
            """
            try:
                # Try to acquire lock and perform operation
                with manager._state_lock:
                    # Simulate some work
                    time.sleep(0.01)
                    manager.transition_to(ConnectionState.CONNECTING)
                    manager.transition_to(ConnectionState.DISCONNECTED)
            except Exception:
                contention_count[0] += 1

        # Create multiple contending threads
        threads = []
        for _i in range(10):
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
        """Measure performance of state transitions under realistic load."""
        manager = BLEStateManager()
        mock_client = MagicMock()

        # Measure state transition performance
        iterations = 1000
        start_time = time.perf_counter()

        for _i in range(iterations):
            # Cycle through states
            manager.transition_to(ConnectionState.CONNECTING)
            manager.transition_to(ConnectionState.CONNECTED, client=mock_client)
            manager.transition_to(ConnectionState.DISCONNECTED)

        end_time = time.perf_counter()
        elapsed = end_time - start_time

        # Should complete quickly under typical CI conditions (allow headroom)
        assert (
            elapsed < 3.0
        ), f"State transitions too slow: {elapsed:.3f}s for {iterations * 3} transitions"

        # Calculate average transition time
        avg_time = elapsed / (iterations * 3)
        assert avg_time < 0.0005, f"Average transition time too high: {avg_time:.6f}s"

        print(
            f"Performance: {iterations * 3} transitions in {elapsed:.3f}s, avg: {avg_time:.6f}s"
        )

    def test_lock_contention_performance(self):
        """
        Measure BLEStateManager throughput and correctness under lock contention.

        Spawns 5 worker threads that each perform 100 iterations of CONNECTING → CONNECTED → DISCONNECTED transition attempts (3 operations per iteration) with a short delay to simulate work. Asserts the total elapsed time stays below 5.0 seconds and that at least 80% of the expected operations (5 * 100 * 3) succeeded. Prints a brief performance summary.
        """

        manager = BLEStateManager()
        results = []

        def worker(worker_id):
            """
            Perform a sequence of simulated BLE state transitions as a worker and record its operation count and elapsed time.

            Simulates 100 iterations of attempting transitions in order: CONNECTING, CONNECTED, DISCONNECTED. Counts each successful transition as one operation, measures elapsed wall-clock time for the loop, and appends a result dictionary to the external `results` list with keys "worker_id", "operations", and "time".

            Parameters
            ----------
                worker_id (int): Identifier included in the appended result to distinguish this worker's measurements.

            Side effects:
                Appends a dict to the outer-scope `results` list and calls `manager.transition_to(...)` using the outer-scope `manager`.

            """
            start_time = time.perf_counter()
            operations = 0

            for _i in range(100):
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
            results.append(
                {
                    "worker_id": worker_id,
                    "operations": operations,
                    "time": end_time - start_time,
                }
            )

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
        total_operations = sum(r["operations"] for r in results)
        expected_operations = 5 * 100 * 3  # 5 workers * 100 iterations * 3 operations
        assert (
            total_operations >= expected_operations * 0.8
        ), f"Too many failed operations: {total_operations}/{expected_operations}"

        print(
            f"Contention performance: {total_operations} operations in {total_time:.3f}s"
        )

def test_memory_efficiency(self):
        """Verify that BLEStateManager does not leak memory during creation and destruction."""
        # Force garbage collection
        gc.collect()
        initial_objects = len(gc.get_objects())

        # Create and destroy many state managers
        for _i in range(100):
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
        assert (
            object_growth < 1000
        ), f"Potential memory leak: {object_growth} objects created"

        print(
            f"Memory efficiency: {object_growth} objects created for 100 state managers"
        )

    def test_property_access_performance(self):
        """Test that state property access is fast."""

        manager = BLEStateManager()

        # Measure property access performance
        iterations = 10000
        start_time = time.perf_counter()

        for _i in range(iterations):
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
        assert avg_time < 0.00001, f"Property access too slow: {avg_time:.9f}s"

        print(
            f"Property access: {iterations * 5} accesses in {elapsed:.3f}s, avg: {avg_time:.9f}s"
        )
