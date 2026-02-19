"""Thread coordination utilities for BLE operations."""

import logging
import warnings
from threading import Event, RLock, Thread, current_thread
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from meshtastic.interfaces.ble.constants import EVENT_THREAD_JOIN_TIMEOUT

logger = logging.getLogger("meshtastic.ble")

# Type alias for objects that behave like Thread (Thread or _InertThread)
ThreadLike = Union["Thread", "_InertThread"]

_INERT_THREAD_START_ERROR = (
    "Cannot start inert thread '{name}': coordinator has been cleaned up"
)


class _InertThread:
    """
    A thread-like object that cannot be started.

    Returned by create_thread after cleanup to maintain API contract while
    preventing orphaned thread creation.
    """

    def __init__(self, name: str) -> None:
        """
        Create an inert thread placeholder returned after cleanup to preserve the thread-like API while preventing new threads from starting.

        Parameters
        ----------
            name (str): Human-readable name for the inert thread; stored on the object as the `name` attribute.

        Attributes
        ----------
            name (str): The provided thread name.
            daemon (bool): Always True to match typical thread daemon status.
            ident (None): Always None to indicate the inert thread has no identity and cannot be started.

        """
        self.name = name
        self.daemon = True
        self.ident = None

    def start(self) -> None:
        """
        Prevent starting an inert thread returned after coordinator cleanup.

        Raises:
            RuntimeError: always raised to indicate this inert thread cannot be started.

        """
        raise RuntimeError(_INERT_THREAD_START_ERROR.format(name=self.name))

    def is_alive(self) -> bool:
        """
        Indicates that this inert thread is not alive.

        Returns:
            bool: False always; inert threads cannot be alive.

        """
        return False

    def join(self, timeout: Optional[float] = None) -> None:
        """
        No-op join for an inert thread; joining has no effect.

        Parameters
        ----------
            timeout (Optional[float]): Ignored. Included for API compatibility.

        """
        _ = timeout


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

    def __init__(self) -> None:
        """
        Create a ThreadCoordinator that centralizes thread and event management.

        Initializes internal coordinator state:
        - a reentrant lock protecting coordinator state
        - an empty list of tracked threads
        - an empty set of threads pending start
        - an empty mapping of event names to Event objects
        - a boolean flag indicating cleanup has not been performed
        """
        self._lock = RLock()
        self._threads: List[Thread] = []
        self._pending_start: Set[ThreadLike] = set()
        self._events: Dict[str, Event] = {}
        self._cleaned_up: bool = False

    def create_thread(
        self,
        target: Callable[..., Any],
        name: str,
        *,
        daemon: bool = True,
        args: Tuple[Any, ...] = (),
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> ThreadLike:
        """
        Create and register a Thread configured with the given target and arguments without starting it.

        If the coordinator has already been cleaned up, returns an inert thread that cannot be started. The returned thread is registered for lifecycle management but is not started by this call.

        Parameters
        ----------
            target (Callable): Callable to be executed by the thread.
            name (str): Name assigned to the thread.
            daemon (bool): Whether the thread should run as a daemon.
            args (tuple): Positional arguments to pass to the target.
            kwargs (Optional[dict]): Keyword arguments to pass to the target.

        Returns
        -------
            Thread: The created Thread instance (registered with the coordinator, not started). After cleanup, an inert thread is returned whose start() raises RuntimeError.

        """
        with self._lock:
            # Prevent thread creation after cleanup to avoid orphaned threads
            if self._cleaned_up:
                logger.warning(
                    "Cannot create thread '%s': coordinator has been cleaned up", name
                )
                # Return an inert thread that cannot be started
                return _InertThread(name)
            # Prune dead threads to prevent unbounded growth in long-running processes
            self._threads = [t for t in self._threads if t.is_alive()]
            thread = Thread(
                target=target, name=name, daemon=daemon, args=args, kwargs=kwargs
            )
            self._threads.append(thread)
            return thread

    def createThread(
        self,
        target: Callable[..., Any],
        name: str,
        *,
        daemon: bool = True,
        args: Tuple[Any, ...] = (),
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> ThreadLike:
        """
        Compatibility wrapper for camelCase callers that issues a DeprecationWarning.

        This method emits a DeprecationWarning and returns a Thread-like object configured
        with the provided target, name, and options.

        Returns:
            ThreadLike: A Thread-like object configured with the provided target and
            name; may be an inert placeholder if the coordinator has already been
            cleaned up.

        """
        warnings.warn(
            "createThread is deprecated; use create_thread instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.create_thread(
            target,
            name,
            daemon=daemon,
            args=args,
            kwargs=kwargs,
        )

    def create_event(self, name: str) -> Event:
        """
        Register a new Event under the given name and return it.

        If an Event with the same name already exists, the existing Event is returned and no replacement occurs. This operation is thread-safe via the coordinator's lock.

        Parameters
        ----------
            name (str): Name to register the Event under.

        Returns
        -------
            Event: The Event instance registered under `name`, or the existing instance if one was already registered.

        """
        with self._lock:
            if name in self._events:
                logger.warning(
                    "Event already exists: %s, returning existing instance", name
                )
                return self._events[name]
            event = Event()
            self._events[name] = event
            return event

    def createEvent(self, name: str) -> Event:
        """
        Deprecated camelCase compatibility wrapper that creates and returns an Event registered under the given name. Issues a DeprecationWarning when called.

        Parameters
        ----------
            name (str): The name to register the Event under.

        Returns
        -------
            Event: The Event registered under the given name — if an Event with that name already exists, the existing Event is returned.

        """
        warnings.warn(
            "createEvent is deprecated; use create_event instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.create_event(name)

    def get_event(self, name: str) -> Optional[Event]:
        """
        Retrieve the Event registered under the given name.

        Returns:
            Event or None: `Event` if the name is tracked, `None` otherwise.

        """
        with self._lock:
            return self._events.get(name)

    def getEvent(self, name: str) -> Optional[Event]:
        """
        Compatibility wrapper for retrieving a named event; emits a DeprecationWarning.

        Parameters
        ----------
            name (str): Name of the tracked event to retrieve.

        Returns
        -------
            Event or None: The registered Event with the given name, or None if no such event exists.

        """
        warnings.warn(
            "getEvent is deprecated; use get_event instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.get_event(name)

    def start_thread(self, thread: ThreadLike) -> None:
        """
        Start a tracked thread if it is registered with the coordinator and has not yet been started.

        If the thread is not registered or has already been started, this method does nothing. Logs a warning when an untracked thread is provided.
        """
        should_start = False
        with self._lock:
            if thread not in self._threads:
                logger.warning(
                    "Cannot start untracked thread %r (ident=%s); coordinator may be cleaned up or a create_thread/cleanup race occurred",
                    thread,
                    thread.ident,
                )
                return
            # thread.ident is None only if the thread has never been started
            # This prevents RuntimeError from calling start() on a completed thread
            # Also check _pending_start to prevent TOCTOU race between checking ident and calling start()
            if thread.ident is None and thread not in self._pending_start:
                self._pending_start.add(thread)
                should_start = True
        # Start outside the lock to prevent deadlocks if the new thread
        # immediately calls back into the coordinator
        if should_start:
            try:
                thread.start()
            finally:
                with self._lock:
                    self._pending_start.discard(thread)

    def startThread(self, thread: ThreadLike) -> None:
        """
        Deprecated camelCase wrapper that starts a tracked thread.

        Emits a DeprecationWarning and attempts to start the provided thread if it is registered and not already started.
        """
        warnings.warn(
            "startThread is deprecated; use start_thread instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.start_thread(thread)

    def join_thread(self, thread: ThreadLike, timeout: Optional[float] = None) -> None:
        """
        Join a tracked thread if it is alive and not the current thread.

        Parameters
        ----------
            thread (Thread): Thread object previously registered with this
                coordinator; no-op if the thread is not tracked, not alive, or
                is the current thread.
            timeout (Optional[float]): Maximum seconds to wait for the thread
                to finish; use `None` to wait indefinitely.

        """
        with self._lock:
            should_join = (
                thread in self._threads
                and thread.is_alive()
                and thread is not current_thread()
            )
        if should_join:
            thread.join(timeout=timeout)

    def joinThread(self, thread: ThreadLike, timeout: Optional[float] = None) -> None:
        """
        Deprecated camelCase alias that joins a tracked Thread if it is alive and not the current thread; emits a DeprecationWarning and delegates to the new behavior.

        Parameters
        ----------
                thread (Thread): The thread to join; only joined if tracked by the coordinator, alive, and not the current thread.
                timeout (Optional[float]): Maximum seconds to wait for the thread to finish, or None to wait indefinitely.

        """
        warnings.warn(
            "joinThread is deprecated; use join_thread instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.join_thread(thread, timeout=timeout)

    def join_all(self, timeout: Optional[float] = None) -> None:
        """
        Join all tracked, alive threads except the calling thread, applying the given timeout to each join.

        Parameters
        ----------
            timeout (Optional[float]): Per-thread join timeout in seconds. If `None`, wait indefinitely for each thread.

        """
        with self._lock:
            current = current_thread()
            threads_to_join = [
                thread
                for thread in self._threads
                if thread.is_alive() and thread is not current
            ]
        for thread in threads_to_join:
            thread.join(timeout=timeout)

    def joinAll(self, timeout: Optional[float] = None) -> None:
        """
        Deprecated camelCase wrapper that joins all tracked, alive threads except the current thread.

        Waits up to `timeout` seconds per thread while joining; if `timeout` is None it will block indefinitely for each thread. Emits a DeprecationWarning advising use of the snake_case `join_all` method.
        """
        warnings.warn(
            "joinAll is deprecated; use join_all instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.join_all(timeout=timeout)

    def _set_event_no_lock(self, name: str) -> None:
        """
        Set the named tracked event without acquiring the coordinator lock.

        If no event with the given name is registered, this is a no-op.
        """
        event = self._events.get(name)
        if event is not None:
            event.set()

    def _clear_event_no_lock(self, name: str) -> None:
        """
        Clear the tracked event named `name` without acquiring the coordinator lock.

        If no event is registered under `name`, this is a no-op. This method clears the event so waiting threads will block until it is set again.

        Parameters
        ----------
            name (str): The name of the event to clear.

        """
        event = self._events.get(name)
        if event is not None:
            event.clear()

    def set_event(self, name: str) -> None:
        """
        Set the coordinator's named event, waking any threads waiting on it.

        If no event is registered under the given name, this is a no-op.

        Parameters
        ----------
            name (str): The registered event name to set.

        """
        with self._lock:
            self._set_event_no_lock(name)

    def setEvent(self, name: str) -> None:
        """
        Deprecated camelCase wrapper that delegates to set_event.

        Issues a DeprecationWarning and forwards the call to set_event(name).
        """
        warnings.warn(
            "setEvent is deprecated; use set_event instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.set_event(name)

    def clear_event(self, name: str) -> None:
        """
        Clear the tracked event with the given name.

        If an event with `name` exists, clear its flag so waiting threads will block until it is set again; otherwise do nothing.

        Parameters
        ----------
            name (str): Name of the tracked event to clear.

        """
        with self._lock:
            self._clear_event_no_lock(name)

    def clearEvent(self, name: str) -> None:
        """Compatibility wrapper for callers using camelCase."""
        warnings.warn(
            "clearEvent is deprecated; use clear_event instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.clear_event(name)

    def wait_for_event(self, name: str, timeout: Optional[float] = None) -> bool:
        """
        Waits for the tracked event named `name` to be set or until `timeout` seconds elapse.

        Parameters
        ----------
            name (str): Name of the tracked event.
            timeout (float | None): Maximum time in seconds to wait; `None` means wait indefinitely.

        Returns
        -------
            `true` if the event was set before the timeout, `false` otherwise (also `false` if no event with that name is tracked).

        """
        event = self.get_event(name)
        if event:
            return event.wait(timeout=timeout)
        return False

    def waitForEvent(self, name: str, timeout: Optional[float] = None) -> bool:
        """
        Compatibility wrapper for camelCase callers that waits for the named event and emits a deprecation warning.

        Returns:
            True if the event was set within the optional timeout, False otherwise.

        """
        warnings.warn(
            "waitForEvent is deprecated; use wait_for_event instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.wait_for_event(name, timeout=timeout)

    def check_and_clear_event(self, name: str) -> bool:
        """
        Clears the named tracked event if it exists and is currently set.

        Returns:
            True if the named event existed and was set (and was cleared), False otherwise.

        """
        with self._lock:
            event = self._events.get(name)
            if event and event.is_set():
                event.clear()
                return True
            return False

    def checkAndClearEvent(self, name: str) -> bool:
        """
        Deprecated camelCase alias that checks whether a named event is set and clears it if so.

        Parameters
        ----------
            name (str): Name of the tracked event to check and clear.

        Returns
        -------
            bool: `True` if the event existed and was set (and was cleared), `False` otherwise.

        """
        warnings.warn(
            "checkAndClearEvent is deprecated; use check_and_clear_event instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.check_and_clear_event(name)

    def wake_waiting_threads(self, *event_names: str) -> None:
        """
        Wake all coordinator-managed events named in event_names, causing any threads waiting on them to resume.

        Parameters
        ----------
                event_names (str): One or more event names to set; names that are not tracked are ignored.

        """
        with self._lock:
            for name in event_names:
                self._set_event_no_lock(name)

    def wakeWaitingThreads(self, *event_names: str) -> None:
        """
        Compatibility camelCase method that wakes the named events to wake any waiting threads.

        Parameters
        ----------
            event_names (str): One or more event names to signal.

        Deprecated:
            This method is deprecated; prefer the snake_case equivalent.

        """
        warnings.warn(
            "wakeWaitingThreads is deprecated; use wake_waiting_threads instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.wake_waiting_threads(*event_names)

    def clear_events(self, *event_names: str) -> None:
        """
        Clear the named tracked events.

        Ignores names that are not registered.

        Parameters
        ----------
            event_names (str): One or more event names to clear.

        """
        with self._lock:
            for name in event_names:
                self._clear_event_no_lock(name)

    def clearEvents(self, *event_names: str) -> None:
        """
        Deprecated camelCase wrapper that clears the specified named events.

        Emits a DeprecationWarning and clears each event whose name is provided.

        Parameters
        ----------
            event_names (str): One or more event names to clear.

        """
        warnings.warn(
            "clearEvents is deprecated; use clear_events instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.clear_events(*event_names)

    def cleanup(self) -> None:
        """
        Perform a coordinated shutdown of the coordinator.

        Marks the coordinator as cleaned, sets all tracked events to wake any waiters, collects and joins all live tracked threads except the current thread (using EVENT_THREAD_JOIN_TIMEOUT per thread), and clears internal thread and event registries. After this completes, the coordinator will refuse creation of new runnable threads (requests return an inert thread that cannot be started).
        """
        with self._lock:
            # Mark as cleaned up to prevent new thread creation
            self._cleaned_up = True

            # Signal all events
            for event in self._events.values():
                event.set()

            current = current_thread()
            threads_to_join = [
                thread
                for thread in self._threads
                if thread.is_alive() and thread is not current
            ]

            # Clear tracking
            self._threads.clear()
            self._events.clear()

        # Join threads outside the lock to avoid deadlocks if threads touch the coordinator during shutdown
        for thread in threads_to_join:
            thread.join(timeout=EVENT_THREAD_JOIN_TIMEOUT)
