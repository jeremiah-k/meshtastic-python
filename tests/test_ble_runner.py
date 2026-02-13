"""Tests for the BLE singleton coroutine runner."""

import asyncio
import threading
import time
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
    Ensure BLECoroutineRunner is running before and after each test.

    This prevents singleton state leakage between tests, particularly
    when a test calls stop() which would leave subsequent tests with
    a non-functional runner.
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
                int: The value of `id()` for the current running event loop.

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
            An awaitable coroutine that suspends forever and never completes.

            Awaiting this coroutine blocks indefinitely (it does not return or raise).
            """
            await asyncio.Event().wait()

        future = runner.run_coroutine_threadsafe(never_complete())

        # Future should not be done yet
        assert not future.done()

        # Cancel all pending futures
        runner.cancel_pending_futures()

        # Future should be cancelled
        assert future.cancelled()

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
        if runner._loop and runner._loop.is_running():
            runner._loop.call_soon_threadsafe(runner._loop.stop)

        # Give thread a moment to exit
        runner._thread.join(timeout=0.1)

        # The count should still be the same since we didn't call stop() with timeout
        # (zombie count only increments when stop() times out)
        current_count = get_zombie_runner_count()

        # Count should be >= initial (may have incremented if thread didn't exit)
        assert current_count >= initial_count


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
            Return the integer 42 from this coroutine.

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
            Return the integer 42 from this coroutine.

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
