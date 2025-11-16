"""BLE client wrapper used by the BLE interface."""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Thread
from typing import Optional

from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner

from .config import (
    BLECLIENT_ERROR_ASYNC_TIMEOUT,
    BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT,
    logger,
)
from .errors import BLEError, BLEErrorHandler


class BLEClient:
    """
    Client wrapper for managing BLE device connections with thread-safe async operations.

    This class provides a synchronous interface to Bleak's async operations by running
    an internal event loop in a dedicated thread. It handles the complexity of
    asyncio-to-thread synchronization while providing a simple API for BLE operations.
    """

    BLEError = BLEError

    def __init__(self, address=None, *, log_if_no_address: bool = True, **kwargs) -> None:
        """
        Create a BLEClient with a dedicated asyncio event loop and, if an address is provided, an underlying Bleak client attached to that address.
        """
        self.error_handler = BLEErrorHandler()
        self.BLEError = BLEError

        self._eventLoop = asyncio.new_event_loop()
        self._eventThread = Thread(target=self._run_event_loop, name="BLEClient", daemon=True)
        self._eventThread.start()

        if not address:
            if log_if_no_address:
                logger.debug("No address provided - only discover method will work.")
            return

        self.bleak_client = BleakRootClient(address, **kwargs)

    def discover(self, **kwargs):
        """Discover nearby BLE devices using BleakScanner."""
        return self.async_await(BleakScanner.discover(**kwargs))

    def pair(self, **kwargs):
        """Pair the underlying BLE client with the remote device."""
        return self.async_await(self.bleak_client.pair(**kwargs))

    def connect(self, *, await_timeout: Optional[float] = None, **kwargs):
        """Initiate a connection using the underlying Bleak client."""
        return self.async_await(
            self.bleak_client.connect(**kwargs), timeout=await_timeout
        )

    def is_connected(self) -> bool:
        """Determine if the underlying Bleak client is currently connected."""
        bleak_client = getattr(self, "bleak_client", None)
        if bleak_client is None:
            return False

        def _check_connection():
            try:
                connected = getattr(bleak_client, "is_connected", False)
                if callable(connected):
                    connected = connected()
                return bool(connected)
            except Exception:
                logger.debug("Unable to read bleak connection state")
                return False

        return self.error_handler.safe_execute(
            _check_connection,
            default_return=False,
            error_msg="Error checking BLE connection state",
        )

    def get_services(self):
        """Retrieve the last cached set of services from the underlying Bleak client."""
        return self.bleak_client.services

    def has_characteristic(self, specifier):
        """Return True if the characteristic is present."""
        services = getattr(self.bleak_client, "services", None)
        if not services or not getattr(services, "get_characteristic", None):
            self.error_handler.safe_execute(
                lambda: self.get_services(),
                error_msg="Unable to populate services before has_characteristic",
                reraise=False,
            )
            services = getattr(self.bleak_client, "services", None)
        return bool(services and services.get_characteristic(specifier))

    def start_notify(self, *args, timeout: Optional[float] = None, **kwargs):
        """Subscribe to notifications for a BLE characteristic on the connected device."""
        self.async_await(
            self.bleak_client.start_notify(*args, **kwargs), timeout=timeout
        )

    def disconnect(self, *, await_timeout: Optional[float] = None, **kwargs):
        """Disconnect the underlying Bleak client and wait for completion."""
        self.async_await(
            self.bleak_client.disconnect(**kwargs), timeout=await_timeout
        )

    def read_gatt_char(self, *args, timeout: Optional[float] = None, **kwargs):
        """Read a GATT characteristic from the connected BLE device."""
        return self.async_await(
            self.bleak_client.read_gatt_char(*args, **kwargs), timeout=timeout
        )

    def write_gatt_char(self, *args, timeout: Optional[float] = None, **kwargs):
        """Write bytes to a GATT characteristic on the connected BLE device."""
        self.async_await(
            self.bleak_client.write_gatt_char(*args, **kwargs), timeout=timeout
        )

    def close(self):
        """Shut down the client's asyncio event loop and its background thread."""
        self.async_run(self._stop_event_loop())
        self._eventThread.join(timeout=BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT)
        if self._eventThread.is_alive():
            logger.warning(
                "BLE event thread did not exit within %.1fs",
                BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT,
            )

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def async_await(self, coro, timeout=None):
        """Wait for the given coroutine to complete on the client's event loop."""
        future = self.async_run(coro)
        try:
            return future.result(timeout)
        except FutureTimeoutError as e:
            future.cancel()
            raise self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT) from e

    def async_run(self, coro):
        """Schedule a coroutine on the client's internal asyncio event loop."""
        return asyncio.run_coroutine_threadsafe(coro, self._eventLoop)

    def _run_event_loop(self):
        self.error_handler.safe_execute(
            self._eventLoop.run_forever, error_msg="Error in event loop", reraise=False
        )
        self._eventLoop.close()

    async def _stop_event_loop(self):
        self._eventLoop.stop()


__all__ = ["BLEClient"]
