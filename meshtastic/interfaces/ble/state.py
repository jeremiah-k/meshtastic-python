"""BLE State management"""
import logging
from enum import Enum
from threading import RLock
from typing import Optional, TYPE_CHECKING


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
        Create a BLEStateManager initialized to DISCONNECTED with a dedicated reentrant lock.
        
        Initializes internal attributes used to manage connection state:
        - _state_lock (RLock): reentrant lock that guards state transitions.
        - _state (ConnectionState): current connection state, set to DISCONNECTED.
        - _client (Optional[BLEClient]): optional BLE client reference, initially None.
        """
        self._state_lock = RLock()  # Single reentrant lock for all state changes
        self._state = ConnectionState.DISCONNECTED
        self._client: Optional["BLEClient"] = None

    @property
    def lock(self) -> RLock:
        """
        Expose the internal reentrant lock used to synchronize state transitions.
        
        Returns:
            RLock: The reentrant lock protecting the manager's internal state.
        """
        return self._state_lock

    @property
    def state(self) -> ConnectionState:
        """
        Current BLE connection state.
        
        Returns:
            ConnectionState: The current connection state.
        """
        with self._state_lock:
            return self._state

    @property
    def is_connected(self) -> bool:
        """
        Indicates whether the BLE connection is in the CONNECTED state.
        
        Returns:
            bool: True if the current state is CONNECTED, False otherwise.
        """
        return self.state == ConnectionState.CONNECTED

    @property
    def is_closing(self) -> bool:
        """
        Whether the BLE interface is in the process of closing or has encountered an error.
        
        Returns:
            `true` if the current state is DISCONNECTING or ERROR, `false` otherwise.
        """
        return self.state in (ConnectionState.DISCONNECTING, ConnectionState.ERROR)

    @property
    def can_connect(self) -> bool:
        """
        Indicates whether a new BLE connection may be initiated.
        
        Returns:
            `true` if the current state is ConnectionState.DISCONNECTED, `false` otherwise.
        """
        return self.state == ConnectionState.DISCONNECTED

    @property
    def client(self) -> Optional["BLEClient"]:
        """
        Retrieve the current BLE client associated with the manager.
        
        Returns:
            The `BLEClient` instance if one is set, `None` otherwise.
        """
        with self._state_lock:
            return self._client

    def transition_to(
        self, new_state: ConnectionState, client: Optional["BLEClient"] = None
    ) -> bool:
        """
        Perform a thread-safe transition of the connection state, validating the move before applying it.
        
        Parameters:
            new_state (ConnectionState): Target state to transition to.
            client (Optional[BLEClient]): BLE client to associate with the manager for this state;
                if provided the manager's client is set to this value. If omitted and
                new_state is ConnectionState.DISCONNECTED, the manager's client is cleared.
        
        Returns:
            bool: `True` if the transition was valid and applied, `False` otherwise.
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
            logger.warning(
                f"Invalid state transition: {self._state.value} → {new_state.value}"
            )
            return False

    def _is_valid_transition(
        self, from_state: ConnectionState, to_state: ConnectionState
    ) -> bool:
        """
        Determine whether transitioning from one ConnectionState to another is allowed by the state machine.
        
        Returns:
            `true` if the transition is allowed, `false` otherwise.
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