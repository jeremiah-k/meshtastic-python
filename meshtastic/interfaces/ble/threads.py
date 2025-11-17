"""Thread management for BLE operations."""
from threading import Event, RLock, Thread, current_thread
from typing import List, Optional, Dict


EVENT_THREAD_JOIN_TIMEOUT = 2.0


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
        Create a ThreadCoordinator used to track and manage threads and events.

        Initializes:
            _lock (RLock): reentrant lock protecting internal state.
            _threads (List[Thread]): list of tracked Thread objects.
            _events (dict[str, Event]): mapping of event names to threading.Event objects for coordination.
        """
        self._lock = RLock()
        self._threads: List[Thread] = []
        self._events: dict[str, Event] = {}

    def create_thread(
        self, target, name: str, *, daemon: bool = True, args=(), kwargs=None
    ) -> Thread:
        """
        Create and register a new Thread with target, name, daemon, args, and kwargs.

        Args:
        ----
            target (callable): The callable to be executed by the thread.
            name (str): The thread's name.
            daemon (bool): Whether the thread should be a daemon thread.
            args (tuple): Positional arguments to pass to `target`.
            kwargs (dict): Keyword arguments to pass to `target`.

        Returns:
        -------
            Thread: The created Thread instance (added to the coordinator's tracked threads, not started).

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
        Retrieve a previously created Event by the specified name.

        Args:
        ----
            name (str): The identifier of the tracked event.

        Returns:
        -------
            Optional[Event]: The Event instance if found, otherwise `None`.

        """
        with self._lock:
            return self._events.get(name)

    def start_thread(self, thread: Thread):
        """
        Start the given thread if it is tracked by this coordinator.

        Only threads previously added to the coordinator's tracking list will be started; otherwise the call has no effect.
        """
        with self._lock:
            if thread in self._threads:
                thread.start()

    def join_thread(self, thread: Thread, timeout: Optional[float] = None):
        """
        Join a tracked thread with the specified thread and timeout.

        Args:
        ----
                thread (Thread): The thread to join; only joined if it is currently tracked and alive.
                timeout (float | None): Maximum seconds to wait for the thread to finish. If None, wait indefinitely.

        """
        thread_to_join: Optional[Thread] = None
        with self._lock:
            if (
                thread in self._threads
                and thread.is_alive()
                and thread is not current_thread()
            ):
                thread_to_join = thread
        if thread_to_join:
            thread_to_join.join(timeout=timeout)

    def join_all(self, timeout: Optional[float] = None):
        """
        Wait for all tracked threads with the specified timeout.

        Args:
        ----
            timeout (Optional[float]): Maximum number of seconds to wait for each thread to join.
                If `None`, wait indefinitely for each thread.

        """
        threads_to_join: List[Thread] = []
        with self._lock:
            current = current_thread()
            for thread in self._threads:
                if thread.is_alive() and thread is not current:
                    threads_to_join.append(thread)
        for thread in threads_to_join:
            thread.join(timeout=timeout)

    def set_event(self, name: str):
        """
        Set the tracked event with the specified name.

        If no event exists with that name, this is a no-op.

        Args:
        ----
            name (str): Name of the tracked event to set.

        Returns:
        -------
            None.

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
        Wait until the named tracked event is set or until the timeout elapses.

        Args:
        ----
            name (str): Name of the tracked event to wait for.
            timeout (Optional[float]): Maximum time in seconds to wait; None means wait indefinitely.

        Returns:
        -------
            bool: True if the event was set before the timeout, False otherwise or if the event is not tracked.

        """
        event = self.get_event(name)
        if event:
            return event.wait(timeout=timeout)
        return False

    def check_and_clear_event(self, name: str) -> bool:
        """
        Check whether a tracked event is set and clear it if set.

        Args:
        ----
            name (str): The tracked event's name.

        Returns:
        -------
            bool: `True` if the event was set (and was cleared), `False` otherwise.

        """
        event = self.get_event(name)
        if event and event.is_set():
            event.clear()
            return True
        return False

    def wake_waiting_threads(self, *event_names: str):
        """
        Set the named events so any threads waiting on them are woken.

        Args:
        ----
            event_names (str): One or more names of events previously created via create_event; each named event will be set.

        """
        for name in event_names:
            self.set_event(name)

    def clear_events(self, *event_names: str):
        """
        Clear multiple tracked events by name.

        Args:
        ----
            event_names (str): One or more event names to clear; names that are not tracked are ignored.

        """
        for name in event_names:
            self.clear_event(name)

    def cleanup(self):
        """
        Signal all tracked events, join and stop tracked threads, and clear coordinator state.

        Sets every tracked Event, joins each tracked Thread (except current) with a short timeout,
        then clears internal thread and event registries so the coordinator no longer tracks them.
        """
        threads_to_join: List[Thread] = []
        with self._lock:
            # Signal all events
            for event in self._events.values():
                event.set()

            # Join threads with timeout (except current thread)
            current = current_thread()
            for thread in self._threads:
                if thread.is_alive() and thread is not current:
                    threads_to_join.append(thread)

            # Clear tracking
            self._threads.clear()
            self._events.clear()

        for thread in threads_to_join:
            thread.join(timeout=EVENT_THREAD_JOIN_TIMEOUT)
