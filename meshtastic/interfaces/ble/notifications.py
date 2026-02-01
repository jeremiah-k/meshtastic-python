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
        Initialize a NotificationManager instance and its thread-safe subscription state.

        Creates internal attributes used to track BLE notification subscriptions:
        - _active_subscriptions: mapping from token (int) to (characteristic, callback) tuples.
        - _subscription_counter: monotonic counter used to allocate unique subscription tokens.
        - _lock: RLock to synchronize access to the subscription state across threads.
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
        Register a BLE characteristic notification callback for tracking so it can be cleaned up or re-registered later.

        Parameters
        ----------
        characteristic : Any
            Identifier of the BLE characteristic (e.g., UUID or handle) being subscribed to.
        callback : Any
            Any], None] Function invoked when a notification arrives; typically called with (sender, data).

        Returns
        -------
                token (int): Opaque token that identifies the tracked subscription.

        Note:
        ----
            Multiple subscriptions to the same characteristic are allowed; the most recently
            registered callback is the one returned by `get_callback()` while all tracked
            subscriptions are attempted during resubscribe.
        """
        with self._lock:
            token = self._subscription_counter
            self._subscription_counter += 1
            self._active_subscriptions[token] = (characteristic, callback)
            self._characteristic_to_callback[characteristic] = callback
            return token

    def cleanup_all(self) -> None:
        """
        Clear all tracked BLE notification subscriptions so the manager no longer remembers them.
        """
        with self._lock:
            self._active_subscriptions.clear()
            self._characteristic_to_callback.clear()

    def resubscribe_all(self, client: "BLEClient", *, timeout: float) -> None:
        """
        Attempt to re-register all tracked BLE notification subscriptions on the given client.

        Attempts to start notifications for each tracked (characteristic, callback) pair using the provided client. Each subscription is attempted independently on a best-effort basis; failures for individual characteristics are caught and logged at debug level.

        Parameters
        ----------
        client : Any
            BLE client used to start notifications.
        timeout : Any
            Per-subscription timeout passed to the client's start_notify method.
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
        Return the number of tracked BLE notification subscriptions.

        Returns
        -------
        int: Number of active subscriptions currently being tracked.
        """
        with self._lock:
            return len(self._active_subscriptions)

    def get_callback(self, characteristic: str) -> Optional[Callable[[Any, Any], None]]:
        """
        Retrieve the callback registered for a BLE characteristic.

        Parameters
        ----------
        characteristic : Any
            Identifier of the characteristic (e.g., UUID or name) to look up.

        Returns
        -------
            Optional[Callable[[Any, Any], None]]: The registered callback for the characteristic if present, `None` otherwise.
        """
        with self._lock:
            return self._characteristic_to_callback.get(characteristic)
