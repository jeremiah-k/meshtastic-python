"""Singleton asyncio runner for BLE operations.

This module provides a singleton BLECoroutineRunner that manages a single, long-lived
background thread running an asyncio event loop for all BLE operations. This approach:

1. Reduces resource usage by avoiding per-client thread/loop creation
2. Prevents "zombie thread" accumulation
3. Simplifies cleanup and shutdown
4. Provides consistent event loop management across all BLE clients

Architecture:
    The singleton pattern ensures all BLEClient instances share the same event loop,
    reducing overhead from N threads to 1 thread regardless of how many clients exist.
"""

import asyncio
import atexit
import logging
import threading
import weakref
from concurrent.futures import Future
from typing import Coroutine, Optional, TypeVar

from bleak.exc import BleakDBusError

T = TypeVar("T")

logger = logging.getLogger("meshtastic.ble")

# Track zombie runners for diagnostics (shouldn't happen with singleton, but useful for monitoring)
_zombie_lock = threading.Lock()
_zombie_runner_count = 0
_ZOMBIE_WARN_THRESHOLD = 3


def get_zombie_runner_count() -> int:
    """Return the number of runner threads that failed to stop cleanly."""
    with _zombie_lock:
        return _zombie_runner_count


class BLECoroutineRunner:
    """
    Singleton runner for executing coroutines in a dedicated event loop thread.

    This avoids creating a new thread and event loop for every BLEClient instance,
    reducing resource usage and avoiding potential deadlocks between different
    event loops.

    Thread Safety:
        This class is fully thread-safe. The singleton pattern uses a lock to
        ensure only one instance exists, and all public methods are safe to call
        from any thread.

    Lifecycle:
        - The runner is created on first access (lazy initialization)
        - The background thread starts on first coroutine submission
        - The runner registers an atexit handler for graceful shutdown
        - If the loop crashes, it will be automatically restarted on next use

    Example:
        >>> runner = BLECoroutineRunner()  # Gets singleton instance
        >>> future = runner.run_coroutine_threadsafe(some_ble_operation())
        >>> result = future.result(timeout=10.0)
    """

    _instance: Optional["BLECoroutineRunner"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls) -> "BLECoroutineRunner":
        """Ensure only one instance of BLECoroutineRunner exists."""
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super(BLECoroutineRunner, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        """Initialize the singleton runner."""
        # Guard against re-initialization
        if getattr(self, "_initialized", False):
            return

        # Initialize instance lock before first use
        if not hasattr(self, "_internal_lock"):
            self._internal_lock = threading.Lock()

        with self._internal_lock:
            if self._initialized:
                return

            self._loop: Optional[asyncio.AbstractEventLoop] = None
            self._thread: Optional[threading.Thread] = None
            self._loop_ready = threading.Event()
            self._stop_requested = False
            self._initialized = True

            # Track pending futures for cleanup
            self._pending_futures: weakref.WeakSet[Future] = weakref.WeakSet()

            # Register atexit handler for graceful shutdown
            atexit.register(self._atexit_shutdown)

    @property
    def _instance_lock(self) -> threading.Lock:
        """Get the lock for this instance."""
        # Lock is always initialized in __init__, but keep property for access
        return self._internal_lock

    @property
    def is_running(self) -> bool:
        """Check if the runner's event loop is currently running."""
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._loop is not None
            and self._loop.is_running()
        )

    def _ensure_running(self) -> None:
        """Ensure the event loop thread is running."""
        with self._instance_lock:
            # Check if already running
            if (
                self._thread is not None
                and self._thread.is_alive()
                and self._loop is not None
            ):
                return

            # Reset stop flag for restart
            self._stop_requested = False
            self._loop_ready.clear()

            # Create and start the thread
            self._thread = threading.Thread(
                target=self._run_loop,
                name="BLECoroutineRunner",
                daemon=True,
            )
            self._thread.start()

            # Wait for loop to be ready
            if not self._loop_ready.wait(timeout=5.0):
                logger.error("BLECoroutineRunner loop failed to start within 5s")
                raise RuntimeError("BLE event loop failed to start")

    def _run_loop(self) -> None:
        """Run the asyncio event loop in the background thread."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.set_exception_handler(self._handle_loop_exception)

            # Signal that the loop is ready
            self._loop.call_soon(self._loop_ready.set)

            # Run forever until stopped
            self._loop.run_forever()

        except Exception as e:
            logger.error("Error in BLECoroutineRunner loop: %s", e, exc_info=True)
        finally:
            # Clean shutdown: cancel all pending tasks
            if self._loop:
                self._cancel_all_tasks()
                self._loop.close()
                self._loop = None
            self._loop_ready.clear()

    def _cancel_all_tasks(self) -> None:
        """Cancel all pending tasks on the event loop."""
        if not self._loop:
            return
        try:
            tasks = asyncio.all_tasks(self._loop)
            for task in tasks:
                task.cancel()
            # Give tasks a moment to handle cancellation
            if tasks:
                # Run one iteration to process cancellations
                self._loop.stop()
                self._loop.run_forever()
        except Exception:
            pass  # Best effort cleanup

    def run_coroutine_threadsafe(
        self, coro: Coroutine[None, None, T], timeout: Optional[float] = None
    ) -> Future[T]:
        """
        Submit a coroutine to be run in the singleton event loop thread.

        Parameters
        ----------
        coro : Coroutine[None, None, T]
            The coroutine to execute.
        timeout : Optional[float]
            Not used directly, but callers may use it with the returned Future.

        Returns
        -------
        Future[T]
            A future that will contain the result of the coroutine.

        Raises
        ------
        RuntimeError
            If the event loop cannot be started or is not available.
        """
        self._ensure_running()

        if self._loop is None:
            raise RuntimeError("BLECoroutineRunner loop is not available")

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        self._pending_futures.add(future)
        return future

    def _handle_loop_exception(
        self, loop: asyncio.AbstractEventLoop, context: dict
    ) -> None:
        """
        Handle asyncio event loop exceptions.

        Suppresses benign BleakDBusError exceptions that commonly occur during
        disconnect operations, and logs all other exceptions.
        """
        exception = context.get("exception")
        if exception and isinstance(exception, BleakDBusError):
            # Suppress DBus errors that happen as unretrieved task exceptions,
            # especially "Operation failed with ATT error: 0x0e" which happens on disconnect.
            logger.debug("Suppressing BleakDBusError in BLE event loop: %s", exception)
            return

        # Log the full context for other exceptions
        message = context.get("message", "Unknown error")
        logger.error("BLE event loop error: %s", message, exc_info=exception)

        # Use default handler for additional processing
        try:
            loop.default_exception_handler(context)
        except Exception:
            pass  # Don't let exception handler errors propagate

    def cancel_pending_futures(self) -> None:
        """Cancel all pending futures for cleanup."""
        for future in list(self._pending_futures):
            if not future.done():
                future.cancel()

    def stop(self, timeout: float = 2.0) -> bool:
        """
        Stop the event loop and join the background thread.

        Parameters
        ----------
        timeout : float
            Maximum seconds to wait for the thread to exit.

        Returns
        -------
        bool
            True if the thread exited cleanly, False if it timed out.
        """
        with self._instance_lock:
            self._stop_requested = True

            # Cancel pending futures first
            self.cancel_pending_futures()

            # Stop the loop
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)

            # Join the thread
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=timeout)

                if self._thread.is_alive():
                    global _zombie_runner_count
                    with _zombie_lock:
                        _zombie_runner_count += 1
                        current_count = _zombie_runner_count

                    logger.warning(
                        "BLECoroutineRunner thread did not exit within %.1fs (zombie count: %d)",
                        timeout,
                        current_count,
                    )
                    if current_count >= _ZOMBIE_WARN_THRESHOLD:
                        logger.warning(
                            "Multiple zombie BLE runner threads detected (%d). "
                            "Consider restarting the process to recover resources.",
                            current_count,
                        )
                    return False

            self._thread = None
            self._loop = None
            return True

    def _atexit_shutdown(self) -> None:
        """Shutdown handler called at process exit."""
        try:
            self.stop(timeout=1.0)
        except Exception:
            pass  # Best effort at exit

    def restart(self) -> bool:
        """
        Restart the runner if it's not running.

        This is useful if the event loop has crashed and needs to be restarted.

        Returns
        -------
        bool
            True if restart was initiated, False if already running.
        """
        with self._instance_lock:
            if self.is_running:
                return False

            # Force cleanup
            self._thread = None
            self._loop = None
            self._stop_requested = False

        # Start fresh
        self._ensure_running()
        return True
