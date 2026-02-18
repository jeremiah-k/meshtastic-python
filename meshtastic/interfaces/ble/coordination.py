"""Thread coordination utilities for BLE operations."""

import logging
import warnings
from threading import Event, RLock, Thread, current_thread
from typing import Any, Callable, Dict, List, Optional, Tuple

from meshtastic.interfaces.ble.constants import EVENT_THREAD_JOIN_TIMEOUT

logger = logging.getLogger("meshtastic.ble")


class _InertThread:
    """
    A thread-like object that cannot be started.

    Returned by create_thread after cleanup to maintain API contract while
    preventing orphaned thread creation.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.daemon = True
        self.ident = None
        self._started = False

    def start(self) -> None:
        """Raise RuntimeError: inert threads cannot be started."""
        raise RuntimeError(
            f"Cannot start inert thread '{self.name}': coordinator has been cleaned up"
        )

    def is_alive(self) -> bool:
        """Return False: inert threads are never alive."""
        return False

    def join(self, timeout: Optional[float] = None) -> None:
        """No-op: inert threads cannot be joined."""
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
        Initialize the ThreadCoordinator that centralizes thread and event management.

        Initializes internal state:
            _lock (RLock): reentrant lock protecting coordinator state.
            _threads (List[Thread]): list of tracked Thread objects (initially empty).
            _events (Dict[str, Event]): mapping of event names to threading.Event objects (initially empty).
            _cleaned_up (bool): flag indicating whether cleanup() has been performed (initially False).
        """
        self._lock = RLock()
        self._threads: List[Thread] = []
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
    ) -> Thread:
        """
        Create and register a Thread with the coordinator without starting it.

        The returned Thread is configured with the provided target, name,
        daemon flag, and arguments and is registered for later lifecycle
        management; it is not started.

        Parameters
        ----------
            target (Callable): Callable to be executed by the thread.
            name (str): Name assigned to the thread.
            daemon (bool): Whether the thread should run as a daemon.
            args (tuple): Positional arguments to pass to `target`.
            kwargs (Optional[dict]): Keyword arguments to pass to `target`.

        Returns
        -------
            Thread: The created Thread instance (registered with the coordinator, not started).
                After cleanup, returns an inert thread that raises RuntimeError on start().

        """
        with self._lock:
            # Prevent thread creation after cleanup to avoid orphaned threads
            if self._cleaned_up:
                logger.warning(
                    "Cannot create thread '%s': coordinator has been cleaned up", name
                )
                # Return an inert thread that cannot be started
                return _InertThread(name)  # type: ignore[return-value]
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
    ) -> Thread:
        """Compatibility wrapper for callers using camelCase."""
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
        Register and return a new Event object under the given name.

        If an event with the same name already exists, a warning is logged and
        the existing Event instance is returned (no replacement occurs).

        This method is thread-safe and uses the coordinator's internal lock.

        Parameters
        ----------
            name (str): Key under which the Event will be stored.

        Returns
        -------
            Event: The created Event instance registered under `name`, or the
                existing Event if one was already registered with that name.

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
            Event: The newly created and registered Event.

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

    def start_thread(self, thread: Thread) -> None:
        """
        Start a tracked Thread if it has not been started yet.

        If the given thread is not registered with this coordinator or has already been started, no action is taken.

        Parameters
        ----------
            thread (Thread): The thread to start if it is tracked and not yet started.

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
            if thread.ident is None:
                should_start = True
        # Start outside the lock to prevent deadlocks if the new thread
        # immediately calls back into the coordinator
        if should_start:
            thread.start()

    def startThread(self, thread: Thread) -> None:
        """Compatibility wrapper for callers using camelCase."""
        warnings.warn(
            "startThread is deprecated; use start_thread instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.start_thread(thread)

    def join_thread(self, thread: Thread, timeout: Optional[float] = None) -> None:
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

    def joinThread(self, thread: Thread, timeout: Optional[float] = None) -> None:
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
        """Compatibility wrapper for callers using camelCase."""
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
        """Clear a tracked event without acquiring the coordinator lock."""
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
        """Compatibility wrapper for callers using camelCase."""
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
            `True` if the event was set before the timeout, `False` otherwise.

        """
        event = self.get_event(name)
        if event:
            return event.wait(timeout=timeout)
        return False

    def waitForEvent(self, name: str, timeout: Optional[float] = None) -> bool:
        """Compatibility wrapper for callers using camelCase."""
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
        """Compatibility wrapper for callers using camelCase."""
        warnings.warn(
            "wakeWaitingThreads is deprecated; use wake_waiting_threads instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.wake_waiting_threads(*event_names)

    def clear_events(self, *event_names: str) -> None:
        """
        Clear the named tracked events, ignoring any names that are not registered.

        Parameters
        ----------
            event_names (str): One or more event names to clear.

        """
        with self._lock:
            for name in event_names:
                self._clear_event_no_lock(name)

    def clearEvents(self, *event_names: str) -> None:
        """Compatibility wrapper for callers using camelCase."""
        warnings.warn(
            "clearEvents is deprecated; use clear_events instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.clear_events(*event_names)

    def cleanup(self) -> None:
        """
        Perform a coordinated shutdown by waking tracked events, joining live tracked threads (except the current thread), and clearing internal registries.

        Sets every tracked Event to wake any waiting threads, collects live tracked Thread objects (excluding the current thread), clears the coordinator's thread and event registries under the coordinator lock, and then joins the collected threads outside the lock using the timeout defined by EVENT_THREAD_JOIN_TIMEOUT. After cleanup is called, create_thread will log a warning and return an untracked Thread to avoid creating orphaned threads.
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
