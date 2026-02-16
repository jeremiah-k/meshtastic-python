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
import warnings
import weakref
from concurrent.futures import Future
from typing import Coroutine, Optional, TypeVar

from bleak.exc import BleakDBusError

from meshtastic.interfaces.ble.constants import BLEConfig

T = TypeVar("T")

logger = logging.getLogger("meshtastic.ble")

# Track zombie runners for diagnostics (shouldn't happen with singleton, but useful for monitoring)
_zombie_lock = threading.Lock()
_zombie_runner_count = 0


def get_zombie_runner_count() -> int:
    """
    Return the number of runner threads that failed to stop cleanly.
    
    Returns:
        int: Count of zombie runner threads.
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
        Initialize per-instance state for the singleton BLECoroutineRunner.
        
        Sets up the per-instance reentrant lock, lifecycle flags and placeholders for the background
        thread and asyncio loop, initializes a WeakSet to track pending futures, and registers
        an atexit handler to ensure the runner is shut down when the process exits.
        """
        # The singleton lock is uncontended after first initialization, so always
        # acquiring it has negligible performance impact while ensuring correctness.
        with self._singleton_lock:
            # Fast path: already initialized - check inside lock for thread safety
            if getattr(self, "_initialized", False):
                return

            # Use RLock to allow re-entrant locking in instance methods.
            # Create lock inside singleton lock to ensure only one is ever created.
            if not hasattr(self, "_internal_lock"):
                self._internal_lock = threading.RLock()

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
        Return whether the runner's background thread and asyncio event loop are active.
        
        @returns
            `true` if the background thread exists and is alive and the event loop exists and is running, `false` otherwise.
        """
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._loop is not None
            and self._loop.is_running()
        )

    def _ensure_running(self, timeout: Optional[float] = None) -> None:
        """
        Ensure the runner's background asyncio event loop is started and ready.
        
        Parameters:
            timeout (Optional[float]): Maximum seconds to wait for the loop to become ready. If None, uses BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS.
        
        Raises:
            RuntimeError: If the event loop fails to start within the given timeout.
        """
        if timeout is None:
            timeout = BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS
        with self._instance_lock:
            ready_event = self._start_locked()
        if ready_event is not None and not ready_event.wait(timeout=timeout):
            logger.error(
                "BLECoroutineRunner loop failed to start within %.1fs",
                timeout,
            )
            raise RuntimeError("BLE event loop failed to start")

    def _start_locked(self) -> Optional[threading.Event]:
        """
        Ensure the background event loop thread is started and return an event that will be set when the loop is ready.
        
        Must be called while holding the instance lock (`_instance_lock`). If the runner is already running, returns `None`. If a startup is already in progress on another thread, returns that startup's readiness event. If this call starts a new thread, returns a fresh `threading.Event` that will be set when the loop becomes ready.
        Returns:
            threading.Event: Event set when the runner's loop is ready, or `None` if the runner is already running.
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
        """
        Start and run the asyncio event loop on the background runner thread and manage its lifecycle.
        
        Creates and installs a new event loop on the current thread, publishes that loop to the runner if this thread remains the active runner and stop has not been requested, signals readiness via `ready_event`, runs the loop until stopped, and on exit cancels remaining tasks and closes the loop.
        
        Parameters:
            ready_event (threading.Event): Event that will be set when the loop is ready and cleared when the thread is shutting down.
        """
        loop: Optional[asyncio.AbstractEventLoop] = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.set_exception_handler(self._handle_loop_exception)

            # Publish this loop only if this thread is still the active runner thread
            # and stop hasn't been requested.
            with self._instance_lock:
                if (
                    self._thread is not threading.current_thread()
                    or self._stop_requested
                ):
                    logger.debug(
                        "Discarding stale BLE runner thread during startup race or stop requested."
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
        Cancel all non-completed tasks associated with the given event loop and wait for their cancellation to finish.
        
        Parameters:
        	loop (asyncio.AbstractEventLoop): The event loop whose pending tasks should be cancelled.
        
        Notes:
        	This method suppresses and logs any exceptions raised while cancelling or awaiting the tasks.
        """
        try:
            tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        except Exception as e:
            logger.debug("Exception during task cancellation: %s", e)

    def run_coroutine_threadsafe(
        self,
        coro: Coroutine[None, None, T],
        timeout: Optional[float] = None,
        *,
        startup_timeout: Optional[float] = None,
    ) -> Future[T]:
        """
        Submit a coroutine to be run in the singleton event loop thread.

        Parameters
        ----------
        coro : Coroutine[None, None, T]
            The coroutine to execute.
        timeout : Optional[float]
            Deprecated alias for `startup_timeout`.
        startup_timeout : Optional[float]
            Optional startup timeout in seconds used while ensuring the runner
            loop is ready. This does not apply to coroutine completion waits;
            use `Future.result(timeout=...)` on the returned Future for that.

        Returns
        -------
        Future[T]
            A future that will contain the result of the coroutine.

        Raises
        ------
        RuntimeError
            If the event loop cannot be started or is not available.
        ValueError
            If both `timeout` and `startup_timeout` are provided.

        """
        if timeout is not None and startup_timeout is not None:
            raise ValueError("Specify only one of timeout or startup_timeout")
        if timeout is not None and startup_timeout is None:
            warnings.warn(
                "run_coroutine_threadsafe(timeout=...) is deprecated; "
                "use startup_timeout=<seconds> instead.",
                DeprecationWarning,
                stacklevel=2,
            )

        effective_startup_timeout = (
            startup_timeout if startup_timeout is not None else timeout
        )
        if effective_startup_timeout is None:
            effective_startup_timeout = BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS

        self._ensure_running(timeout=effective_startup_timeout)

        with self._instance_lock:
            loop = self._loop

        if loop is None or not loop.is_running():
            raise RuntimeError("BLECoroutineRunner loop is not available")

        future = asyncio.run_coroutine_threadsafe(coro, loop)
        # Protect concurrent access to _pending_futures WeakSet
        with self._instance_lock:
            self._pending_futures.add(future)
        # Remove completed futures promptly instead of waiting for GC.
        future.add_done_callback(self._discard_tracked_future)
        return future

    def _discard_tracked_future(self, future: Future) -> None:
        """
        Remove a completed Future from the runner's tracked pending futures.
        
        Parameters:
            future (Future): The completed future to remove from `_pending_futures`.
        """
        with self._instance_lock:
            self._pending_futures.discard(future)

    def _handle_loop_exception(
        self, loop: asyncio.AbstractEventLoop, context: dict
    ) -> None:
        """
        Handle exceptions raised in the runner's asyncio event loop.
        
        Suppresses BleakDBusError exceptions (logged at debug) that commonly occur during disconnects, logs other exceptions with their context at error level, and delegates to the loop's default exception handler. If the default handler raises, that error is logged at debug level.
        
        Parameters:
            loop (asyncio.AbstractEventLoop): The event loop where the exception occurred.
            context (dict): The context mapping provided by asyncio containing exception and message information.
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
        Cancel all tracked futures that have not completed.
        
        Attempts to cancel each future currently tracked by the runner; futures that are already done are not affected. Exceptions raised while cancelling individual futures are handled internally and do not propagate.
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
        
        This requests the runner to stop, cancels any tracked pending futures, and attempts to stop the background asyncio loop. If called from the runner thread, joining the thread is skipped to avoid deadlock. The method performs thread-safe cleanup and avoids clearing loop/thread references if a concurrent restart replaced them.
        
        Parameters:
            timeout (float): Maximum number of seconds to wait for the background thread to join.
        
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
                    if current_count >= BLEConfig.RUNNER_ZOMBIE_WARN_THRESHOLD:
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
        
        Returns:
            bool: `True` if restart completed and loop readiness was confirmed, `False` if the runner was already running.
        
        Raises:
            RuntimeError: If the event loop fails to become ready within the configured timeout.
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
            timeout=BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS
        ):
            logger.error(
                "BLECoroutineRunner restart timed out waiting for loop readiness after %.1fs",
                BLEConfig.RUNNER_LOOP_READY_TIMEOUT_SECONDS,
            )
            raise RuntimeError("BLE event loop failed to restart")
        return True