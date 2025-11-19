"Bluetooth interface, utility functions."

import asyncio
import importlib.metadata
import logging
import re
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Event, RLock, Thread, current_thread
from typing import Any, List, Optional, Tuple, cast, Callable, Dict

from bleak.exc import BleakDBusError, BleakError
from google.protobuf.message import DecodeError

from .exceptions import BLEError

logger = logging.getLogger(__name__)

EVENT_THREAD_JOIN_TIMEOUT = (
    2.0  # Matches receive thread join window for consistent teardown
)
ERROR_TIMEOUT = "{0} timed out after {1:.1f} seconds"

# Get bleak version using importlib.metadata (reliable method)
BLEAK_VERSION = importlib.metadata.version("bleak")


def _bleak_supports_connected_fallback(
    bleak_connected_device_fallback_min_version: Tuple[int, int, int],
) -> bool:
    """
    Determine whether the installed bleak version supports the connected-device fallback.
    
    Parameters:
        bleak_connected_device_fallback_min_version (Tuple[int, int, int]): Minimum required Bleak version as a (major, minor, patch) triplet.
    
    Returns:
        bool: `True` if the installed Bleak version is greater than or equal to the provided triplet, `False` otherwise.
    """
    return (
        _parse_version_triplet(BLEAK_VERSION)
        >= bleak_connected_device_fallback_min_version
    )


def _sleep(delay: float) -> None:
    """
    Pause execution for the given number of seconds.
    
    Parameters:
        delay (float): Number of seconds to sleep; may be fractional.
    """
    time.sleep(delay)


def _parse_version_triplet(version_str: str) -> Tuple[int, int, int]:
    """
    Return a normalized three-part integer version tuple extracted from the input string.
    
    Parses up to three numeric components from the provided version string, ignoring non-numeric segments; missing components are treated as zero. If numeric conversion fails, returns (0, 0, 0).
    
    Parameters:
        version_str (str): Version string to parse; may include non-numeric characters.
    
    Returns:
        Tuple[int, int, int]: (major, minor, patch) integers extracted from the string, with absent parts set to 0.
    """
    matches = re.findall(r"\d+", version_str or "")
    while len(matches) < 3:
        matches.append("0")
    try:
        return cast(
            Tuple[int, int, int],
            tuple(int(segment) for segment in matches[:3]),
        )
    except ValueError:
        return 0, 0, 0


async def _with_timeout(awaitable, timeout: Optional[float], label: str):
    """
    Waits for the given awaitable to complete, enforcing an optional timeout.
    
    Parameters:
        awaitable: An awaitable or coroutine to wait on.
        timeout (float | None): Timeout in seconds; if None, wait indefinitely.
        label (str): Label used in the timeout error message.
    
    Returns:
        The result produced by the awaitable.
    
    Raises:
        BLEError: If the awaitable does not complete before the specified timeout.
    """
    if timeout is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise BLEError(ERROR_TIMEOUT.format(label, timeout)) from exc


def _sanitize_address(address: Optional[str]) -> Optional[str]:
    """
    Normalize a BLE address by removing common separators and lowercasing the result.
    
    Parameters:
        address (Optional[str]): BLE address or identifier; may be None or only whitespace.
    
    Returns:
        Optional[str]: The address with "-", "_", ":", and spaces removed and converted to lowercase,
        or `None` if `address` is None or contains only whitespace.
    """
    if address is None or not address.strip():
        return None
    return (
        address.strip()
        .replace("-", "")
        .replace("_", "")
        .replace(":", "")
        .replace(" ", "")
        .lower()
    )


