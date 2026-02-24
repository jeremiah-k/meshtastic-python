"""BLE connection state management.

Lock Ordering Note:
    When acquiring multiple locks in the BLE subsystem, always acquire in this order:
    1. Global registry lock (_REGISTRY_LOCK in gating.py)
    2. Per-address locks (_ADDR_LOCKS in gating.py, via _addr_lock_context)
    3. Interface connect lock (_connect_lock)
    4. Interface state lock (_state_lock)
    5. Interface disconnect lock (_disconnect_lock)

    This ordering prevents deadlocks in concurrent connection scenarios.

    Note: _disconnect_lock is acquired non-blocking first for early-return
    optimization, then _state_lock is acquired. This is intentional for
    handling concurrent disconnect callbacks.
"""

from enum import Enum
from threading import RLock
from typing import ClassVar

from meshtastic.interfaces.ble.constants import logger


class ConnectionState(Enum):
    """Enum for managing BLE connection states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    # Reserved reconnect lifecycle state for compatibility/observability.
    RECONNECTING = "reconnecting"
    ERROR = "error"


class BLEStateManager:
    """Thread-safe state management for BLE connections.

    Replaces multiple locks and boolean flags with a single state machine
    for clearer connection management and reduced complexity.
    """

    _VALID_TRANSITIONS: ClassVar[dict[ConnectionState, set[ConnectionState]]] = {
        ConnectionState.DISCONNECTED: {
            ConnectionState.CONNECTING,
            ConnectionState.ERROR,
            # Note: DISCONNECTING is intentionally NOT allowed from DISCONNECTED.
            # A disconnected interface cannot "begin disconnecting" - it's already disconnected.
            # close() explicitly checks for DISCONNECTED state and skips the transition.
        },
        ConnectionState.CONNECTING: {
            ConnectionState.CONNECTED,
            ConnectionState.DISCONNECTING,
            ConnectionState.ERROR,
            ConnectionState.DISCONNECTED,
        },
        ConnectionState.CONNECTED: {
            ConnectionState.DISCONNECTING,
            ConnectionState.RECONNECTING,
            ConnectionState.DISCONNECTED,
            ConnectionState.ERROR,
        },
        ConnectionState.DISCONNECTING: {
            ConnectionState.DISCONNECTED,
            ConnectionState.ERROR,
        },
        ConnectionState.RECONNECTING: {
            ConnectionState.CONNECTED,
            ConnectionState.DISCONNECTING,
            ConnectionState.CONNECTING,
            ConnectionState.ERROR,
            ConnectionState.DISCONNECTED,
        },
        ConnectionState.ERROR: {
            ConnectionState.DISCONNECTED,
            ConnectionState.DISCONNECTING,
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
        },
    }

    def __init__(self) -> None:
        """Initialize the BLEStateManager by creating its reentrant lock and setting the initial connection state.

        Creates an RLock for serializing state transitions and sets the internal state to ConnectionState.DISCONNECTED.

        Returns
        -------
        None
        """
        self._state_lock = RLock()  # Single reentrant lock for all state changes
        self._state = ConnectionState.DISCONNECTED

    @property
    def _lock(self) -> RLock:
        """Access the internal RLock that serializes BLE state transitions.

        Returns
        -------
        RLock
            The reentrant lock used to protect state changes.
        """
        return self._state_lock

    @property
    def _current_state(self) -> ConnectionState:
        """Get the current BLE connection state.

        This property reads the manager's state while holding the internal reentrant lock to provide a consistent, thread-safe view.

        Returns
        -------
        ConnectionState
            The current connection state.
        """
        with self._state_lock:
            return self._state

    @property
    def _is_connected(self) -> bool:
        """Whether the BLE interface is currently in the CONNECTED state.

        This is a point-in-time snapshot and is not an atomic check+act
        primitive. Callers that need atomic transitions must hold
        `_state_lock` or use `_transition_if_in(...)`.

        Returns
        -------
        bool
            True if the current connection state is ConnectionState.CONNECTED, False otherwise.
        """
        return self._current_state == ConnectionState.CONNECTED

    @property
    def _is_closing(self) -> bool:
        """Internal property: Indicates whether the BLE interface is in the DISCONNECTING state.

        This is a point-in-time snapshot and is not an atomic check+act
        primitive. Callers that need atomic transitions must hold
        `_state_lock` or use `_transition_if_in(...)`.

        Returns
        -------
        bool
            True if the current connection state is DISCONNECTING, False otherwise.
        """
        return self._current_state == ConnectionState.DISCONNECTING

    @property
    def _can_connect(self) -> bool:
        """Whether a new BLE connection may be initiated.

        This is a point-in-time snapshot and is not an atomic check+act
        primitive. Callers that need atomic transitions must hold
        `_state_lock` or use `_transition_if_in(...)`.

        Returns
        -------
        bool
            True if the current state is DISCONNECTED or ERROR, False otherwise.
        """
        return self._current_state in (
            ConnectionState.DISCONNECTED,
            ConnectionState.ERROR,
        )

    @property
    def _is_connecting(self) -> bool:
        """Whether the BLE interface is in a connecting state.

        This is a point-in-time snapshot and is not an atomic check+act
        primitive. Callers that need atomic transitions must hold
        `_state_lock` or use `_transition_if_in(...)`.

        Returns
        -------
        bool
            True if the current state is CONNECTING or RECONNECTING, False otherwise.
        """
        return self._current_state in (
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
        )

    @property
    def _is_active(self) -> bool:
    """Return whether the BLE interface currently has an active or pending connection.

        This is a point-in-time snapshot and is not an atomic check+act
        primitive. Callers that need atomic transitions must hold
        `_state_lock` or use `_transition_if_in(...)`.

        Returns
        -------
        bool
            `True` if the current state is CONNECTING, RECONNECTING, or CONNECTED, `False` otherwise.
        """
        return self._current_state in (
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
            ConnectionState.CONNECTED,
        )

    def _transition_to(self, new_state: ConnectionState) -> bool:
        """Internal method: Attempt to change the manager's BLE connection state to the given target state.

        Validates the requested transition against the state machine and treats a transition to the current state as a valid no-op.

        Parameters
        ----------
        new_state : ConnectionState
            Target connection state to transition to.

        Returns
        -------
        bool
            `True` if the state was changed or already equals the target state, `False` if the transition is invalid.
        """
        with self._state_lock:
            if new_state == self._state:
                # No-op transition: already in target state, this is valid
                logger.debug("State transition no-op: already in %s", self._state.value)
                return True
            if self._is_valid_transition(self._state, new_state):
                old_state = self._state
                self._state = new_state
                logger.debug(
                    "State transition: %s → %s", old_state.value, new_state.value
                )
                return True
            logger.warning(
                "Invalid state transition: %s → %s",
                self._state.value,
                new_state.value,
            )
            return False

    def _is_valid_transition(
        self, from_state: ConnectionState, to_state: ConnectionState
    ) -> bool:
    """Return whether transitioning from one ConnectionState to another is permitted by the state machine.

        Parameters
        ----------
        from_state : ConnectionState
            Current connection state.
        to_state : ConnectionState
            Proposed next connection state.

        Returns
        -------
        bool
            True if the transition from `from_state` to `to_state` is allowed, False otherwise.
        """
        return to_state in self._VALID_TRANSITIONS.get(from_state, set())

    def _transition_if_in(
        self, expected_states: set[ConnectionState], new_state: ConnectionState
    ) -> bool:
        """Atomically transition to `new_state` only if the current state is one of `expected_states`.

        Performs the check and potential transition while holding the internal state lock to avoid time-of-check-to-time-of-use races.

        Parameters
        ----------
        expected_states : set[ConnectionState]
            Allowed current states for the transition.
        new_state : ConnectionState
            Target state to transition to.

        Returns
        -------
        bool
            `True` if the current state was in `expected_states` and the transition to `new_state` succeeded, `False` otherwise.
        """
        with self._state_lock:
            if self._state not in expected_states:
                logger.debug(
                    "State check failed: expected one of %s, got %s",
                    sorted(s.value for s in expected_states),
                    self._state.value,
                )
                return False
            return self._transition_to(new_state)

    def _reset_to_disconnected(self) -> bool:
        """Force the connection state to DISCONNECTED for recovery or cleanup.

        Acquires the internal state lock and attempts a transition to DISCONNECTED; this will be a no-op if the state is already DISCONNECTED.

        Returns
        -------
        bool
            `True` if the transition succeeded or was a no-op (state is DISCONNECTED after the call), `False` otherwise.
        """
        with self._state_lock:
            # Prefer validated transition semantics; this is a no-op when already
            # disconnected and remains resilient to future transition-map edits.
            return self._transition_to(ConnectionState.DISCONNECTED)
