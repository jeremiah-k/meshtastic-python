"""BLE notification management."""

import logging
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable

from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_SUBSCRIPTION_TOKEN_EXHAUSTED,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient

logger = logging.getLogger("meshtastic.ble")


class NotificationManager:
    """Manage BLE notification subscriptions so we can resubscribe cleanly after reconnects."""

    _MAX_SUBSCRIPTION_TOKEN = 0x7FFFFFFF


    def __init__(self) -> None:
        """Initialize a NotificationManager and its thread-safe subscription state.

        Initializes the following internal attributes used for tracking subscriptions:
        - _active_subscriptions: maps a unique subscription token to a (characteristic, callback) pair.
        - _characteristic_to_callback: maps a characteristic UUID to the most recently registered callback.
        - _subscription_counter: monotonic counter used to allocate unique subscription tokens.
        - _lock: re-entrant lock protecting access to subscription state.

        Returns
        -------
        None
        """
        self._active_subscriptions: dict[
            int, tuple[str, Callable[[Any, Any], None]]
        ] = {}
        self._characteristic_to_callback: dict[str, Callable[[Any, Any], None]] = {}
        self._subscription_counter = 0
        self._lock = RLock()

    def _subscribe(
        self, characteristic: str, callback: Callable[[Any, Any], None]
    ) -> int:
        """Track a notification callback for a BLE characteristic so it can be cleaned up or re-registered later.

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
        registered callback for a characteristic is returned by `_get_callback()`, while
        all tracked subscriptions are retained for cleanup or resubscription attempts.
        """
        with self._lock:
            token = self._subscription_counter
            # Wrap at 2 billion to prevent unbounded growth in long-running processes
            self._subscription_counter = (
                self._subscription_counter + 1
            ) & self._MAX_SUBSCRIPTION_TOKEN
            # Detect a full cycle through the token space instead of using a fixed
            # iteration cap, which can raise prematurely under wraparound collisions.
            start_token = token
            while token in self._active_subscriptions:
                token = self._subscription_counter
                self._subscription_counter = (
                    self._subscription_counter + 1
                ) & self._MAX_SUBSCRIPTION_TOKEN
                if token == start_token:
                    raise RuntimeError(BLECLIENT_ERROR_SUBSCRIPTION_TOKEN_EXHAUSTED)
            self._active_subscriptions[token] = (characteristic, callback)
            self._characteristic_to_callback[characteristic] = callback
            return token

    def _cleanup_all(self) -> None:
        """Clear all tracked BLE notification subscriptions and per-characteristic callbacks.

        Removes every active subscription entry, clears the characteristic-to-callback mapping, and resets the subscription token counter to zero. This operation is performed while holding the manager's internal lock.

        Returns
        -------
        None
        """
        with self._lock:
            self._active_subscriptions.clear()
            self._characteristic_to_callback.clear()
            self._subscription_counter = 0

    def _unsubscribe_all(self, client: "BLEClient", *, timeout: float | None) -> None:
        """Stop notifications for every characteristic currently tracked and ignore any errors.

        Parameters
        ----------
        client : 'BLEClient'
            BLE client used to stop notifications.
        timeout : float | None
            Per-unsubscribe timeout passed to the client's `stopNotify` method; may be None.

        Returns
        -------
        None
        """
        with self._lock:
            characteristics = list(self._characteristic_to_callback.keys())

        for characteristic in characteristics:
            try:
                client.stopNotify(characteristic, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "Failed to unsubscribe %s during shutdown: %s",
                    characteristic,
                    e,
                )

    def _resubscribe_all(self, client: "BLEClient", *, timeout: float | None) -> None:
        """Resubscribe all tracked BLE notification callbacks on the given client.

        Uses the per-characteristic latest callback to avoid duplicate resubscription attempts when
        multiple subscriptions were registered for the same characteristic.

        Parameters
        ----------
        client : 'BLEClient'
            BLE client on which to call `start_notify` for each characteristic.
        timeout : float | None
            Per-subscription timeout to pass to the client's `start_notify` method.

        Returns
        -------
        None
        """
        with self._lock:
            # Use _characteristic_to_callback to deduplicate - it holds only the latest
            # callback per characteristic, avoiding redundant resubscription attempts
            subscriptions = list(self._characteristic_to_callback.items())

        for characteristic, callback in subscriptions:
            try:
                client.start_notify(
                    characteristic,
                    callback,
                    timeout=timeout,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "Failed to resubscribe %s during reconnect: %s",
                    characteristic,
                    e,
                )

    def __len__(self) -> int:
        """Report the number of active BLE notification subscriptions being tracked.

        Returns
        -------
        int
            Number of active subscriptions currently tracked.
        """
        with self._lock:
            return len(self._active_subscriptions)

    def _get_callback(self, characteristic: str) -> Callable[[Any, Any], None] | None:
        """Retrieve the most recently registered callback for a BLE characteristic.

        Parameters
        ----------
        characteristic : str
            BLE characteristic identifier (e.g., UUID or handle) to look up.

        Returns
        -------
        Callable[[Any, Any], None] | None
            The most recently registered callback for the characteristic, or `None` if no callback is registered.
        """
        with self._lock:
            return self._characteristic_to_callback.get(characteristic)
