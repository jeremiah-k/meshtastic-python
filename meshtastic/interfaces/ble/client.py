"""BLE client wrapper for managing BLE device connections with thread-safe async operations."""
import asyncio
import logging
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Thread
from typing import Optional

from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner

from .error_handler import BLEErrorHandler
from .exceptions import BLEError

# BLEClient-specific constants
BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT = (
    2.0  # Ensures client.close() does not block shutdown indefinitely
)
BLECLIENT_ERROR_ASYNC_TIMEOUT = "Async operation timed out"

logger = logging.getLogger(__name__)


class BLEClient:
    """
    Client wrapper for managing BLE device connections with thread-safe async operations.

    This class provides a synchronous interface to Bleak's async operations by running
    an internal event loop in a dedicated thread. It handles the complexity of
    asyncio-to-thread synchronization while providing a simple API for BLE operations.
    """

    # Class-level fallback so callers using __new__ still get the right exception type
    BLEError = BLEError

    def __init__(
        self, address=None, *, log_if_no_address: bool = True, **kwargs
    ) -> None:
        """
        Create a BLEClient that runs its own asyncio event loop in a background thread and, if an address is provided, attaches a Bleak client for device communication.
        
        Parameters:
            address (Optional[str]): BLE device address to bind an underlying Bleak client to. If `None`, no Bleak client is created and the instance is limited to discovery-only operations.
            log_if_no_address (bool): When `True` (default), log a debug message if `address` is not provided.
            **kwargs: Forwarded to the underlying Bleak client constructor when `address` is provided.
        """
        # Error handling infrastructure
        self.error_handler = BLEErrorHandler()
        # Share exception type with BLEInterface for consistent public API.
        self.BLEError = BLEError

        # Create dedicated event loop for this client instance
        self._eventLoop = asyncio.new_event_loop()
        # Start event loop in background thread for async operations
        self._eventThread = Thread(
            target=self._run_event_loop, name="BLEClient", daemon=True
        )
        self._eventThread.start()
        self._loop_closed = False

        if not address:
            if log_if_no_address:
                logger.debug("No address provided - only discover method will work.")
            return

        # Create underlying Bleak client for actual BLE communication
        self.bleak_client = BleakRootClient(address, **kwargs)

    def _require_bleak_client(self) -> BleakRootClient:
        """
        Return the underlying Bleak client for this BLEClient instance.
        
        Returns:
            bleak_client (BleakRootClient): The underlying Bleak client.
        
        Raises:
            BLEError: If the instance was created without an address and no Bleak client exists.
        """
        bleak_client = getattr(self, "bleak_client", None)
        if bleak_client is None:
            raise self.BLEError(
                "BLEClient was created without an address; this operation "
                "requires an underlying BLE client"
            )
        return bleak_client

    def discover(self, **kwargs):  # pylint: disable=C0116
        """
        Locate nearby Bluetooth Low Energy devices using BleakScanner.
        
        Keyword arguments are forwarded to BleakScanner.discover (for example, `timeout` or `adapter`).
        
        Returns:
            devices (list): A list of discovered `BLEDevice` objects.
        """
        return self.async_await(BleakScanner.discover(**kwargs))

    def pair(self, **kwargs):  # pylint: disable=C0116
        """
        Initiates pairing with the connected BLE device.
        
        Parameters:
            kwargs: Backend-specific pairing options to forward to the underlying BLE client.
        
        Returns:
            `True` if pairing succeeded, `False` otherwise.
        """
        bleak_client = self._require_bleak_client()
        return self.async_await(bleak_client.pair(**kwargs))

    def connect(
        self, *, await_timeout: Optional[float] = None, **kwargs
    ):  # pylint: disable=C0116
        """
        Connect to the BLE device using the instance's underlying Bleak client.
        
        Parameters:
            await_timeout (float | None): Maximum seconds to wait for the connect operation to complete; `None` to wait indefinitely.
            **kwargs: Forwarded to the underlying client's `connect` call.
        
        Returns:
            The result returned by the underlying client's `connect` operation (typically a connection success indicator).
        """
        bleak_client = self._require_bleak_client()
        return self.async_await(
            bleak_client.connect(**kwargs), timeout=await_timeout
        )

    def is_connected(self) -> bool:
        """
        Return whether the underlying Bleak client reports an active connection.
        
        Returns:
            bool: `true` if the underlying Bleak client reports it is connected, `false` otherwise (also `false` when no Bleak client exists or the connection state cannot be read).
        """
        bleak_client = getattr(self, "bleak_client", None)
        if bleak_client is None:
            return False

        def _check_connection():
            """
            Determine whether the current Bleak client reports an active connection.
            
            Handles both a boolean `is_connected` attribute and a callable `is_connected()` method on the `bleak_client`.
            
            Returns:
                bool: `True` if the bleak client reports an active connection, `False` otherwise.
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

    def disconnect(
        self, *, await_timeout: Optional[float] = None, **kwargs
    ):  # pylint: disable=C0116
        """
        Disconnects the underlying Bleak client and waits for the operation to complete.
        
        Parameters:
            await_timeout (float | None): Maximum seconds to wait for disconnect completion. If None, wait indefinitely.
            **kwargs: Additional keyword arguments forwarded to the underlying Bleak client's disconnect method.
        """
        bleak_client = self._require_bleak_client()
        self.async_await(bleak_client.disconnect(**kwargs), timeout=await_timeout)

    def read_gatt_char(
        self, *args, timeout: Optional[float] = None, **kwargs
    ):  # pylint: disable=C0116
        """
        Read a GATT characteristic from the connected BLE device.
        
        Parameters:
            *args: Positional arguments forwarded to the Bleak client's `read_gatt_char` (typically the characteristic UUID or handle).
            timeout (float | None): Maximum seconds to wait for the read to complete; `None` means no per-call timeout.
            **kwargs: Keyword arguments forwarded to the Bleak client's `read_gatt_char`.
        
        Returns:
            bytes: Raw bytes read from the characteristic.
        """
        bleak_client = self._require_bleak_client()
        return self.async_await(
            bleak_client.read_gatt_char(*args, **kwargs), timeout=timeout
        )

    def write_gatt_char(
        self, *args, timeout: Optional[float] = None, **kwargs
    ):  # pylint: disable=C0116
        """
        Write bytes to a GATT characteristic on the connected BLE device and wait for the operation to complete.
        
        Parameters:
            timeout (Optional[float]): Maximum time in seconds to wait for the write to finish; `None` to use the default.
        
        Raises:
            BLEError: If the write operation fails or times out.
        """
        bleak_client = self._require_bleak_client()
        self.async_await(
            bleak_client.write_gatt_char(*args, **kwargs), timeout=timeout
        )

    def get_services(self):
        """
        Get the discovered GATT services and characteristics for the connected device.
        
        Returns:
            The underlying Bleak client's services object representing discovered GATT services and their characteristics.
        """
        bleak_client = self._require_bleak_client()
        # services is a property, not an async method, so we access it directly
        return bleak_client.services

    def has_characteristic(self, specifier):
        """
        Determine whether the connected BLE device exposes the characteristic identified by `specifier`.
        
        Parameters:
            specifier (str | UUID): UUID string or UUID object identifying the characteristic to check.
        
        Returns:
            bool: True if the characteristic is present, False otherwise.
        """
        bleak_client = self._require_bleak_client()
        services = getattr(bleak_client, "services", None)
        if not services or not getattr(services, "get_characteristic", None):
            # Lambda is appropriate here for deferred execution in error handling
            self.error_handler.safe_execute(
                lambda: self.get_services(),
                error_msg="Unable to populate services before has_characteristic",
                reraise=False,
            )
            services = getattr(bleak_client, "services", None)
        return bool(services and services.get_characteristic(specifier))

    def start_notify(
        self, *args, timeout: Optional[float] = None, **kwargs
    ):  # pylint: disable=C0116
        """
        Subscribe to notifications for a BLE characteristic on the connected device.
        
        Parameters:
            *args: Positional arguments forwarded to the BLE backend's start_notify call (typically characteristic identifier and a notification callback).
            timeout (Optional[float]): Maximum time in seconds to wait for the operation.
            **kwargs: Keyword arguments forwarded to the BLE backend's start_notify call.
        """
        bleak_client = self._require_bleak_client()
        self.async_await(
            bleak_client.start_notify(*args, **kwargs), timeout=timeout
        )

    def close(self):  # pylint: disable=C0116
        """
        Shuts down the client's asyncio event loop and its background thread.

        Signals the internal event loop to stop, waits up to BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT for the thread to exit,
        and logs a warning if the thread does not terminate within that timeout.
        """
        event_loop = getattr(self, "_eventLoop", None)
        if event_loop is None or event_loop.is_closed():
            return

        self.async_run(self._stop_event_loop())
        thread = getattr(self, "_eventThread", None)
        if thread:
            thread.join(timeout=BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT)
            if thread.is_alive():
                logger.warning(
                    "BLE event thread did not exit within %.1fs",
                    BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT,
                )
        self._loop_closed = True
        self._eventLoop = None
        self._eventThread = None

    def __enter__(self):
        """
        Enter the context manager for the BLEClient.
        
        Returns:
            self: the BLEClient instance to be used within the with-block.
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
        Wait for a coroutine to complete on the client's internal event loop and return its result.
        
        If the coroutine does not finish within `timeout` seconds, the pending task is cancelled and a `BLEError` is raised.
        
        Parameters:
            coro: The coroutine to schedule and wait for.
            timeout (float | None): Maximum seconds to wait for completion; `None` means wait indefinitely.
        
        Returns:
            The value produced by the completed coroutine.
        
        Raises:
            BLEError: If the wait times out.
        """
        # Exception mapping contract:
        #   - FutureTimeoutError -> BLEInterface.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT)
        #   - Bleak* exceptions propagate so BLEInterface wrappers can convert them consistently.
        future = self.async_run(coro)
        try:
            return future.result(timeout)
        except FutureTimeoutError as e:
            try:
                future.cancel()  # Clean up pending task to avoid resource leaks
            except RuntimeError:
                # Event loop may already be closed; in that case cancellation is unnecessary.
                pass
            raise self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT) from e

    def async_run(self, coro):  # pylint: disable=C0116
        """
        Schedule a coroutine to run on the client's internal asyncio event loop.
        
        Parameters:
            coro (coroutine): Coroutine to schedule on the internal event loop.
        
        Returns:
            concurrent.futures.Future: Future that will contain the coroutine's result once completed.
        """
        event_loop = getattr(self, "_eventLoop", None)
        if (
            event_loop is None
            or getattr(self, "_loop_closed", False)
            or event_loop.is_closed()
        ):
            raise self.BLEError("BLEClient has been closed")
        return asyncio.run_coroutine_threadsafe(coro, event_loop)

    def _run_event_loop(self):
        """
        Run the client's internal asyncio event loop until it is stopped, then close it.
        
        This method executes the dedicated event loop in the background thread and ensures the loop is closed when finished. Exceptions raised while running the loop are handled by the instance's error handler.
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
