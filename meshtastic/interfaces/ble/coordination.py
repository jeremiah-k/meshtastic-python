"Utility helpers for coordinating interface-owned threads and events."

from __future__ import annotations

from threading import Event, RLock, Thread, current_thread
from typing import Dict, Optional

from .config import EVENT_THREAD_JOIN_TIMEOUT


class ThreadCoordinator:
    """Track and manage interface-owned threads and events."""

    def __init__(self):
        self._threads: list[Thread] = []
        self._events: Dict[str, Event] = {}
        self._lock = RLock()

    def create_thread(
        self,
        *,
        target,
        name: str,
        daemon: bool = True,
        args=(),
        kwargs=None,
    ) -> Thread:
        """Create and register a thread without starting it."""
        kwargs = kwargs or {}
        with self._lock:
            thread = Thread(
                target=target, name=name, daemon=daemon, args=args, kwargs=kwargs
            )
            self._threads.append(thread)
            return thread

    def create_event(self, name: str) -> Event:
        """Create and track a named event."""
        with self._lock:
            event = Event()
            self._events[name] = event
            return event

    def get_event(self, name: str) -> Optional[Event]:
        """Return the tracked event with the provided name."""
        with self._lock:
            return self._events.get(name)

    def start_thread(self, thread: Thread) -> None:
        """Start the thread if it is tracked."""
        with self._lock:
            if thread in self._threads:
                thread.start()

    def join_thread(self, thread: Thread, timeout: Optional[float] = None) -> None:
        """Join a tracked thread with the specified timeout."""
        with self._lock:
            should_join = (
                thread in self._threads
                and thread.is_alive()
                and thread is not current_thread()
            )
        if should_join:
            thread.join(timeout=timeout)

    def join_all(self, timeout: Optional[float] = None) -> None:
        """Join all tracked threads with the specified timeout."""
        with self._lock:
            current = current_thread()
            to_join = [t for t in self._threads if t.is_alive() and t is not current]
        for thread in to_join:
            thread.join(timeout=timeout)

    def set_event(self, name: str) -> None:
        """Set the tracked event with the specified name."""
        with self._lock:
            if name in self._events:
                self._events[name].set()

    def clear_event(self, name: str) -> None:
        """Clear the tracked event with the specified name."""
        with self._lock:
            if name in self._events:
                self._events[name].clear()

    def wait_for_event(self, name: str, timeout: Optional[float] = None) -> bool:
        """Wait until the named tracked event is set or until the timeout elapses."""
        event = self.get_event(name)
        if event:
            return event.wait(timeout=timeout)
        return False

    def check_and_clear_event(self, name: str) -> bool:
        """Return True if the event was set and clear it."""
        event = self.get_event(name)
        if event and event.is_set():
            event.clear()
            return True
        return False

    def wake_waiting_threads(self, *event_names: str) -> None:
        """Set the named events to wake any waiting threads."""
        for name in event_names:
            self.set_event(name)

    def clear_events(self, *event_names: str) -> None:
        """Clear multiple tracked events by name."""
        for name in event_names:
            self.clear_event(name)

    def cleanup(self) -> None:
        """Signal all events, join tracked threads, and reset state."""
        with self._lock:
            events = list(self._events.values())
            current = current_thread()
            to_join = [t for t in self._threads if t.is_alive() and t is not current]
            self._threads.clear()
            self._events.clear()

        for event in events:
            event.set()
        for thread in to_join:
            thread.join(timeout=EVENT_THREAD_JOIN_TIMEOUT)


__all__ = ["ThreadCoordinator"]
