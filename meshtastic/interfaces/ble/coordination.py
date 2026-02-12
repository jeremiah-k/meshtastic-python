"""Thread coordination utilities for BLE operations."""

import logging
from threading import Event, RLock, Thread, current_thread
from typing import Callable, Optional

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
        self._threads: list[Thread] = []
        self._events: dict[str, Event] = {}

    def create_thread(
        self,
        target: "Callable",
        name: str,
        *,
        daemon: bool = True,
        args: tuple = (),
        kwargs: Optional[dict] = None,
    ) -> Thread:
        """
        Create and register a Thread tracked by this coordinator without starting it.
        
        Prunes dead threads from the coordinator's registry, creates a Thread configured
        with the provided arguments, and registers it for lifecycle management. The
        returned thread is not started.
        
        Parameters:
            target (Callable): Callable to be executed by the thread.
            name (str): Name assigned to the thread.
            daemon (bool): Whether the thread should run as a daemon.
            args (tuple): Positional arguments to pass to `target`.
            kwargs (Optional[dict]): Keyword arguments to pass to `target`.
        
        Returns:
            Thread: The created Thread instance (registered with the coordinator, not started).
        """
        with self._lock:
            # Prune dead threads to prevent unbounded growth in long-running processes
            self._threads = [t for t in self._threads if t.is_alive()]
            thread = Thread(
                target=target, name=name, daemon=daemon, args=args, kwargs=kwargs
            )
            self._threads.append(thread)
            return thread

    def create_event(self, name: str) -> Event:
        """
        Create and register a threading.Event under the given name.
        
        Parameters:
            name (str): Key to register the event under.
        
        Returns:
            Event: The created Event instance registered under `name`.
        """
        with self._lock:
            if name in self._events:
                logger.warning("Replacing existing event: %s", name)
            event = Event()
            self._events[name] = event
            return event

    def get_event(self, name: str) -> Optional[Event]:
        """
        Get the Event registered under the given name.
        
        Parameters:
            name (str): Name of the tracked event.
        
        Returns:
            Optional[Event]: `Event` if an event with the given name exists, `None` otherwise.
        """
        with self._lock:
            return self._events.get(name)

    def start_thread(self, thread: Thread):
        """
        Start the given thread if it is tracked by this coordinator and has never been started.

        Only threads previously added to the coordinator's tracking list will be started;
        threads that have already been started (even if completed) are skipped to avoid
        RuntimeError from calling start() on a non-fresh thread.
        """
        with self._lock:
            # thread.ident is None only if the thread has never been started
            # This prevents RuntimeError from calling start() on a completed thread
            if thread in self._threads and thread.ident is None:
                thread.start()

    def join_thread(self, thread: Thread, timeout: Optional[float] = None):
        """
        Join a tracked thread if it is alive and not the current thread.
        
        Parameters:
            thread (Thread): Thread object previously registered with this coordinator; no-op if the thread is not tracked, not alive, or is the current thread.
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
        Join all tracked, alive threads except the calling thread, applying the given timeout to each join.
        
        Parameters:
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

    def set_event(self, name: str):
        """
        Set the named event registered with the coordinator.
        
        If no event with that name is registered, this method does nothing.
        """
        with self._lock:
            if name in self._events:
                self._events[name].set()

    def clear_event(self, name: str):
        """
        Clear the coordinator's tracked Event with the given name.
        
        If an Event with `name` is tracked, clear its flag; otherwise do nothing.
        
        Parameters:
            name (str): Name of the tracked event to clear.
        """
        with self._lock:
            if name in self._events:
                self._events[name].clear()

    def wait_for_event(self, name: str, timeout: Optional[float] = None) -> bool:
        """
        Waits for a named tracked event to be set or until the timeout elapses.
        
        Parameters:
            name (str): Name of the tracked event to wait for.
            timeout (float | None): Maximum time in seconds to wait; None means wait indefinitely.
        
        Returns:
            bool: True if the event was set before the timeout, False otherwise (also False if the event is not tracked).
        """
        event = self.get_event(name)
        if event:
            return event.wait(timeout=timeout)
        return False

    def check_and_clear_event(self, name: str) -> bool:
        """
        Clear the tracked event with the given name if it is currently set.
        
        Returns:
            True if the named event existed and was set (and was cleared), False if the event was not found or was not set.
        """
        with self._lock:
            event = self._events.get(name)
            if event and event.is_set():
                event.clear()
                return True
            return False

    def wake_waiting_threads(self, *event_names: str):
        """
        Wake threads waiting on the named coordinator-managed events.
        
        Parameters:
            event_names (str): One or more event names previously registered with `create_event`. Names not tracked by the coordinator are ignored.
        """
        for name in event_names:
            self.set_event(name)

    def clear_events(self, *event_names: str):
        """
        Clear multiple tracked events by name.
        
        Parameters:
            event_names (str): One or more event names to clear; names not registered with the coordinator are ignored.
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
