"""BLE GATT constants and NotificationManager"""

import asyncio
import logging
from threading import RLock
from typing import Any, Callable, Dict, Optional, Tuple, TYPE_CHECKING

from bleak.exc import BleakError

from .client import BLEClient
from .exceptions import BLEError

logger = logging.getLogger(__name__)


SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"
LEGACY_LOGRADIO_UUID = "6c6fd238-78fa-436b-aacf-15c5be1ef2e2"
LOGRADIO_UUID = "5a3d6e49-06e6-4423-9944-e9de8cdf9547"
MALFORMED_NOTIFICATION_THRESHOLD = 10


class NotificationManager:
    """
    Manage BLE notification subscriptions so we can resubscribe cleanly after reconnects.
    """

    def __init__(self):
        """
        Initialize a NotificationManager that tracks BLE notification subscriptions and provides thread-safe access.
        
        Attributes:
            _active_subscriptions (Dict[int, Tuple[str, Callable[[Any, Any], None]]]): Maps subscription tokens to (characteristic UUID, callback).
            _characteristic_index (Dict[str, Callable[[Any, Any], None]]): Maps characteristic UUIDs to their registered callback.
            _subscription_counter (int): Monotonic counter used to generate unique subscription tokens.
            _lock (RLock): Reentrant lock protecting internal state for thread-safe access.
        """
        self._active_subscriptions: Dict[
            int, Tuple[str, Callable[[Any, Any], None]]
        ] = {}
        self._characteristic_index: Dict[str, Callable[[Any, Any], None]] = {}
        self._subscription_counter = 0
        self._lock = RLock()

    def subscribe(self, characteristic: str, callback) -> int:
        """
        Register a notification callback for a BLE characteristic and return a token for later management.
        
        Parameters:
        	characteristic (str): The characteristic UUID or identifier to subscribe to.
        	callback (Callable[[Any, Any], None]): Function invoked on notifications (typically signature (sender, data)).
        
        Returns:
        	token (int): A unique integer token that identifies the stored subscription.
        """
        with self._lock:
            token = self._subscription_counter
            self._subscription_counter += 1
            self._active_subscriptions[token] = (characteristic, callback)
            self._characteristic_index[characteristic] = callback
            return token

    def cleanup_all(self) -> None:
        """
        Remove all tracked notification subscriptions and clear internal subscription state.
        """
        with self._lock:
            self._active_subscriptions.clear()
            self._characteristic_index.clear()

    def resubscribe_all(self, client: "BLEClient", *, timeout: float) -> None:
        """
        Re-registers all tracked subscriptions on the given BLE client.
        
        Each tracked (characteristic, callback) pair is passed to the client's start_notify with the provided timeout. Failures for individual subscriptions are logged and do not stop attempts to resubscribe others.
        
        Parameters:
            client (BLEClient): BLE client to register notifications with.
            timeout (float): Timeout, in seconds, forwarded to the client's start_notify call for each subscription.
        """
        with self._lock:
            for characteristic, callback in self._active_subscriptions.values():
                try:
                    client.start_notify(
                        characteristic,
                        callback,
                        timeout=timeout,
                    )
                except (
                    BLEError,
                    BleakError,
                    TimeoutError,
                    OSError,
                    asyncio.TimeoutError,
                ) as exc:
                    logger.warning(
                        "Resubscribe of %s failed during reconnect: %s",
                        characteristic,
                        exc,
                    )
                except Exception:  # pragma: no cover - defensive logging
                    logger.exception(
                        "Unexpected error while resubscribing %s",
                        characteristic,
                    )

    def __len__(self) -> int:
        """
        Get the number of active subscriptions.
        
        Returns:
            int: Number of currently tracked subscriptions.
        """
        with self._lock:
            return len(self._active_subscriptions)

    def get_callback(self, characteristic: str) -> Optional[Callable[[Any, Any], None]]:
        """
        Return the callback currently registered for the given BLE characteristic.
        
        Parameters:
            characteristic (str): Characteristic UUID or handle used when subscribing.
        
        Returns:
            callback (Optional[Callable[[Any, Any], None]]): The registered callback, or `None` if no callback is registered for that characteristic.
        """
        with self._lock:
            return self._characteristic_index.get(characteristic)