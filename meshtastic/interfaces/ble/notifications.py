"""BLE notification management."""

import logging
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient

logger = logging.getLogger("meshtastic.ble")


class NotificationManager:
    """
    Manage BLE notification subscriptions so we can resubscribe cleanly after reconnects.
    """

    def __init__(self):
        """
        Create a NotificationManager and initialize its thread-safe subscription state.
        
        Attributes:
            _active_subscriptions (dict[int, tuple[str, Callable[[Any, Any], None]]]):
                Maps a unique subscription token to a (characteristic, callback) pair for each tracked subscription.
            _characteristic_to_callback (dict[str, Callable[[Any, Any], None]]):
                Maps a characteristic UUID to the most recently registered callback for that characteristic.
            _subscription_counter (int):
                Monotonic counter used to allocate unique subscription tokens.
            _lock (RLock):
                Re-entrant lock protecting access to the subscription state for thread safety.
        """
        self._active_subscriptions: Dict[
            int, Tuple[str, Callable[[Any, Any], None]]
        ] = {}
        self._characteristic_to_callback: Dict[str, Callable[[Any, Any], None]] = {}
        self._subscription_counter = 0
        self._lock = RLock()

    def subscribe(
        self, characteristic: str, callback: Callable[[Any, Any], None]
    ) -> int:
        """
        Track a notification callback for a BLE characteristic so it can be cleaned up or re-registered later.
        
        Parameters
        ----------
        characteristic : str
            Identifier of the BLE characteristic (for example, a UUID string or handle).
        callback : Callable[[Any, Any], None]
            Function invoked when a notification arrives; typically called as (sender, data).
        
        Returns
        -------
        token : int
            Unique opaque token that identifies the tracked subscription.
        
        Notes
        -----
        Multiple subscriptions to the same characteristic are allowed; the most recently
        registered callback for a characteristic is returned by `get_callback()`, while
        all tracked subscriptions are retained for cleanup or resubscription attempts.
        """
        with self._lock:
            token = self._subscription_counter
            self._subscription_counter += 1
            self._active_subscriptions[token] = (characteristic, callback)
            self._characteristic_to_callback[characteristic] = callback
            return token

    def cleanup_all(self) -> None:
        """
        Remove all tracked BLE notification subscriptions and associated callbacks from the manager.
        
        This clears the internal subscription registry and the per-characteristic callback mapping.
        """
        with self._lock:
            self._active_subscriptions.clear()
            self._characteristic_to_callback.clear()

    def resubscribe_all(self, client: "BLEClient", *, timeout: float) -> None:
        """
        Re-register all tracked BLE notification subscriptions on the provided client.
        
        Attempts to start notifications for each tracked (characteristic, callback) pair. Each subscription is attempted independently; failures for individual characteristics are caught and logged at debug level.
        
        Parameters:
            client (BLEClient): BLE client used to start notifications.
            timeout (float): Per-subscription timeout passed to the client's `start_notify` method.
        """
        with self._lock:
            subscriptions = list(self._active_subscriptions.values())

        for characteristic, callback in subscriptions:
            try:
                client.start_notify(
                    characteristic,
                    callback,
                    timeout=timeout,
                )
            except Exception as e:  # pragma: no cover - best effort; noqa: BLE001
                logger.debug(
                    "Failed to resubscribe %s during reconnect: %s",
                    characteristic,
                    e,
                )

    def __len__(self) -> int:
        """
        Get the count of active BLE notification subscriptions being tracked.
        
        Returns:
            int: Number of active subscriptions currently tracked.
        """
        with self._lock:
            return len(self._active_subscriptions)

    def get_callback(self, characteristic: str) -> Optional[Callable[[Any, Any], None]]:
        """
        Retrieve the callback registered for a BLE characteristic.
        
        Parameters:
            characteristic (str): Identifier of the BLE characteristic (for example, a UUID or name).
        
        Returns:
            Optional[Callable[[Any, Any], None]]: The callback registered for the characteristic, or `None` if none is registered.
        """
        with self._lock:
            return self._characteristic_to_callback.get(characteristic)