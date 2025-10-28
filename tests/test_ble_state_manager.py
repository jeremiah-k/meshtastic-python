"""Tests for BLEStateManager state machine functionality."""

import gc
import threading
import time
from unittest.mock import MagicMock, Mock

import pytest  # pylint: disable=E0401


@pytest.fixture(autouse=True)  # pylint: disable=R0917
def _load_state_manager_after_mocks(
    mock_serial,  # pylint: disable=W0613
    mock_pubsub,  # pylint: disable=W0613
    mock_tabulate,  # pylint: disable=W0613
    mock_bleak,  # pylint: disable=W0613
    mock_bleak_exc,  # pylint: disable=W0613
    mock_publishing_thread,  # pylint: disable=W0613
):
    # Consume fixtures to enforce ordering and silence Ruff (ARG001)
    """
    Load BLEStateManager and ConnectionState into the module globals after mocks are applied.

    Dynamically import the meshtastic.ble_interface module and assign its BLEStateManager and ConnectionState attributes to the module-level globals so tests can reference them once mock fixtures have been applied.
    """
    _ = (
        mock_serial,
        mock_pubsub,
        mock_tabulate,
        mock_bleak,
        mock_bleak_exc,
        mock_publishing_thread,
    )
    """
    Ensure BLEStateManager and ConnectionState are loaded into module globals for tests.

    Dynamically imports the meshtastic.ble_interface module and assigns its BLEStateManager and ConnectionState attributes
    to the module-level globals BLEStateManager and ConnectionState so tests can reference them after mock fixtures are applied.
    """
    global BLEStateManager, ConnectionState  # pylint: disable=W0601
    import importlib

    ble_mod = importlib.import_module("meshtastic.ble_interface")
    BLEStateManager = ble_mod.BLEStateManager
    ConnectionState = ble_mod.ConnectionState


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
            Run 100 transition attempts from a worker thread and record outcomes.

            Each iteration attempts to transition the shared `manager` to ConnectionState.CONNECTING on even
            indices and ConnectionState.DISCONNECTED on odd indices. Each attempt appends a tuple
            (worker_id, iteration_index, success, current_state_value) to the shared `results` list.
            If an exception occurs, a tuple (worker_id, error_message) is appended to the shared `errors` list.

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
        assert isinstance(manager.state, ConnectionState)

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
            Obtain the manager's current connection state using a nested acquisition of its reentrant state lock.

            Returns:
                ConnectionState: The manager's current connection state.

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
        """
        Verify BLEStateManager maintains correct state and properties after an error and can recover to DISCONNECTED.

        After transitioning to `ERROR`, the manager's state is `ConnectionState.ERROR`, `is_connected` is `False`, `is_closing` is `True`, and `can_connect` is `False`. After transitioning to `DISCONNECTED`, the manager's state is `ConnectionState.DISCONNECTED` and `can_connect` is `True`.
        """
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
            Perform a sequence of state transitions on the shared BLEStateManager to exercise concurrent behavior.

            This worker runs several iterations, attempting CONNECTING, CONNECTED (with a mock client), and DISCONNECTED transitions, and records outcomes by appending tuples to the shared lists:
            - results: (worker_id, iteration, manager.state, success)
            - errors: (worker_id, error_message) for any exceptions raised during execution

            Parameters
            ----------
                worker_id (int | str): Identifier used in recorded result and error entries to distinguish this worker's operations.

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
        assert isinstance(manager.state, ConnectionState)

        # Verify client consistency
        if manager.state == ConnectionState.CONNECTED:
            assert manager.client is not None
        else:
            assert manager.client is None

    def test_reentrant_lock_behavior(self):
        """Test that unified lock supports reentrancy for nested operations."""
        from unittest.mock import MagicMock  # pylint: disable=W0404,W0621

        manager = BLEStateManager()
        mock_client = MagicMock()

        # Test nested lock acquisition through state manager methods
        def nested_operation():
            # This should work without deadlock due to reentrant lock
            """
            Exercise nested reentrant state-lock behavior by performing a sequence of state transitions.

            Acquires the manager's reentrant state lock and performs CONNECTING, CONNECTED (with mock_client), and DISCONNECTED transitions to verify nested lock acquisition does not deadlock.
            """
            with manager._state_lock:
                manager.transition_to(ConnectionState.CONNECTING)
                manager.transition_to(ConnectionState.CONNECTED, client=mock_client)
                manager.transition_to(ConnectionState.DISCONNECTED)

        # Call nested operation - should not deadlock
        nested_operation()

        # Verify final state
        assert manager.state == ConnectionState.DISCONNECTED

    @pytest.mark.slow
    def test_state_transition_performance(self):
        """Measure performance of state transitions under realistic load."""
        import os  # pylint: disable=C0415

        manager = BLEStateManager()
        mock_client = MagicMock()

        # Measure state transition performance
        iterations = 500 if os.getenv("CI") else 1000
        start_time = time.perf_counter()

        for _i in range(iterations):
            # Cycle through states
            manager.transition_to(ConnectionState.CONNECTING)
            manager.transition_to(ConnectionState.CONNECTED, client=mock_client)
            manager.transition_to(ConnectionState.DISCONNECTED)

        end_time = time.perf_counter()
        elapsed = end_time - start_time

        # Should complete quickly under typical CI conditions (allow headroom)
        assert elapsed < (
            4.5 if os.getenv("CI") else 3.0
        ), f"State transitions too slow: {elapsed:.3f}s for {iterations * 3} transitions"

        # Calculate average transition time
        avg_time = elapsed / (iterations * 3)
        assert avg_time < 0.0005, f"Average transition time too high: {avg_time:.6f}s"

        print(
            f"Performance: {iterations * 3} transitions in {elapsed:.3f}s, avg: {avg_time:.6f}s"
        )


