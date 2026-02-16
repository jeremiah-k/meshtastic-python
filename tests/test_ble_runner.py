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
    Ensure the BLECoroutineRunner is running for the duration of a test.

    Used as an autouse pytest fixture to start the singleton runner before the test and to verify/restart it after the test, preventing singleton state leakage between tests.
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
            Return the identifier of the currently running asyncio event loop.

            Returns:
                int: The value of id() for the current running event loop.

            Raises:
                RuntimeError: If there is no running event loop in the current context.

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
            Suspend forever; an awaitable that never completes.

            Awaiting this coroutine never returns or raises and will wait indefinitely.
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
                Check whether the BLE coroutine runner is currently active.

                Returns:
                    `true` if the runner is active, `false` otherwise.

                """
                return True

        with runner._instance_lock:
            original_loop = runner._loop
            runner._loop = _LoopStub()

        def _fake_submit(coro, _loop):
            """
            Close the provided coroutine without running it and return an already-resolved Future with value None.

            Parameters
            ----------
                coro (coroutine): Coroutine object that will be closed and not scheduled for execution.
                _loop (asyncio.AbstractEventLoop): Ignored loop parameter kept for signature compatibility.

            Returns
            -------
                future (asyncio.Future): A Future already completed with result `None`.

            """
            coro.close()
            future = Future()
            future.set_result(None)
            return future

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _fake_submit)

        async def _noop():
            """
            A coroutine that performs no operation.

            Returns:
                None

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
                Check whether the BLE coroutine runner is currently active.

                Returns:
                    `true` if the runner is active, `false` otherwise.

                """
                return True

        with runner._instance_lock:
            original_loop = runner._loop
            runner._loop = _LoopStub()

        def _fake_submit(coro, _loop):
            """
            Close the provided coroutine without running it and return an already-resolved Future with value None.

            Parameters
            ----------
                coro (coroutine): Coroutine object that will be closed and not scheduled for execution.
                _loop (asyncio.AbstractEventLoop): Ignored loop parameter kept for signature compatibility.

            Returns
            -------
                future (asyncio.Future): A Future already completed with result `None`.

            """
            coro.close()
            future = Future()
            future.set_result(None)
            return future

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _fake_submit)

        async def _noop():
            """
            A coroutine that performs no operation.

            Returns:
                None

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
                Check whether the BLE coroutine runner is currently active.

                Returns:
                    `true` if the runner is active, `false` otherwise.

                """
                return True

        with runner._instance_lock:
            original_loop = runner._loop
            runner._loop = _LoopStub()

        def _fake_submit(coro, _loop):
            """
            Close the provided coroutine without running it and return an already-resolved Future with value None.

            Parameters
            ----------
                coro (coroutine): Coroutine object that will be closed and not scheduled for execution.
                _loop (asyncio.AbstractEventLoop): Ignored loop parameter kept for signature compatibility.

            Returns
            -------
                future (asyncio.Future): A Future already completed with result `None`.

            """
            coro.close()
            future = Future()
            future.set_result(None)
            return future

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _fake_submit)

        async def _noop():
            """
            A coroutine that performs no operation.

            Returns:
                None

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
            A coroutine that performs no operation.

            Returns:
                None

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
            Return the constant 42.

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
            Return the constant 42.

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
                self.is_connected = True
                self.disconnect_calls = 0

            async def disconnect(self):
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
                raise RuntimeError("boom")

        client = BLEClient()
        client.bleak_client = FailingBleakClient()

        client.close()

        assert client._closed is True