class ThreadCoordinator:
    """
    Simplified thread management for BLE operations.

    This class provides centralized thread and event management for BLE interface
    operations. It ensures proper cleanup, prevents resource leaks, and provides
    a consistent API for thread coordination patterns.

    Features:
        - Thread lifecycle management (create, start, join, cleanup)
        - Event coordination for thread synchronization
        - Automatic resource cleanup on shutdown
        - Thread-safe operations with RLock
        - Helper methods for common coordination patterns
    """

    def __init__(self):
        """
        Initialize a ThreadCoordinator for tracking threads and coordinating events.
        
        Creates an internal reentrant lock and initializes empty containers used by the coordinator:
        - _lock: reentrant lock protecting internal state.
        - _threads: list of tracked Thread objects.
        - _events: mapping from event name to threading.Event instances.
        """
        self._lock = RLock()
        self._threads: List[Thread] = []
        self._events: dict[str, Event] = {}

    def create_thread(
        self, target, name: str, *, daemon: bool = True, args=(), kwargs=None
    ) -> Thread:
        """
        Create and register a new Thread with the coordinator without starting it.
        
        Parameters:
            target: Callable to run in the thread.
            name (str): Thread name.
            daemon (bool): If True, mark the thread as a daemon.
            args (tuple): Positional arguments for `target`.
            kwargs (dict | None): Keyword arguments for `target`; may be None.
        
        Returns:
            Thread: The created Thread instance added to the coordinator's tracked threads (not started).
        """
        with self._lock:
            thread = Thread(
                target=target, name=name, daemon=daemon, args=args, kwargs=kwargs
            )
            self._threads.append(thread)
            return thread

    def create_event(self, name: str) -> Event:
        """
        Create and register a new Event with the specified name.

        Args:
        ----
            name (str): Key under which the event will be stored and retrievable.

        Returns:
        -------
            Event: The newly created Event instance.

        """
        with self._lock:
            event = Event()
            self._events[name] = event
            return event

    def get_event(self, name: str) -> Optional[Event]:
        """
        Get a tracked Event by name.
        
        Parameters:
            name (str): The name identifier of the tracked event.
        
        Returns:
            Optional[Event]: The Event if found, `None` otherwise.
        """
        with self._lock:
            return self._events.get(name)

    def start_thread(self, thread: Thread):
        """
        Start a tracked thread managed by this coordinator.
        
        If the provided thread is among the coordinator's tracked threads, start it; otherwise the call has no effect.
        
        Parameters:
            thread (threading.Thread): The thread to start if it is tracked by the coordinator.
        """
        with self._lock:
            if thread in self._threads:
                thread.start()

    def join_thread(self, thread: Thread, timeout: Optional[float] = None):
        """
        Join a tracked thread if it is alive, tracked, and not the current thread.
        
        If the provided thread is currently being tracked and is alive (and is not the calling thread), wait up to `timeout` seconds for it to finish.
        
        Parameters:
        	thread (Thread): The thread to join.
        	timeout (float | None): Maximum seconds to wait for the thread to finish; `None` to wait indefinitely.
        """
        with self._lock:
            if (
                thread in self._threads
                and thread.is_alive()
                and thread is not current_thread()
            ):
                thread.join(timeout=timeout)

    def join_all(self, timeout: Optional[float] = None):
        """
        Wait for all tracked threads to finish, joining each with an optional per-thread timeout.
        
        Parameters:
            timeout (Optional[float]): Maximum seconds to wait for each thread to join; if `None`, wait indefinitely for each tracked thread. The current thread (caller) is never joined.
        """
        with self._lock:
            current = current_thread()
            for thread in self._threads:
                if thread.is_alive() and thread is not current:
                    thread.join(timeout=timeout)

    def set_event(self, name: str):
        """
        Set the tracked event with the given name.
        
        If no event is tracked under that name, this is a no-op.
        """
        with self._lock:
            if name in self._events:
                self._events[name].set()

    def clear_event(self, name: str):
        """
        Clear the tracked event with the specified name.

        If an event with `name` is being tracked, clear its internal flag; otherwise do nothing.

        Args:
        ----
            name (str): The identifier of the tracked event to clear.

        """
        with self._lock:
            if name in self._events:
                self._events[name].clear()

    def wait_for_event(self, name: str, timeout: Optional[float] = None) -> bool:
        """
        Block until the named tracked event is set or the optional timeout elapses.
        
        Parameters:
            name (str): Name of the tracked event.
            timeout (Optional[float]): Maximum seconds to wait; None to wait indefinitely.
        
        Returns:
            `true` if the event was set before the timeout, `false` otherwise (including when the event is not tracked).
        """
        event = self.get_event(name)
        if event:
            return event.wait(timeout=timeout)
        return False

    def check_and_clear_event(self, name: str) -> bool:
        """
        Check whether the named tracked event is set and clear it if set.
        
        Returns:
            `True` if the event was set (and was cleared), `False` otherwise.
        """
        event = self.get_event(name)
        if event and event.is_set():
            event.clear()
            return True
        return False

    def wake_waiting_threads(self, *event_names: str):
        """
        Wake threads waiting on one or more named events.
        
        Parameters:
            event_names (str): One or more event names previously created with `create_event`. Each specified event will be set if it is tracked, which will wake any threads waiting on it.
        """
        for name in event_names:
            self.set_event(name)

    def clear_events(self, *event_names: str):
        """
        Clear multiple tracked events.
        
        Parameters:
            event_names (str): One or more event names to clear; names not tracked are ignored.
        """
        for name in event_names:
            self.clear_event(name)

    def cleanup(self):
        """
        Signal all tracked events, join and stop tracked threads, and clear coordinator state.

        Sets every tracked Event, joins each tracked Thread (except current) with a short timeout,
        then clears internal thread and event registries so the coordinator no longer tracks them.
        """
        with self._lock:
            # Signal all events
            for event in self._events.values():
                event.set()

            # Join threads with timeout (except current thread)
            current = current_thread()
            for thread in self._threads:
                if thread.is_alive() and thread is not current:
                    thread.join(timeout=EVENT_THREAD_JOIN_TIMEOUT)

            # Clear tracking
            self._threads.clear()
            self._events.clear()


