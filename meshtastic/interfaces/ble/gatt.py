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
        self._active_subscriptions: Dict[
            int, Tuple[str, Callable[[Any, Any], None]]
        ] = {}
        self._subscription_counter = 0
        self._lock = RLock()

    def subscribe(self, characteristic: str, callback) -> int:
        """
        Track a subscription for later cleanup or resubscription.
        """
        with self._lock:
            token = self._subscription_counter
            self._subscription_counter += 1
            self._active_subscriptions[token] = (characteristic, callback)
            return token

    def cleanup_all(self) -> None:
        """
        Forget all tracked subscriptions.
        """
        with self._lock:
            self._active_subscriptions.clear()

    def resubscribe_all(self, client: "BLEClient", *, timeout: float) -> None:
        """
        Re-register every tracked subscription on the provided client.
        """
        with self._lock:
            for characteristic, callback in self._active_subscriptions.values():
                try:
                    client.start_notify(
                        characteristic,
                        callback,
                        timeout=timeout,
                    )
                except (BLEError, BleakError, TimeoutError, OSError, asyncio.TimeoutError) as exc:
                    logger.warning(
                        "Resubscribe of %s failed during reconnect: %s",
                        characteristic,
                        exc,
                    )
                except Exception as exc:  # pragma: no cover - defensive logging
                    logger.exception(
                        "Unexpected error while resubscribing %s: %s",
                        characteristic,
                        exc,
                    )

    def __len__(self) -> int:
        with self._lock:
            return len(self._active_subscriptions)

    def get_callback(self, characteristic: str) -> Optional[Callable[[Any, Any], None]]:
        """
        Fetch the callback registered for a given characteristic if present.
        """
        with self._lock:
            for registered_char, callback in self._active_subscriptions.values():
                if registered_char == characteristic:
                    return callback
        return None
