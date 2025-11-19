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
        Create and register an Event object under the given name.
        
        Parameters:
            name (str): Name used as the key to store and retrieve the event.
        
        Returns:
            Event: The created Event instance.
        """
        with self._lock:
            event = Event()
            self._events[name] = event
            return event

    def get_event(self, name: str) -> Optional[Event]:
        """
        Retrieve a tracked Event by name.
        
        Parameters:
            name (str): Name of the event to retrieve.
        
        Returns:
            Optional[Event]: The Event if found, `None` otherwise.
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
        Join a tracked worker thread if it is alive and not the caller.
        
        Parameters:
            thread (Thread): Thread previously registered with this coordinator; no action is taken if the thread is not tracked, is not alive, or is the current thread.
            timeout (Optional[float]): Maximum seconds to wait for the thread to finish; `None` to wait indefinitely.
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
        Join all tracked threads that are alive and not the current thread.
        
        Each tracked thread is joined individually using the provided timeout.
        
        Parameters:
            timeout (Optional[float]): Maximum number of seconds to wait for each thread to finish;
                use `None` to wait indefinitely for each thread.
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
        Set the named tracked event to wake any waiting threads.
        
        If no event is registered under the given name, this is a no-op.
        """
        with self._lock:
            if name in self._events:
                self._events[name].set()

    def clear_event(self, name: str):
        """
        Clear the tracked event with the given name.
        
        If an event with the specified name is registered, clear its internal flag; otherwise no action is taken.
        
        Parameters:
            name (str): Name of the tracked event to clear.
        """
        with self._lock:
            if name in self._events:
                self._events[name].clear()

    def wait_for_event(self, name: str, timeout: Optional[float] = None) -> bool:
        """
        Wait for a named tracked event to be set.
        
        Parameters:
            name (str): Name of the tracked event to wait for.
            timeout (Optional[float]): Maximum time in seconds to wait; None means wait indefinitely.
        
        Returns:
            bool: `True` if the event was set before the timeout; `False` otherwise (including when the named event is not registered).
        """
        event = self.get_event(name)
        if event:
            return event.wait(timeout=timeout)
        return False

    def check_and_clear_event(self, name: str) -> bool:
        """
        Check whether a tracked event is set and, if so, clear it.
        
        Parameters:
            name (str): Name of the tracked event to check.
        
        Returns:
            bool: `True` if the named event was set (and was cleared), `False` if it was not set or not tracked.
        """
        event = self.get_event(name)
        if event and event.is_set():
            event.clear()
            return True
        return False

    def wake_waiting_threads(self, *event_names: str):
        """
        Wake threads waiting on the given named events by setting each event.
        
        Parameters:
            event_names (str): One or more event names previously registered with `create_event`.
        """
        for name in event_names:
            self.set_event(name)

    def clear_events(self, *event_names: str):
        """
        Clear multiple tracked events by name.
        
        Each provided name that is registered will be cleared; names that are not tracked are ignored.
        
        Parameters:
            event_names (str): One or more event names to clear.
        """
        for name in event_names:
            self.clear_event(name)

    def cleanup(self):
        """
        Signal all registered events, join tracked worker threads, and clear the coordinator's state.
        
        Sets every tracked Event to wake any waiters, attempts to join each tracked Thread that is alive and not the current thread using EVENT_THREAD_JOIN_TIMEOUT, and then clears the internal thread and event registries so the coordinator no longer tracks them.
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