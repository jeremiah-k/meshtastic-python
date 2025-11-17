"""BLE notification management."""
import logging
from threading import RLock
from typing import Any, Callable, Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .client import BLEClient

logger = logging.getLogger(__name__)


class NotificationManager:
    """
    Manage BLE notification subscriptions so we can resubscribe cleanly after reconnects.
    """

    def __init__(self):
        """
        Initialize a NotificationManager instance and its internal state for tracking BLE subscriptions.
        
        Sets up:
        - _active_subscriptions: mapping from a unique integer token to (characteristic, callback).
        - _subscription_counter: monotonically increasing integer used to generate tokens.
        - _lock: re-entrant lock to synchronize access to the subscription mapping.
        """
        self._active_subscriptions: Dict[
            int, Tuple[str, Callable[[Any, Any], None]]
        ] = {}
        self._subscription_counter = 0
        self._lock = RLock()

    def subscribe(self, characteristic: str, callback) -> int:
        """
        Register a notification callback for the given BLE characteristic and return a token for later management.
        
        Parameters:
            characteristic (str): Identifier of the BLE characteristic (e.g., UUID or handle) to subscribe to.
            callback (Callable[[Any, Any], None]): Function invoked when a notification for the characteristic is received.
        
        Returns:
            int: A unique token that identifies the tracked subscription for cleanup or resubscription.
        """
        with self._lock:
            token = self._subscription_counter
            self._subscription_counter += 1
            self._active_subscriptions[token] = (characteristic, callback)
            return token

    def cleanup_all(self) -> None:
        """
        Clear all tracked subscriptions.
        
        After this call the manager will not retain any subscription records.
        """
        with self._lock:
            self._active_subscriptions.clear()

    def resubscribe_all(self, client: "BLEClient", *, timeout: float) -> None:
        """
        Attempt to re-subscribe all tracked characteristic callbacks on the given BLE client.
        
        This performs a best-effort resubscription for every currently tracked (characteristic, callback) pair:
        per-subscription failures are caught and logged; this method does not raise on individual resubscription errors.
        
        Parameters:
            timeout (float): Maximum time in seconds to wait for each subscription attempt.
        """
        with self._lock:
            # Copy the subscriptions so we can perform I/O without holding the lock.
            subscriptions = list(self._active_subscriptions.values())

        for characteristic, callback in subscriptions:
            try:
                client.start_notify(
                    characteristic,
                    callback,
                    timeout=timeout,
                )
            except Exception as e:  # pragma: no cover  # noqa: BLE001 - best effort
                logger.debug(
                    "Failed to resubscribe %s during reconnect: %s",
                    characteristic,
                    e,
                )

    def __len__(self) -> int:
        """
        Return the number of currently tracked BLE notification subscriptions.
        
        Returns:
            int: The number of active subscriptions.
        """
        with self._lock:
            return len(self._active_subscriptions)

    def get_callback(self, characteristic: str) -> Optional[Callable[[Any, Any], None]]:
        """
        Retrieve the callback function registered for the specified BLE characteristic.
        
        Parameters:
            characteristic (str): The UUID or identifier of the BLE characteristic to look up.
        
        Returns:
            Optional[Callable[[Any, Any], None]]: The callback associated with `characteristic` if one exists, `None` otherwise.
        """
        with self._lock:
            for registered_char, callback in self._active_subscriptions.values():
                if registered_char == characteristic:
                    return callback
        return None