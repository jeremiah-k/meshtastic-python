"""Tests for the BLE singleton coroutine runner."""

import asyncio
import threading
import time
import warnings
from concurrent.futures import Future

import pytest

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.runner import (
    BLECoroutineRunner,
    get_zombie_runner_count,
)


@pytest.fixture(autouse=True)
def ensure_runner_running():
    """
    Ensure the BLECoroutineRunner singleton is running for the duration of a test.
    
    Start the BLECoroutineRunner before the test and re-validate or restart it after the test to prevent singleton state leakage between tests. Intended for use as an autouse pytest fixture.
    """
    runner = BLECoroutineRunner()
    runner._ensure_running()
    yield
    # Ensure runner is still running after test for subsequent tests
    runner._ensure_running()


class TestBLECoroutineRunner:
    """Tests for the singleton BLECoroutineRunner."""

    def test_singleton_returns_same_instance(self):
        """Verify that BLECoroutineRunner is a true singleton."""
        runner1 = BLECoroutineRunner()
        runner2 = BLECoroutineRunner()

        assert runner1 is runner2

    def test_singleton_persistence(self):
        """Verify the singleton persists its thread across client instances."""
        runner = BLECoroutineRunner()

        # Create a client and ensure loop is running
        client1 = BLEClient()
        runner._ensure_running()
        thread1 = runner._thread

        assert thread1 is not None
        assert thread1.is_alive()
        assert thread1.name == "BLECoroutineRunner"

        # Close client and check if thread is still alive
        client1.close()
        assert thread1.is_alive()

        # Create another client and check if it uses the same thread
        client2 = BLEClient()
        assert runner._thread is thread1
        client2.close()
        assert thread1.is_alive()

    def test_multiple_clients_shared_loop(self):
        """Verify that multiple clients can run coroutines on the shared loop."""
        client1 = BLEClient()
        client2 = BLEClient()

        async def get_loop_id():
            """
            Get the identifier of the currently running asyncio event loop.
            
            Returns:
                int: The result of calling `id()` on the current running event loop.
            
            Raises:
                RuntimeError: If no event loop is running in the current context.
            """
            return id(asyncio.get_running_loop())

        loop_id1 = client1.async_await(get_loop_id())
        loop_id2 = client2.async_await(get_loop_id())

        assert loop_id1 == loop_id2

        client1.close()
        client2.close()

    def test_runner_restart_if_dead(self):
        """Verify that the runner restarts its thread if it somehow died."""
        runner = BLECoroutineRunner()
        runner._ensure_running()
        thread1 = runner._thread

        # Force stop the loop to kill the thread
        runner.stop()
        assert thread1.is_alive() is False

        # Next operation should restart it
        client = BLEClient()
        runner._ensure_running()
        thread2 = runner._thread

        assert thread2 is not None
        assert thread2 is not thread1
        assert thread2.is_alive()

        client.close()

    def test_is_running_property(self):
        """Verify the is_running property correctly reports state."""
        runner = BLECoroutineRunner()

        # Initially not running
        runner.stop()  # Ensure stopped
        # After stopping, may or may not be running depending on singleton state

        # After ensuring running
        runner._ensure_running()
        assert runner.is_running is True

        # After stopping
        runner.stop()
        assert runner.is_running is False

    def test_cancel_pending_futures(self):
        """Verify that pending futures are properly cancelled."""
        runner = BLECoroutineRunner()
        runner._ensure_running()

        # Create a future that won't complete
        async def never_complete():
            """
            A coroutine that suspends indefinitely.
            
            Awaiting this coroutine will block forever; it never completes and never raises.
            """
            await asyncio.Event().wait()

        future = runner.run_coroutine_threadsafe(never_complete())

        # Future should not be done yet
        assert not future.done()

        # Cancel all pending futures
        runner.cancel_pending_futures()

        # Future should be cancelled
        assert future.cancelled()

    def test_ensure_running_timeout_raises(self, monkeypatch):
        """_ensure_running should fail fast when loop readiness does not arrive."""
        runner = BLECoroutineRunner()
        never_ready = threading.Event()
        monkeypatch.setattr(runner, "_start_locked", lambda: never_ready)

        with pytest.raises(RuntimeError, match="failed to start"):
            runner._ensure_running(timeout=0.01)

    def test_run_coroutine_threadsafe_supports_startup_timeout_aliases(
        self, monkeypatch
    ):
        """Both startup_timeout and legacy timeout should drive runner startup wait."""
        runner = BLECoroutineRunner()
        observed_timeouts = []

        monkeypatch.setattr(
            runner,
            "_ensure_running",
            lambda timeout=None: observed_timeouts.append(timeout),
        )

        class _LoopStub:
            @staticmethod
            def is_running():
                """
                Report whether the BLE coroutine runner is currently active.
                
                Returns:
                    True if the runner is active, False otherwise.
                """
                return True

        with runner._instance_lock:
            original_loop = runner._loop
            runner._loop = _LoopStub()

        def _fake_submit(coro, _loop):
            """
            Close the provided coroutine without executing it and return a completed Future with result None.
            
            Parameters:
                coro (coroutine): Coroutine object to be closed and not scheduled for execution.
                _loop (asyncio.AbstractEventLoop): Ignored; present for signature compatibility.
            
            Returns:
                asyncio.Future: A Future already completed with result `None`.
            """
            coro.close()
            future = Future()
            future.set_result(None)
            return future

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _fake_submit)

        async def _noop():
            """
            A coroutine that does nothing.
            """
            return None

        try:
            runner.run_coroutine_threadsafe(_noop(), timeout=0.25)
            runner.run_coroutine_threadsafe(_noop(), startup_timeout=0.5)
            assert observed_timeouts == [0.25, 0.5]
        finally:
            with runner._instance_lock:
                runner._loop = original_loop

    def test_run_coroutine_threadsafe_timeout_alias_warns_deprecated(self, monkeypatch):
        """Legacy timeout alias should emit a deprecation warning."""
        runner = BLECoroutineRunner()
        monkeypatch.setattr(runner, "_ensure_running", lambda timeout=None: None)

        class _LoopStub:
            @staticmethod
            def is_running():
                """
                Report whether the BLE coroutine runner is currently active.
                
                Returns:
                    True if the runner is active, False otherwise.
                """
                return True

        with runner._instance_lock:
            original_loop = runner._loop
            runner._loop = _LoopStub()

        def _fake_submit(coro, _loop):
            """
            Close the provided coroutine without executing it and return a completed Future with result None.
            
            Parameters:
                coro (coroutine): Coroutine object to be closed and not scheduled for execution.
                _loop (asyncio.AbstractEventLoop): Ignored; present for signature compatibility.
            
            Returns:
                asyncio.Future: A Future already completed with result `None`.
            """
            coro.close()
            future = Future()
            future.set_result(None)
            return future

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _fake_submit)

        async def _noop():
            """
            A coroutine that does nothing.
            """
            return None

        try:
            with pytest.warns(DeprecationWarning, match="startup_timeout"):
                runner.run_coroutine_threadsafe(_noop(), timeout=0.25)
        finally:
            with runner._instance_lock:
                runner._loop = original_loop

    def test_run_coroutine_threadsafe_startup_timeout_has_no_deprecation_warning(
        self, monkeypatch
    ):
        """Explicit startup_timeout should not emit timeout-alias deprecation warnings."""
        runner = BLECoroutineRunner()
        monkeypatch.setattr(runner, "_ensure_running", lambda timeout=None: None)

        class _LoopStub:
            @staticmethod
            def is_running():
                """
                Report whether the BLE coroutine runner is currently active.
                
                Returns:
                    True if the runner is active, False otherwise.
                """
                return True

        with runner._instance_lock:
            original_loop = runner._loop
            runner._loop = _LoopStub()

        def _fake_submit(coro, _loop):
            """
            Close the provided coroutine without executing it and return a completed Future with result None.
            
            Parameters:
                coro (coroutine): Coroutine object to be closed and not scheduled for execution.
                _loop (asyncio.AbstractEventLoop): Ignored; present for signature compatibility.
            
            Returns:
                asyncio.Future: A Future already completed with result `None`.
            """
            coro.close()
            future = Future()
            future.set_result(None)
            return future

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _fake_submit)

        async def _noop():
            """
            A coroutine that does nothing.
            """
            return None

        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", DeprecationWarning)
                runner.run_coroutine_threadsafe(_noop(), startup_timeout=0.25)
            assert not any(issubclass(w.category, DeprecationWarning) for w in caught)
        finally:
            with runner._instance_lock:
                runner._loop = original_loop

    def test_run_coroutine_threadsafe_rejects_ambiguous_timeout_args(self):
        """Passing both timeout names should raise to avoid ambiguous behavior."""
        runner = BLECoroutineRunner()

        async def _noop():
            """
            A coroutine that does nothing.
            """
            return None

        coro = _noop()
        try:
            with pytest.raises(ValueError, match="timeout or startup_timeout"):
                runner.run_coroutine_threadsafe(coro, timeout=0.25, startup_timeout=0.5)
        finally:
            coro.close()

    def test_completed_futures_are_removed_from_tracking(self):
        """Completed futures should be removed from runner tracking promptly."""
        runner = BLECoroutineRunner()
        future = Future()
        with runner._instance_lock:
            runner._pending_futures.add(future)
        future.add_done_callback(runner._discard_tracked_future)
        future.set_result(7)

        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            with runner._instance_lock:
                if future not in runner._pending_futures:
                    break
            time.sleep(0.01)

        with runner._instance_lock:
            assert future not in runner._pending_futures

    def test_zombie_runner_count(self):
        """Verify zombie runner count is tracked."""
        # Initial count should be 0
        initial_count = get_zombie_runner_count()

        # Create runner and force a timeout scenario
        runner = BLECoroutineRunner()
        runner._ensure_running()

        # Force stop without waiting (simulates zombie)
        # Only call call_soon_threadsafe if the loop is a real asyncio event loop
        loop = runner._loop
        if loop and loop.is_running() and hasattr(loop, "call_soon_threadsafe"):
            try:
                loop.call_soon_threadsafe(loop.stop)
            except (RuntimeError, AttributeError):
                # Loop may already be stopping or closed
                pass

        # Give thread a moment to exit
        if runner._thread:
            runner._thread.join(timeout=0.1)

        # The count should still be the same since we didn't call stop() with timeout
        # (zombie count only increments when stop() times out)
        current_count = get_zombie_runner_count()

        # Count should be >= initial (may have incremented if thread didn't exit)
        assert current_count >= initial_count

    def test_zombie_runner_increments_when_stop_times_out(self):
        """stop() timeout path should increment zombie runner count."""

        class FakeLoop:
            def is_running(self) -> bool:
                """
                Report whether the runner's event loop thread is currently active.
                
                Returns:
                    bool: `True` if the runner is active and its thread is alive, `False` otherwise.
                """
                return True

            def stop(self) -> None:
                """
                Stop the background coroutine runner and clean up its resources.
                
                Stops the runner's event loop and associated thread (if active), unregisters the atexit handler when registered, and marks the runner as not running.
                """
                return None

            def call_soon_threadsafe(self, fn):
                """
                Execute the provided callable immediately and synchronously.
                
                Parameters:
                    fn (callable): Function or callable object to be invoked with no arguments.
                """
                fn()

        class FakeThread:
            def __init__(self):
                """
                Initialize the fake thread and prepare a list to record calls to `join`.
                
                Attributes:
                    join_calls (list): Appends each call's arguments when `join` is invoked (captures positional and keyword arguments).
                """
                self.join_calls = []

            def is_alive(self) -> bool:
                """
                Indicates whether the thread is currently alive.
                
                Returns:
                    bool: `True` if the thread is alive, `False` otherwise.
                """
                return True

            def join(self, timeout=None):
                """
                Record a join call for this fake thread by storing the provided timeout.
                
                Parameters:
                    timeout (float | None): The maximum number of seconds to wait for the thread to join, or None to wait indefinitely. The value is appended to self.join_calls.
                """
                self.join_calls.append(timeout)

        runner = BLECoroutineRunner()
        # Ensure we don't interfere with any real runner state.
        runner.stop()

        initial_count = get_zombie_runner_count()
        fake_thread = FakeThread()
        fake_loop = FakeLoop()
        with runner._instance_lock:
            runner._thread = fake_thread  # type: ignore[assignment]
            runner._loop = fake_loop  # type: ignore[assignment]
            runner._stop_requested = False

        try:
            assert runner.stop(timeout=0.0) is False
            assert get_zombie_runner_count() == initial_count + 1
            assert fake_thread.join_calls == [0.0]
        finally:
            # Restore singleton runner for subsequent tests.
            with runner._instance_lock:
                runner._thread = None
                runner._loop = None
                runner._stop_requested = False
            runner._ensure_running()

    def test_stop_unregisters_atexit_handler(self, monkeypatch):
        """Explicit stop should unregister the runner atexit callback."""
        runner = BLECoroutineRunner()
        runner._ensure_running()
        unregister_calls = []

        monkeypatch.setattr(
            "meshtastic.interfaces.ble.runner.atexit.unregister",
            lambda func: unregister_calls.append(func),
        )

        assert runner.stop() is True
        assert unregister_calls == [runner._atexit_handler]
        assert runner._atexit_registered is False

    def test_restart_reregisters_atexit_handler(self, monkeypatch):
        """Runner restart should re-register the atexit callback after explicit stop."""
        runner = BLECoroutineRunner()
        runner._ensure_running()
        register_calls = []
        unregister_calls = []

        monkeypatch.setattr(
            "meshtastic.interfaces.ble.runner.atexit.register",
            lambda func: register_calls.append(func),
        )
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.runner.atexit.unregister",
            lambda func: unregister_calls.append(func),
        )

        assert runner.stop() is True
        assert unregister_calls == [runner._atexit_handler]
        assert runner._atexit_registered is False

        runner._ensure_running()
        assert register_calls == [runner._atexit_handler]
        assert runner._atexit_registered is True


