"""BLE client wrapper"""

import asyncio
import logging
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Thread, current_thread
from typing import Optional, TYPE_CHECKING

from bleak import BleakClient as BleakRootClient

from .exceptions import BLEError
from .util import BLEErrorHandler

logger = logging.getLogger(__name__)

# BLEClient-specific constants
BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT = (
    2.0  # Ensures client.close() does not block shutdown indefinitely
)
BLECLIENT_ERROR_ASYNC_TIMEOUT = "Async operation timed out"


class BLEClient:
    """
    Client wrapper for managing BLE device connections with thread-safe async operations.

    This class provides a synchronous interface to Bleak's async operations by running
    an internal event loop in a dedicated thread. It handles the complexity of
    asyncio-to-thread synchronization while providing a simple API for BLE operations.
    """

    def __init__(
        self, address=None, *, log_if_no_address: bool = True, **kwargs
    ) -> None:
        """
        Initialize the BLEClient, creating a dedicated asyncio event loop and background thread; if `address` is provided, also create an underlying Bleak client bound to that address.
        
        Parameters:
            address (Optional[str]): BLE device address to connect to. If `None`, no underlying Bleak client is created and the instance is suitable for discovery-only operations.
            log_if_no_address (bool): If `True`, log a debug message when no `address` is provided.
            **kwargs: Forwarded to the underlying Bleak client constructor when `address` is supplied.
        """
        # Error handling infrastructure
        self.error_handler = BLEErrorHandler()
        # Share exception type with BLEInterface for consistent public API.
        self.BLEError = BLEError
        # Track service-discovery support and warning state for the underlying bleak client.
        self._service_discovery_method = None
        self._service_discovery_warning_logged = False

        # Create dedicated event loop for this client instance
        self._eventLoop = asyncio.new_event_loop()
        # Start event loop in background thread for async operations
        self._eventThread = Thread(
            target=self._run_event_loop, name="BLEClient", daemon=True
        )
        self._eventThread.start()
        self._closed = False

        if not address:
            if log_if_no_address:
                logger.debug("No address provided - only discover method will work.")
            return

        # Create underlying Bleak client for actual BLE communication
        self.bleak_client = BleakRootClient(address, **kwargs)
        get_services = getattr(self.bleak_client, "get_services", None)
        if callable(get_services):
            self._service_discovery_method = get_services
        else:
            logger.debug(
                "Underlying BleakClient %s does not expose get_services(); "
                "falling back to cached services when available.",
                getattr(self.bleak_client, "address", "unknown"),
            )

    def pair(self, **kwargs):  # pylint: disable=C0116
        """
        Initiates pairing with the remote BLE device.
        
        Parameters:
            kwargs: Backend-specific pairing options forwarded to the BLE backend.
        
        Returns:
            True if pairing succeeded, False otherwise.
        """
        return self.async_await(self.bleak_client.pair(**kwargs))

    def connect(self, *, await_timeout: Optional[float] = None, **kwargs):  # pylint: disable=C0116
        """
        Initiate a connection using the underlying Bleak client and its internal event loop.

        Args:
        ----
            await_timeout (float | None): Maximum seconds to wait for the connect operation to complete; `None` to wait indefinitely.
            **kwargs: Forwarded to the underlying Bleak client's `connect` call.

        Returns:
        -------
            The value returned by the underlying Bleak client's `connect` call.

        """
        return self.async_await(
            self.bleak_client.connect(**kwargs), timeout=await_timeout
        )

    def is_connected(self) -> bool:
        """
        Determine whether the underlying Bleak client is connected.
        
        Returns:
            True if the underlying Bleak client reports it is connected, False otherwise (also False when no Bleak client exists or the connection state cannot be read).
        """
        bleak_client = getattr(self, "bleak_client", None)
        if bleak_client is None:
            return False

        def _check_connection():
            """
            Determine whether the current `bleak_client` reports an active connection; accepts either a boolean `is_connected` attribute or a callable `is_connected()`.
            
            Returns:
                `True` if the bleak client is connected, `False` otherwise.
            """
            connected = getattr(bleak_client, "is_connected", False)
            if callable(connected):
                connected = connected()
            return bool(connected)

        return self.error_handler.safe_execute(
            _check_connection,
            default_return=False,
            error_msg="Unable to read bleak connection state",
            reraise=False,
        )

    def disconnect(self, *, await_timeout: Optional[float] = None, **kwargs):  # pylint: disable=C0116
        """
        Disconnect the underlying Bleak client and wait for completion.
        
        Parameters:
            await_timeout (float | None): Maximum seconds to wait for the disconnect to complete; if None, wait indefinitely.
            **kwargs: Additional keyword arguments passed to the underlying client's disconnect method.
        """
        self.async_await(self.bleak_client.disconnect(**kwargs), timeout=await_timeout)

    def read_gatt_char(self, *args, timeout: Optional[float] = None, **kwargs):  # pylint: disable=C0116
        """
        Read a GATT characteristic from the connected BLE device.
        
        Parameters:
            *args: Characteristic identifier (commonly a UUID string or handle).
            timeout (float | None): Maximum seconds to wait for the read to complete.
            **kwargs: Additional options passed to the underlying read operation.
        
        Returns:
            bytes: Raw bytes read from the characteristic.
        """
        return self.async_await(
            self.bleak_client.read_gatt_char(*args, **kwargs), timeout=timeout
        )

    def write_gatt_char(self, *args, timeout: Optional[float] = None, **kwargs):  # pylint: disable=C0116
        """
        Write bytes to a GATT characteristic on the connected BLE device and wait for the operation to complete.
        
        Raises:
            BLEError: If the write operation fails or times out.
        """
        self.async_await(
            self.bleak_client.write_gatt_char(*args, **kwargs), timeout=timeout
        )

    def get_services(self):
        """
        Retrieve the discovered GATT services and characteristics for the connected device.

        Returns
        -------
            The device's GATT services and their characteristics as returned by the underlying BLE library.

        """
        # services is a property, not an async method, so we access it directly
        return self.bleak_client.services

    def ensure_services_available(self):
        """
        Ensure the connected device has populated GATT services available.

        Bleak 0.22+ requires explicitly awaiting `get_services()` while some
        downstream environments still ship older bleak versions that omit that
        coroutine. This helper works with both styles by attempting the async
        discovery first and falling back to the cached `services` attribute when
        a discovery coroutine is unavailable.

        Returns:
            The device's GATT services object as provided by the underlying BLE library, or None when the backend cannot populate it.
        """
        bleak_client = getattr(self, "bleak_client", None)
        if bleak_client is None:
            logger.debug(
                "ensure_services_available called without an underlying bleak client."
            )
            return None

        discovery = self._service_discovery_method
        if callable(discovery):
            return self.async_await(discovery())

        services = getattr(bleak_client, "services", None)
        if not self._service_discovery_warning_logged:
            logger.debug(
                "Bleak backend for %s does not export get_services(); "
                "returning cached services without explicit discovery.",
                getattr(bleak_client, "address", "unknown"),
            )
            self._service_discovery_warning_logged = True
        return services

    def has_characteristic(self, specifier):
        """
        Determine whether the connected BLE device exposes the characteristic identified by `specifier`.
        
        Parameters:
            specifier (str | UUID): UUID string or UUID object identifying the characteristic to check.
        
        Returns:
            True if the characteristic is present, False otherwise.
        """
        services = getattr(self.bleak_client, "services", None)
        if not services or not getattr(services, "get_characteristic", None):
            # Use ensure_services_available to populate services
            self.error_handler.safe_execute(
                self.ensure_services_available,
                error_msg="Unable to populate services before has_characteristic",
                reraise=False,
            )
            services = getattr(self.bleak_client, "services", None)
        return bool(services and services.get_characteristic(specifier))

    def start_notify(self, *args, timeout: Optional[float] = None, **kwargs):  # pylint: disable=C0116
        """
        Subscribe to notifications for a BLE characteristic on the connected device.
        
        Parameters:
            timeout (Optional[float]): Maximum seconds to wait for the operation to complete.
        """
        self.async_await(
            self.bleak_client.start_notify(*args, **kwargs), timeout=timeout
        )

    def close(self):  # pylint: disable=C0116
        """
        Shuts down the internal asyncio event loop and background thread for this client.
        
        If the client is already closed this is a no-op. Otherwise schedules the loop to stop, then joins the event thread waiting up to BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT; if the thread remains alive after the timeout a warning is logged.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.async_run(self._stop_event_loop())
        if current_thread() is not self._eventThread:
            self._eventThread.join(timeout=BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT)
        if self._eventThread.is_alive():
            logger.warning(
                "BLE event thread did not exit within %.1fs",
                BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT,
            )

    def __enter__(self):
        """
        Support use as a context manager by returning the BLEClient instance.
        
        Returns:
            self: The BLEClient instance to be used inside the with-block.
        """
        return self

    def __exit__(self, _type, _value, _traceback):
        """
        Close the BLEClient's internal event loop and threads when exiting a context manager.

        Calls `close()` to stop the background event loop and join the event thread. Any exception information passed to the context
            manager is ignored.
        """
        self.close()

    def async_await(self, coro, timeout=None):  # pylint: disable=C0116
        """
        Wait for a coroutine scheduled on the client's event loop to complete and return its result.
        
        Parameters:
            coro: The coroutine to run on the client's internal event loop.
            timeout (float | None): Maximum seconds to wait for completion; `None` means wait indefinitely.
        
        Returns:
            The value produced by the completed coroutine.
        
        Raises:
            BLEError: If the wait times out.
        """
        # Exception mapping contract:
        #   - FutureTimeoutError -> BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT)
        #   - Bleak* exceptions propagate so BLEInterface wrappers can convert them consistently.
        future = self.async_run(coro)
        try:
            return future.result(timeout)
        except FutureTimeoutError as e:
            future.cancel()  # Clean up pending task to avoid resource leaks
            raise BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT) from e

    def async_run(self, coro) -> Future:  # pylint: disable=C0116
        """
        Schedule a coroutine on the client's internal asyncio event loop.

        Args:
        ----
            coro (coroutine): The coroutine to schedule.

        Returns:
        -------
            concurrent.futures.Future: Future representing the scheduled coroutine's eventual result.

        """
        return asyncio.run_coroutine_threadsafe(coro, self._eventLoop)

    def _run_event_loop(self):
        """
        Run the client's internal asyncio event loop in the background thread until it is stopped, then close the loop to release resources.
        """
        self.error_handler.safe_execute(
            self._eventLoop.run_forever, error_msg="Error in event loop", reraise=False
        )
        self._eventLoop.close()  # Clean up resources when loop stops

    async def _stop_event_loop(self):
        """
        Request the internal event loop to stop.
        """
        self._eventLoop.stop()