@pytest.mark.slow
def test_lock_contention_performance():
    """
    Measure BLEStateManager throughput and correctness under lock contention.

    Starts five worker threads; each performs 100 cycles of CONNECTING → CONNECTED → DISCONNECTED on a shared BLEStateManager. Asserts the total elapsed time is below a CI-adjusted threshold and that at least 80% of the expected operations completed. Prints a short performance summary with total operations and elapsed time.
    """
    import os

    manager = BLEStateManager()
    results = []

    def worker(worker_id):
        """
        Execute 100 cycles of BLE state transitions and append this worker's metrics to the shared results list.

        Performs 100 iterations attempting transitions to ConnectionState.CONNECTING, ConnectionState.CONNECTED, and ConnectionState.DISCONNECTED on the outer-scope `manager`. Counts the number of successful transitions (incremented for each successful transition attempt) and measures the elapsed wall-clock time for the loop. Appends a dict to the outer-scope `results` list with keys "worker_id" (the provided identifier), "operations" (total successful transitions), and "time" (elapsed seconds).

        Parameters
        ----------
            worker_id (int): Identifier recorded in the appended result to distinguish this worker's measurements.

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
    assert total_time < (
        7.5 if os.getenv("CI") else 5.0
    ), f"Contention test too slow: {total_time:.3f}s"

    # Verify all operations completed
    total_operations = sum(r["operations"] for r in results)
    expected_operations = 5 * 100 * 3  # 5 workers * 100 iterations * 3 operations
    assert (
        total_operations >= expected_operations * 0.8
    ), f"Too many failed operations: {total_operations}/{expected_operations}"

    print(f"Contention performance: {total_operations} operations in {total_time:.3f}s")


def test_memory_efficiency():
    """
    Check that creating and destroying many BLEStateManager instances does not cause substantial memory growth.

    Creates 100 BLEStateManager instances, transitions each through CONNECTING → CONNECTED (with a mock client) → DISCONNECTED, forces garbage collection before and after, and asserts the net number of tracked Python objects increases by less than 1000. Prints the measured object growth for the run.
    """
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

    print(f"Memory efficiency: {object_growth} objects created for 100 state managers")


@pytest.mark.slow
def test_property_access_performance():
    """
    Measure average access time of key BLEStateManager properties and assert it meets a performance threshold.

    Performs repeated reads of the following properties: `state`, `is_connected`, `is_closing`, `can_connect`, and `client`. Uses 5,000 iterations when running in CI (environment variable `CI` set) and 10,000 iterations otherwise, computes the average time per property access, and raises an AssertionError if the average exceeds the configured threshold (a higher threshold is allowed in CI). Prints a concise performance summary with total accesses, elapsed time, and average time per access.
    """
    import os

    manager = BLEStateManager()

    # Measure property access performance
    iterations = 5000 if os.getenv("CI") else 10000
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
    assert avg_time < (
        0.00003 if os.getenv("CI") else 0.00002
    ), f"Property access too slow: {avg_time:.9f}s"

    print(
        f"Property access: {iterations * 5} accesses in {elapsed:.3f}s, avg: {avg_time:.9f}s"
    )
