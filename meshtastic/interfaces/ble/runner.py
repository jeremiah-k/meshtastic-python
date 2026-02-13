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
_LOOP_READY_TIMEOUT_SECONDS = 5.0


def get_zombie_runner_count() -> int:
    """
    Get the current count of runner threads that failed to stop cleanly.

    Returns
    -------
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
    -------
        >>> runner = BLECoroutineRunner()  # Gets singleton instance
        >>> future = runner.run_coroutine_threadsafe(some_ble_operation())
        >>> result = future.result(timeout=10.0)
    """

    _instance: Optional["BLECoroutineRunner"] = None
    _singleton_lock = threading.Lock()
    _initialized: bool
    _internal_lock: threading.RLock
    _loop: Optional[asyncio.AbstractEventLoop]
    _thread: Optional[threading.Thread]
    _loop_ready: threading.Event
    _stop_requested: bool
    _pending_futures: weakref.WeakSet[Future]

    def __new__(cls) -> "BLECoroutineRunner":
        """
        Create or return the singleton BLECoroutineRunner instance.

        On first instantiation, allocates the single instance and sets its `_initialized` flag to False.

        Returns
        -------
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
        # Guard creation of per-instance lock and initialization state with the
        # class-level singleton lock to avoid concurrent double-initialize races.
        with self._singleton_lock:
            if getattr(self, "_initialized", False):
                return
            # Use RLock to allow re-entrant locking in instance methods.
            if not hasattr(self, "_internal_lock"):
                self._internal_lock = threading.RLock()
            lock = self._internal_lock

        with lock:
            if self._initialized:
                return

            self._loop = None
            self._thread = None
            self._loop_ready = threading.Event()
            self._stop_requested = False
            self._initialized = True

            # Track pending futures for cleanup
            self._pending_futures = weakref.WeakSet()

            # Register atexit handler for graceful shutdown
            atexit.register(self._atexit_shutdown)

    @property
    def _instance_lock(self) -> "threading.RLock":
        """
        Return the reentrant lock used to synchronize this instance's internal state.

        Returns
        -------
            threading.RLock: The instance's reentrant lock for protecting internal attributes.
        """
        # Lock is always initialized in __init__, but keep property for access
        return self._internal_lock

    @property
    def is_running(self) -> bool:
        """
        Indicates whether the runner's background thread and event loop are active.

        Returns
        -------
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
            ready_event = self._start_locked()
        if ready_event is not None and not ready_event.wait(
            timeout=_LOOP_READY_TIMEOUT_SECONDS
        ):
            logger.error(
                "BLECoroutineRunner loop failed to start within %.1fs",
                _LOOP_READY_TIMEOUT_SECONDS,
            )
            raise RuntimeError("BLE event loop failed to start")

    def _start_locked(self) -> Optional[threading.Event]:
        """
        Start the background event loop thread if needed and return its readiness event.

        Must be called while holding the instance lock (`_instance_lock`).
        If the runner is already active, returns `None`.
        If startup is in progress on another thread, returns that startup's readiness event.
        If a new thread is started, returns a fresh readiness event for the caller to wait on.

        Returns
        ------
            Optional[threading.Event]: Event set when the runner loop is ready, or `None` if already running.
        """
        # Check if already running
        if (
            self._thread is not None
            and self._thread.is_alive()
            and self._loop is not None
            and self._loop.is_running()
        ):
            return None
        # If a thread exists but loop is not yet published, another caller is already
        # starting the runner. Reuse its readiness event.
        if self._thread is not None and self._thread.is_alive():
            return self._loop_ready

        # Reset stop flag for restart
        self._stop_requested = False
        # Use a fresh ready-event per start cycle to avoid cross-thread interference
        # if a stale runner thread exits while a new one is starting.
        self._loop_ready = threading.Event()
        ready_event = self._loop_ready

        # Create and start the thread
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(ready_event,),
            name="BLECoroutineRunner",
            daemon=True,
        )
        self._thread.start()
        return ready_event

    def _run_loop(self, ready_event: threading.Event) -> None:
        """Run the asyncio event loop in the background thread."""
        loop: Optional[asyncio.AbstractEventLoop] = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.set_exception_handler(self._handle_loop_exception)

            # Publish this loop only if this thread is still the active runner thread.
            with self._instance_lock:
                if self._thread is not threading.current_thread():
                    logger.debug(
                        "Discarding stale BLE runner thread during startup race."
                    )
                    return
                self._loop = loop

            # Signal that the loop is ready
            loop.call_soon(ready_event.set)

            # Run forever until stopped
            loop.run_forever()

        except Exception as e:
            logger.error("Error in BLECoroutineRunner loop: %s", e, exc_info=True)
        finally:
            # Clean shutdown: cancel all pending tasks
            if loop:
                self._cancel_all_tasks(loop)
                loop.close()

            ready_event.clear()
            with self._instance_lock:
                if self._thread is threading.current_thread():
                    if self._loop is loop:
                        self._loop = None
                    self._thread = None

    def _cancel_all_tasks(self, loop: asyncio.AbstractEventLoop) -> None:
        """
        Cancel all pending asyncio tasks on the runner's event loop.

        Cancels every task returned by asyncio.all_tasks() for the internal loop and runs the loop briefly to allow cancellation callbacks to execute.
        """
        try:
            tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(
                    asyncio.gather(*tasks, return_exceptions=True)
                )
        except Exception as e:
            logger.debug("Exception during task cancellation: %s", e)

    def run_coroutine_threadsafe(
        self,
        coro: Coroutine[None, None, T],
        timeout: Optional[float] = None,
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
        _ = timeout
        self._ensure_running()

        with self._instance_lock:
            loop = self._loop

        if loop is None or not loop.is_running():
            raise RuntimeError("BLECoroutineRunner loop is not available")

        future = asyncio.run_coroutine_threadsafe(coro, loop)
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

        Parameters
        ----------
            timeout (float): Maximum number of seconds to wait for the thread to join.

        Returns
        -------
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

            # Capture thread and loop references for join/cleanup outside lock
            thread = self._thread
            loop = self._loop

        # Stop the loop outside the lock
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                logger.debug(
                    "Unable to stop BLE event loop thread-safely; loop may already be closing."
                )

        # Avoid self-join deadlock if stop() is called from the runner thread.
        if thread is threading.current_thread():
            logger.debug("stop() called from runner thread; skipping join.")
        # Join OUTSIDE the lock to avoid deadlocking with runner-thread code
        # that might need _instance_lock (e.g., run_coroutine_threadsafe)
        elif thread and thread.is_alive():
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
            if self._thread is thread and (
                thread is None
                or thread is threading.current_thread()
                or not thread.is_alive()
            ):
                self._thread = None
            # Only clear _loop if it still references the original loop.
            # This avoids clearing a newer loop installed by a concurrent restart.
            if self._loop is loop:
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

        Returns
        -------
            bool: True if restart completed and loop readiness was confirmed, False if the runner was already running.
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
            ready_event = self._start_locked()
        if ready_event is not None and not ready_event.wait(
            timeout=_LOOP_READY_TIMEOUT_SECONDS
        ):
            logger.error(
                "BLECoroutineRunner restart timed out waiting for loop readiness after %.1fs",
                _LOOP_READY_TIMEOUT_SECONDS,
            )
            raise RuntimeError("BLE event loop failed to restart")
        return True