class BLEErrorHandler:
    """
    Helper class for consistent error handling in BLE operations.

    This class provides static methods for standardized error handling patterns
    throughout the BLE interface. It centralizes error logging and recovery strategies.

    Features:
        - Safe execution with fallback return values
        - Consistent error logging and classification
        - Cleanup operations that never raise exceptions
    """

    @staticmethod
    def safe_execute(
        func,
        default_return=None,
        log_error: bool = True,
        error_msg: str = "Error in operation",
        reraise: bool = False,
    ):
        """
        Execute a zero-argument callable and return its result, substituting a default value if an exception is handled.
        
        Parameters:
            func (callable): Zero-argument callable to execute.
            default_return: Value to return when execution fails; defaults to None.
            log_error (bool): If True, log caught exceptions; defaults to True.
            error_msg (str): Message used when logging errors; defaults to "Error in operation".
            reraise (bool): If True, re-raise any caught exception instead of returning `default_return`.
        
        Returns:
            The value returned by `func()` on success, or `default_return` if an exception is caught.
        
        Raises:
            Exception: Re-raises the original exception if `reraise` is True.
        
        Notes:
            The function treats BleakError, BleakDBusError, DecodeError, FutureTimeoutError, and any other Exception as handled for the purpose of returning `default_return` (unless `reraise` is set).
        """
        try:
            return func()
        except (BleakError, BleakDBusError, DecodeError, FutureTimeoutError) as e:
            if log_error:
                logger.debug("%s: %s", error_msg, e)
            if reraise:
                raise
            return default_return
        except Exception:  # noqa: BLE001
            if log_error:
                logger.exception("%s", error_msg)
            if reraise:
                raise
            return default_return

    @staticmethod
    def safe_cleanup(func, cleanup_name: str = "cleanup operation"):
        """
        Execute a cleanup callable and suppress any exceptions, logging failures at debug level.
        
        Parameters:
            func (callable): Zero-argument cleanup function to execute.
            cleanup_name (str): Human-readable name for the cleanup used in debug logging.
        """
        try:
            func()
        except Exception as e:  # noqa: BLE001
            logger.debug("Error during %s: %s", cleanup_name, e)