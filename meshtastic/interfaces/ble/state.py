"""BLE connection state management."""

from enum import Enum
from threading import RLock
from typing import TYPE_CHECKING, Optional

from meshtastic.interfaces.ble.constants import logger

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient


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

    def __init__(self):
        """Initialize state manager with disconnected state."""
        self._state_lock = RLock()  # Single reentrant lock for all state changes
        self._state = ConnectionState.DISCONNECTED
        self._client: Optional["BLEClient"] = None

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
        Return the current BLE connection state.

        Returns
        -------
            ConnectionState: The current connection state (read under the manager's internal lock).
        """
        with self._state_lock:
            return self._state

    @property
    def is_connected(self) -> bool:
        """
        Indicate whether the BLE interface is in the connected state.

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
        Determine if a new BLE connection may be initiated.

        Returns
        -------
            `true` if the current state is DISCONNECTED or ERROR, `false` otherwise.
        """
        return self.state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR)

    @property
    def client(self) -> Optional["BLEClient"]:
        """
        Get the currently associated BLE client, if any; access is protected by the manager's lock.

        Returns
        -------
            Optional[BLEClient]: The current BLE client, or `None` if no client is set.
        """
        with self._state_lock:
            return self._client

    def transition_to(
        self, new_state: ConnectionState, client: Optional["BLEClient"] = None
    ) -> bool:
        """
        Attempt to change the manager's BLE connection state to the given target state.

        If the transition is allowed, updates the internal state and sets the stored client when a `client` is provided; when transitioning to `DISCONNECTED` with no `client`, the stored client is cleared.

        Parameters
        ----------
        new_state : Any
            ConnectionState Target state to transition to.
        client : Any
            BLE client to associate with the new state. If omitted and `new_state` is `ConnectionState.DISCONNECTED`, the stored client is cleared.

        Returns
        -------
        bool: `True` if the transition was valid and applied, `False` otherwise.
        """
        with self._state_lock:
            if new_state == self._state:
                # Idempotent no-op: avoid warning noise for redundant transitions
                logger.debug("State transition noop: already in %s", self._state.value)
                return False
            if self._is_valid_transition(self._state, new_state):
                old_state = self._state
                self._state = new_state

                # Update client reference if provided
                if client is not None:
                    self._client = client
                elif new_state == ConnectionState.DISCONNECTED:
                    self._client = None

                logger.debug(f"State transition: {old_state.value} → {new_state.value}")
                return True
            else:
                logger.warning(
                    f"Invalid state transition: {self._state.value} → {new_state.value}"
                )
                return False

    def _is_valid_transition(
        self, from_state: ConnectionState, to_state: ConnectionState
    ) -> bool:
        """
        Determine whether changing from one ConnectionState to another is permitted.

        Parameters
        ----------
        from_state : Any
            ConnectionState Current connection state.
        to_state : Any
            ConnectionState Proposed next connection state.

        Returns
        -------
        bool: True if the transition from from_state to to_state is allowed, False otherwise.
        """
        # Define valid transitions based on connection lifecycle
        valid_transitions = {
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

        return to_state in valid_transitions.get(from_state, set())
