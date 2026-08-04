"""Typed BLE runtime fakes for lifecycle tests."""

from __future__ import annotations

from collections.abc import Callable

from meshtastic.interfaces.ble.state import ConnectionState


class _FakeStateManager:
    """Minimal state-manager fake implementing the current lifecycle contract."""

    def __init__(
        self,
        *,
        state: ConnectionState = ConnectionState.DISCONNECTED,
        connected: bool = False,
        closing: bool = False,
    ) -> None:
        self.state = state
        self.connected = connected
        self.closing = closing
        self.transition_calls: list[ConnectionState] = []
        self.reset_calls = 0

    @property
    def is_connected(self) -> bool:
        """Return the configured connected state."""
        return self.connected

    @is_connected.setter
    def is_connected(self, value: bool) -> None:
        """Update the configured connected state."""
        self.connected = value

    @property
    def current_state(self) -> ConnectionState:
        """Return the configured connection state."""
        return self.state

    @current_state.setter
    def current_state(self, value: ConnectionState) -> None:
        """Update the configured connection state."""
        self.state = value

    def transition_to(self, new_state: ConnectionState) -> bool:
        """Record and apply a state transition."""
        self.transition_calls.append(new_state)
        self.state = new_state
        self.connected = new_state is ConnectionState.CONNECTED
        return True

    def reset_to_disconnected(self) -> bool:
        """Record and apply a reset to disconnected state."""
        self.reset_calls += 1
        self.state = ConnectionState.DISCONNECTED
        self.connected = False
        return True

    @property
    def is_closing(self) -> bool:
        """Return the configured closing state."""
        return self.closing

    @is_closing.setter
    def is_closing(self, value: bool) -> None:
        """Update the configured closing state."""
        self.closing = value


class _FakeConnectedClient:
    """BLE client fake with an explicit public connectivity probe."""

    def __init__(self, *, connected: bool) -> None:
        self.connected = connected
        self.probe_calls = 0

    def isConnected(self) -> bool:  # noqa: N802 - public compatibility spelling
        """Return the configured connectivity state."""
        self.probe_calls += 1
        return self.connected


class _FakeThread:
    """Small thread-like fake used by coordinator tests."""

    def __init__(self) -> None:
        self.started = False
        self.join_timeouts: list[float | None] = []

    def start(self) -> None:
        """Record a start call."""
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        """Record a join call."""
        self.join_timeouts.append(timeout)


class _FakeThreadCoordinator:
    """Thread-coordinator fake implementing current lifecycle hooks explicitly."""

    def __init__(self) -> None:
        self.created: list[tuple[str, bool, _FakeThread]] = []
        self.started: list[object] = []
        self.joined: list[tuple[object, float | None]] = []
        self.events: list[str] = []
        self.cleared_events: list[tuple[str, ...]] = []
        self.wake_calls = 0
        self.wake_events: list[tuple[str, ...]] = []

    def create_thread(
        self,
        *,
        target: Callable[..., object],
        name: str,
        daemon: bool = True,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
    ) -> _FakeThread:
        """Create and record a deterministic thread-like object."""
        _ = (target, args, kwargs)
        thread = _FakeThread()
        self.created.append((name, daemon, thread))
        return thread

    def start_thread(self, thread: object) -> None:
        """Record and start a thread-like object when possible."""
        self.started.append(thread)
        start = getattr(thread, "start", None)
        if callable(start):
            start()

    def join_thread(self, thread: object, *, timeout: float | None = None) -> None:
        """Record and join a thread-like object when possible."""
        self.joined.append((thread, timeout))
        join = getattr(thread, "join", None)
        if callable(join):
            join(timeout=timeout)

    def set_event(self, event_name: str) -> None:
        """Record an event set operation."""
        self.events.append(event_name)

    def clear_events(self, *event_names: str) -> None:
        """Record an event clear operation."""
        self.cleared_events.append(tuple(event_names))

    def wake_waiting_threads(self, *event_names: str) -> None:
        """Record a wake request and its requested event names."""
        self.wake_calls += 1
        self.wake_events.append(tuple(event_names))
