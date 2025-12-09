"""Thread coordination utilities for BLE operations."""

import logging
from threading import Event, RLock, Thread, current_thread
from typing import List, Optional

from meshtastic.interfaces.ble.constants import EVENT_THREAD_JOIN_TIMEOUT

logger = logging.getLogger("meshtastic.ble")

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
        Create and register a Thread tracked by this coordinator without starting it.
        
        Parameters:
            target (callable): Callable to be executed by the thread.
            name (str): Name assigned to the thread.
            daemon (bool): Whether the thread should run as a daemon.
            args (tuple): Positional arguments to pass to `target`.
            kwargs (dict | None): Keyword arguments to pass to `target`.
        
        Returns:
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
        Create and register an Event under the given name.
        
        Parameters:
            name (str): Name to register the event under.
        
        Returns:
            event (Event): The created Event instance.
        """
        with self._lock:
            event = Event()
            self._events[name] = event
            return event

    def get_event(self, name: str) -> Optional[Event]:
        """
        Retrieve a tracked Event by name.
        
        Parameters:
            name (str): The event's identifier.
        
        Returns:
            Optional[Event]: `Event` if an event with the given name exists, `None` otherwise.
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
        Join the specified tracked thread if it is alive, tracked by this coordinator, and not the current thread.
        
        Parameters:
            thread (Thread): Thread to join; no-op if the thread is not tracked, not alive, or is the current thread.
            timeout (Optional[float]): Maximum seconds to wait for the thread to finish; use `None` to wait indefinitely.
        """
        with self._lock:
            should_join = (
                thread in self._threads
                and thread.is_alive()
                and thread is not current_thread()
            )
        if should_join:
            thread.join(timeout=timeout)

    def join_all(self, timeout: Optional[float] = None):
        """
        Wait for all tracked threads with the specified timeout.

        Args:
        ----
            timeout (Optional[float]): Maximum number of seconds to wait for each thread to join.
                If `None`, wait indefinitely for each thread.

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

    def set_event(self, name: str):
        """
        Set a tracked event by name.
        
        If no event is registered under the given name, this method does nothing.
        
        Parameters:
        	name (str): Name of the event to set.
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
        Wait for the tracked event with the given name to become set or for the timeout to elapse.
        
        Parameters:
            name (str): Name of the tracked event to wait for.
            timeout (Optional[float]): Maximum time in seconds to wait; None means wait indefinitely.
        
        Returns:
            bool: True if the event was set before the timeout, False otherwise (including when the event is not tracked).
        """
        event = self.get_event(name)
        if event:
            return event.wait(timeout=timeout)
        return False

    def check_and_clear_event(self, name: str) -> bool:
        """
        Clear the named tracked event if it is currently set.
        
        Parameters:
            name (str): Name of the tracked event.
        
        Returns:
            bool: `True` if the event was set and cleared, `False` otherwise.
        """
        event = self.get_event(name)
        if event and event.is_set():
            event.clear()
            return True
        return False

    def wake_waiting_threads(self, *event_names: str):
        """
        Wake threads waiting on the given coordinator-managed events by setting each named event.
        
        Parameters:
            event_names (str): One or more event names previously registered with `create_event`. Names not tracked by the coordinator are ignored.
        """
        for name in event_names:
            self.set_event(name)

    def clear_events(self, *event_names: str):
        """
        Clear multiple tracked events by name.
        
        Clears each named event stored in the coordinator; names not tracked are ignored.
        
        Parameters:
            event_names (str): One or more event names to clear.
        """
        for name in event_names:
            self.clear_event(name)

    def cleanup(self):
        """
        Signal all tracked events, join live tracked threads (excluding the current thread), and clear the coordinator's internal registries.
        
        Sets every tracked Event to wake waiting threads, collects all live Thread objects except the current thread, clears the coordinator's thread and event registries under the lock, then joins the collected threads outside the lock using a short timeout.
        """
        with self._lock:
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