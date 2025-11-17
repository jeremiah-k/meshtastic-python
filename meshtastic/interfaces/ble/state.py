"""State management for BLE connections."""
import logging
from enum import Enum
from threading import RLock
from typing import Optional, TYPE_CHECKING


if TYPE_CHECKING:
    from .client import BLEClient


logger = logging.getLogger(__name__)


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
        """
        Create a BLEStateManager with the initial DISCONNECTED state and a reentrant lock.
        
        Initializes a single RLock used to serialize state transitions, sets the internal state to
        ConnectionState.DISCONNECTED, and initializes the associated client reference to None.
        """
        self._state_lock = RLock()  # Single reentrant lock for all state changes
        self._state = ConnectionState.DISCONNECTED
        self._client: Optional["BLEClient"] = None

    @property
    def lock(self) -> RLock:
        """Expose the reentrant lock controlling state transitions."""
        return self._state_lock

    @property
    def state(self) -> ConnectionState:
        """
        Retrieve the current BLE connection state.
        
        Returns:
            current_state (ConnectionState): The active connection state.
        """
        with self._state_lock:
            return self._state

    @property
    def is_connected(self) -> bool:
        """
        Indicates whether the manager is in the CONNECTED state.
        
        Returns:
            True if the current state is ConnectionState.CONNECTED, False otherwise.
        """
        return self.state == ConnectionState.CONNECTED

    @property
    def is_closing(self) -> bool:
        """
        Indicates whether the connection is closing or in an error state.
        
        Returns:
            bool: `true` if the current state is DISCONNECTING or ERROR, `false` otherwise.
        """
        return self.state in (ConnectionState.DISCONNECTING, ConnectionState.ERROR)

    @property
    def can_connect(self) -> bool:
        """
        Indicates whether a new BLE connection may be started.
        
        Returns:
            True if the manager is in the DISCONNECTED state, False otherwise.
        """
        return self.state == ConnectionState.DISCONNECTED

    @property
    def client(self) -> Optional["BLEClient"]:
        """
        Return the BLE client associated with the current connection state.
        
        Returns:
            The associated `BLEClient` instance, or `None` if no client is set.
        """
        with self._state_lock:
            return self._client

    def transition_to(
        self, new_state: ConnectionState, client: Optional["BLEClient"] = None
    ) -> bool:
        """
        Attempt a thread-safe transition of the manager's connection state to the specified target state.
        
        Parameters:
            new_state (ConnectionState): Target state to transition to.
            client (Optional[BLEClient]): BLE client to associate with the new state. If omitted and `new_state` is `ConnectionState.DISCONNECTED`, the stored client reference is cleared.
        
        Returns:
            bool: `True` if the transition was allowed and applied, `False` otherwise.
        """
        with self._state_lock:
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
        Determine whether a transition between two connection states is allowed.
        
        Parameters:
            from_state (ConnectionState): Current connection state.
            to_state (ConnectionState): Candidate next connection state.
        
        Returns:
            bool: `True` if the transition from `from_state` to `to_state` is permitted, `False` otherwise.
        """
        # Define valid transitions based on connection lifecycle
        valid_transitions = {
            ConnectionState.DISCONNECTED: {
                ConnectionState.CONNECTING,
                ConnectionState.ERROR,
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