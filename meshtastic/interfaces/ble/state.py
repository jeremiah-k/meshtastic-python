"""Connection state tracking for the BLE interface."""

from __future__ import annotations

from enum import Enum
from threading import RLock
from typing import Optional, TYPE_CHECKING

from .config import logger

if TYPE_CHECKING:
    from .client import BLEClient


class ConnectionState(Enum):
    """Enum for managing BLE connection states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class BLEStateManager:
    """Thread-safe state management for BLE connections."""

    def __init__(self):
        self._state_lock = RLock()
        self._state = ConnectionState.DISCONNECTED
        self._client: Optional["BLEClient"] = None

    @property
    def lock(self) -> RLock:
        """Expose the reentrant lock controlling state transitions."""
        return self._state_lock

    @property
    def state(self) -> ConnectionState:
        """Get current connection state."""
        with self._state_lock:
            return self._state

    @property
    def client(self) -> Optional["BLEClient"]:
        """Return the current BLE client, if any."""
        with self._state_lock:
            return self._client

    @property
    def is_connected(self) -> bool:
        """Return True if the interface is connected."""
        with self._state_lock:
            return self._state == ConnectionState.CONNECTED

    @property
    def is_closing(self) -> bool:
        """Return True if the interface is closing or in an error state."""
        with self._state_lock:
            return self._state in (
                ConnectionState.DISCONNECTING,
                ConnectionState.ERROR,
            )

    @property
    def can_connect(self) -> bool:
        """Return True if connection attempts are currently allowed."""
        with self._state_lock:
            return self._state == ConnectionState.DISCONNECTED

    def transition_to(
        self, new_state: ConnectionState, client: Optional["BLEClient"] = None
    ) -> bool:
        """Attempt to transition to `new_state` while holding the state lock."""
        with self._state_lock:
            if self._state == new_state:
                logger.debug("State transition ignored; already %s", new_state.value)
                return False

            valid_transitions = {
                ConnectionState.DISCONNECTED: {
                    ConnectionState.CONNECTING,
                    ConnectionState.DISCONNECTING,
                    ConnectionState.ERROR,
                },
                ConnectionState.CONNECTING: {
                    ConnectionState.CONNECTED,
                    ConnectionState.ERROR,
                    ConnectionState.DISCONNECTED,
                    ConnectionState.DISCONNECTING,
                },
                ConnectionState.CONNECTED: {
                    ConnectionState.DISCONNECTING,
                    ConnectionState.DISCONNECTED,
                    ConnectionState.ERROR,
                },
                ConnectionState.DISCONNECTING: {
                    ConnectionState.DISCONNECTED,
                    ConnectionState.ERROR,
                },
                ConnectionState.ERROR: {
                    ConnectionState.CONNECTING,
                    ConnectionState.DISCONNECTED,
                },
                ConnectionState.RECONNECTING: {
                    ConnectionState.CONNECTED,
                    ConnectionState.DISCONNECTED,
                    ConnectionState.ERROR,
                },
            }

            allowed = valid_transitions.get(self._state, set())
            if new_state not in allowed:
                logger.warning(
                    "Invalid state transition: %s \u2192 %s",
                    self._state.value,
                    new_state.value,
                )
                return False

            logger.debug(
                "State transition: %s \u2192 %s",
                self._state.value,
                new_state.value,
            )
            self._state = new_state

            if new_state == ConnectionState.DISCONNECTED:
                self._client = None
            elif client is not None:
                self._client = client
            return True


__all__ = ["BLEStateManager", "ConnectionState"]
