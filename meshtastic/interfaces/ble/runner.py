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
    """
    Get the current count of runner threads that failed to stop cleanly.

    Returns:
        int: The number of zombie runner threads.
    """
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
        """
        Create or return the singleton BLECoroutineRunner instance.

        On first instantiation, allocates the single instance and sets its `_initialized` flag to False.

        Returns:
            BLECoroutineRunner: The singleton BLECoroutineRunner instance.
        """
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super(BLECoroutineRunner, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        """
        Initialize the singleton BLECoroutineRunner instance and its runtime state.

        Sets up per-instance synchronization, lifecycle flags, background thread/loop placeholders, a weak set to track pending futures, and registers an atexit handler to ensure graceful shutdown.
        """
        # Guard against re-initialization
        if getattr(self, "_initialized", False):
            return

        # Initialize instance lock before first use
        # Use RLock to allow re-entrant locking (e.g., stop() calling cancel_pending_futures())
        if not hasattr(self, "_internal_lock"):
            self._internal_lock = threading.RLock()

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
    def _instance_lock(self) -> "threading.RLock":
        """
        Return the reentrant lock used to synchronize this instance's internal state.

        Returns:
            threading.RLock: The instance's reentrant lock for protecting internal attributes.
        """
        # Lock is always initialized in __init__, but keep property for access
        return self._internal_lock

    @property
    def is_running(self) -> bool:
        """
        Indicates whether the runner's background thread and event loop are active.

        Returns:
            `true` if the background thread exists and is alive and the event loop exists and is running, `false` otherwise.
        """
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._loop is not None
            and self._loop.is_running()
        )

    def _ensure_running(self) -> None:
        """Ensure the event loop thread is running."""
        with self._instance_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        """
        Start the background event loop thread and wait for it to become ready.

        Must be called while holding the instance lock (_instance_lock). If the runner is already
        running this is a no-op. Raises RuntimeError if the loop does not signal readiness
        within 5 seconds.

        Raises:
            RuntimeError: If the event loop fails to start within 5 seconds.
        """
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
        """
        Cancel all pending asyncio tasks on the runner's event loop.

        Cancels every task returned by asyncio.all_tasks() for the internal loop and runs the loop briefly to allow cancellation callbacks to execute.
        """
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
        except Exception as e:
            logger.debug("Exception during task cancellation: %s", e)

    def run_coroutine_threadsafe(
        self,
        coro: Coroutine[None, None, T],
        _timeout: Optional[float] = None,  # noqa: ARG002
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
        # Protect concurrent access to _pending_futures WeakSet
        with self._instance_lock:
            self._pending_futures.add(future)
        return future

    def _handle_loop_exception(
        self, loop: asyncio.AbstractEventLoop, context: dict
    ) -> None:
        """
        Handle exceptions raised in the runner's asyncio event loop.

        Suppresses `BleakDBusError` exceptions (logged at debug) that commonly occur during disconnects, logs other exceptions with their context at error level, and then delegates to `loop.default_exception_handler`. Any exception raised by the default handler is logged at debug level.
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
        except Exception as e:
            logger.debug("Exception in default exception handler: %s", e)

    def cancel_pending_futures(self) -> None:
        """
        Cancel all tracked pending futures that have not completed.

        This method iterates the runner's tracked futures and cancels each one that is not done. Exceptions raised while attempting to cancel an individual future are caught and logged at the debug level.
        """
        with self._instance_lock:
            for future in list(self._pending_futures):
                if not future.done():
                    try:
                        future.cancel()
                    except Exception as e:
                        logger.debug("Exception cancelling future: %s", e)

    def stop(self, timeout: float = 2.0) -> bool:
        """
        Stop the runner's event loop and wait for the background thread to exit.

        Parameters:
            timeout (float): Maximum number of seconds to wait for the thread to join.

        Returns:
            bool: `True` if the background thread exited within `timeout`, `False` if it did not (thread considered a zombie).
        """
        # Capture state and schedule stop under lock, but join OUTSIDE the lock
        # to avoid deadlock if the runner thread needs _instance_lock
        with self._instance_lock:
            self._stop_requested = True

            # Cancel pending futures inline (already holding lock, RLock allows this)
            for future in list(self._pending_futures):
                if not future.done():
                    try:
                        future.cancel()
                    except Exception as e:
                        logger.debug("Exception cancelling future: %s", e)

            # Stop the loop
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)

            # Capture thread and loop references for join/cleanup outside lock
            thread = self._thread
            loop = self._loop

        # Join OUTSIDE the lock to avoid deadlocking with runner-thread code
        # that might need _instance_lock (e.g., run_coroutine_threadsafe)
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

            # Re-acquire lock to check result and update state
            with self._instance_lock:
                if thread.is_alive():
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

        # Final cleanup under lock - only clear if no concurrent _ensure_running replaced them
        with self._instance_lock:
            # Only clear _thread if it still references the original thread we stopped
            if self._thread is thread:
                self._thread = None
            # Only clear _loop if it still references the original loop (or is not running)
            if self._loop is loop or (
                self._loop is not None and not self._loop.is_running()
            ):
                self._loop = None
        return True

    def _atexit_shutdown(self) -> None:
        """
        Attempt to stop the runner during process exit, suppressing and logging any exceptions.

        Calls stop(timeout=1.0) to perform shutdown; any exception raised during this call is caught and logged at debug level.
        """
        try:
            self.stop(timeout=1.0)
        except Exception as e:
            logger.debug("Exception during atexit shutdown: %s", e)

    def restart(self) -> bool:
        """
        Restart the singleton runner if it is not currently running.

        Returns:
            bool: True if restart was initiated, False if the runner was already running.
        """
        with self._instance_lock:
            if self.is_running:
                return False

            # Force cleanup
            self._thread = None
            self._loop = None
            self._stop_requested = False

            # Start fresh - call _start_locked() while still holding the lock
            # to avoid race with concurrent stop()/restart() calls
            self._start_locked()
        return True