class TestBLEClientWithRunner:
    """Tests for BLEClient using the singleton runner."""

    def test_client_creates_runner(self):
        """Verify that creating a client initializes the runner."""
        client = BLEClient()

        assert hasattr(client, "_runner")
        assert isinstance(client._runner, BLECoroutineRunner)

        client.close()

    def test_client_close_is_idempotent(self):
        """Verify that close() can be called multiple times."""
        client = BLEClient()

        client.close()
        client.close()
        client.close()

        assert client._closed is True

    def test_async_await_raises_when_closed(self):
        """Verify that async_await raises when client is closed."""
        client = BLEClient()
        client.close()

        async def dummy():
            """
            Return the integer 42.
            
            Returns:
                int: The integer 42.
            """
            return 42

        coro = dummy()
        try:
            with pytest.raises(BLEClient.BLEError) as exc_info:
                client.async_await(coro)
        finally:
            coro.close()

        assert "closed" in str(exc_info.value).lower()

    def test_async_run_raises_when_closed(self):
        """Verify that async_run raises when client is closed."""
        client = BLEClient()
        client.close()

        async def dummy():
            """
            Return the integer 42.
            
            Returns:
                int: The integer 42.
            """
            return 42

        coro = dummy()
        try:
            with pytest.raises(BLEClient.BLEError) as exc_info:
                client.async_run(coro)
        finally:
            coro.close()

        assert "closed" in str(exc_info.value).lower()

    def test_get_zombie_thread_count_delegates_to_runner(self):
        """Verify get_zombie_thread_count uses the runner module."""
        from meshtastic.interfaces.ble.client import get_zombie_thread_count

        # Should return same as runner function
        runner_count = get_zombie_runner_count()
        client_count = get_zombie_thread_count()

        assert client_count == runner_count

    def test_client_close_disconnects_active_bleak_client(self):
        """close() should disconnect a connected underlying bleak client before closing."""

        class ConnectedBleakClient:
            def __init__(self):
                """
                Initialize the mock connected bleak client state.
                
                Sets `is_connected` to True and initializes `disconnect_calls` to 0.
                """
                self.is_connected = True
                self.disconnect_calls = 0

            async def disconnect(self):
                """
                Mark the client as disconnected and record the disconnect invocation.
                
                This method sets the client's connected state to False and increments an internal
                disconnect call counter used for testing or tracking.
                """
                self.disconnect_calls += 1
                self.is_connected = False

        client = BLEClient()
        bleak_client = ConnectedBleakClient()
        client.bleak_client = bleak_client

        client.close()

        assert bleak_client.disconnect_calls == 1
        assert client._closed is True

    def test_client_close_suppresses_disconnect_failures(self):
        """close() should remain best-effort when disconnect raises."""

        class FailingBleakClient:
            is_connected = True

            @staticmethod
            async def disconnect():
                """
                Always raise a RuntimeError with the message "boom".
                
                Raises:
                    RuntimeError: Always raised with the message "boom".
                """
                raise RuntimeError("boom")

        client = BLEClient()
        client.bleak_client = FailingBleakClient()

        client.close()

        assert client._closed is True