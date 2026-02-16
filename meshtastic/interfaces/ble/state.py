"""BLE connection state management.

Lock Ordering Note:
    When acquiring multiple locks in the BLE subsystem, always acquire in this order:
    1. Global registry lock (_REGISTRY_LOCK in gating.py)
    2. Per-address locks (_ADDR_LOCKS in gating.py, via addr_lock_context)
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
from typing import ClassVar, Dict, Set

from meshtastic.interfaces.ble.constants import logger


class ConnectionState(Enum):
    """Enum for managing BLE connection states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class BLEStateManager:
    """Thread-safe state management for BLE connections.

    Replaces multiple locks and boolean flags with a single state machine
    for clearer connection management and reduced complexity.
    """

    _VALID_TRANSITIONS: ClassVar[Dict[ConnectionState, Set[ConnectionState]]] = {
        ConnectionState.DISCONNECTED: {
            ConnectionState.CONNECTING,
            ConnectionState.ERROR,
            ConnectionState.DISCONNECTING,
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
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
        },
    }

    def __init__(self):
        """
        Create a BLEStateManager with a reentrant lock and initial state set to DISCONNECTED.

        Initializes the internal reentrant lock stored on self._state_lock and sets self._state to ConnectionState.DISCONNECTED for thread-safe state management.
        """
        self._state_lock = RLock()  # Single reentrant lock for all state changes
        self._state = ConnectionState.DISCONNECTED

    @property
    def lock(self) -> RLock:
        """
        Provide the reentrant lock used to synchronize BLE state transitions.

        Returns
        -------
            RLock: The internal reentrant lock protecting state changes.

        """
        return self._state_lock

    @property
    def state(self) -> ConnectionState:
        """
        Get the current BLE connection state.

        Returns
        -------
            ConnectionState: The current connection state (read while holding the manager's internal lock).

        """
        with self._state_lock:
            return self._state

    @property
    def is_connected(self) -> bool:
        """
        Indicates whether the BLE interface is in the connected state.

        Returns
        -------
            `true` if the current state is CONNECTED, `false` otherwise.

        """
        return self.state == ConnectionState.CONNECTED

    @property
    def is_closing(self) -> bool:
        """
        Indicate whether the BLE interface is in a closing state.

        Returns
        -------
            True if the current state is DISCONNECTING, False otherwise.

        """
        return self.state == ConnectionState.DISCONNECTING

    @property
    def can_connect(self) -> bool:
        """
        Indicates whether a new BLE connection may be initiated.

        Returns
        -------
            True if the current state is DISCONNECTED or ERROR, False otherwise.

        """
        return self.state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR)

    @property
    def is_connecting(self) -> bool:
        """
        Indicates whether the BLE interface is in a connecting state.

        Returns
        -------
            True if the current state is CONNECTING or RECONNECTING, False otherwise.

        """
        return self.state in (ConnectionState.CONNECTING, ConnectionState.RECONNECTING)

    @property
    def is_active(self) -> bool:
        """
        Report whether the BLE interface has an active or pending connection.

        Returns
        -------
            bool: `True` if the current state is CONNECTING, RECONNECTING, or CONNECTED, `False` otherwise.

        """
        return self.state in (
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
            ConnectionState.CONNECTED,
        )

    def transition_to(self, new_state: ConnectionState) -> bool:
        """
        Attempt to transition the manager's BLE connection state to the specified target state.

        The requested transition is validated against the manager's state machine; transitioning to the current state is treated as a valid no-op.

        Returns
        -------
            bool: `True` if the state was changed or the target equals the current state; `False` if the transition is invalid.

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
        """
        Check whether a transition between two ConnectionState values is allowed.

        Parameters
        ----------
            from_state (ConnectionState): Current connection state.
            to_state (ConnectionState): Proposed next connection state.

        Returns
        -------
            bool: `True` if the transition from `from_state` to `to_state` is allowed, `False` otherwise.

        """
        return to_state in self._VALID_TRANSITIONS.get(from_state, set())

    def transition_if_in(
        self, expected_states: Set[ConnectionState], new_state: ConnectionState
    ) -> bool:
        """
        Atomically check current state and transition if in expected states.

        This is a convenience method that combines state checking and transition
        into a single atomic operation, preventing TOCTOU race conditions.

        Parameters
        ----------
        expected_states : Set[ConnectionState]
            Set of states from which the transition is allowed.
        new_state : ConnectionState
            Target state to transition to.

        Returns
        -------
        bool: True if the current state was in expected_states and the transition
            succeeded, False otherwise.

        """
        with self._state_lock:
            if self._state not in expected_states:
                logger.debug(
                    "State check failed: expected one of %s, got %s",
                    {s.value for s in expected_states},
                    self._state.value,
                )
                return False
            return self.transition_to(new_state)

    def reset_to_disconnected(self) -> bool:
        """
        Forces the connection state to DISCONNECTED regardless of the current state.

        Useful for error recovery and cleanup where the BLE state must be reset.

        Returns
        -------
            bool: True indicating the state was set to DISCONNECTED.

        """
        with self._state_lock:
            old_state = self._state
            self._state = ConnectionState.DISCONNECTED
            logger.debug("State reset: %s → disconnected", old_state.value)
            return True
