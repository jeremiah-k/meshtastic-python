"""BLE client management and async operations."""

import asyncio
import logging
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Thread
from typing import Any, Optional, Type

from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner, BLEDevice
from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.coordination import ThreadCoordinator
from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_ASYNC_TIMEOUT,
    BLEConfig,
    ERROR_TIMEOUT,
    logger,
)
from meshtastic.interfaces.ble.errors import BLEErrorHandler

class BLEClient:
    """
    Client wrapper for managing BLE device connections with thread-safe async operations.

    This class provides a synchronous interface to Bleak's async operations by running
    an internal event loop in a dedicated thread. It handles the complexity of
    asyncio-to-thread synchronization while providing a simple API for BLE operations.
    """

    # Class-level fallback so callers using __new__ still get the right exception type
    class BLEError(Exception):
        """An exception class for BLE errors in the client."""
        pass

    @staticmethod
    def _sanitize_address(address: Optional[str]) -> Optional[str]:
        """
        Normalize a BLE address or identifier by stripping separators and lowercasing.

        Args:
        ----
            address (Optional[str]): Address or device name; may be None or only whitespace.

        Returns:
        -------
            Optional[str]: Normalized value with common separators removed and lowercased,
                or None if the input is empty/whitespace.

        """
        if address is None or not address.strip():
            return None
        return (
            address.strip()
            .replace("-", "")
            .replace("_", "")
            .replace(":", "")
            .replace(" ", "")
            .lower()
        )

    @staticmethod
    async def _with_timeout(awaitable, timeout: Optional[float], label: str):
        """
        Await `awaitable`, enforcing `timeout` seconds if provided.

        Raises
        ------
            BLEClient.BLEError: when the awaitable does not finish before the timeout elapses.

        """
        if timeout is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BLEClient.BLEError(ERROR_TIMEOUT.format(label, timeout)) from exc

    def __init__(
        self, address=None, *, log_if_no_address: bool = True, **kwargs
    ) -> None:
        """
        Create a BLEClient with a dedicated asyncio event loop and, if an address is provided, an underlying Bleak client attached to that address.

        Args:
        ----
            address (Optional[str]): BLE device address to attach a Bleak client to. If None, the instance is created for
                discovery-only use and will not instantiate an underlying Bleak client.
            **kwargs: Keyword arguments forwarded to the underlying Bleak client constructor when `address` is provided.

        """
        # Error handling infrastructure
        self.error_handler = BLEErrorHandler()
        # Share exception type with BLEInterface for consistent public API.
        self.BLEError: Type[BLEClient.BLEError] = BLEClient.BLEError  # type: ignore[misc]

        # Create dedicated event loop for this client instance
        self._eventLoop = asyncio.new_event_loop()
        # Start event loop in background thread for async operations
        self._eventThread = Thread(
            target=self._run_event_loop, name="BLEClient", daemon=True
        )
        try:
            self._eventThread.start()
        except RuntimeError:
            self._eventLoop.close()
            raise

        if not address:
            if log_if_no_address:
                logger.debug("No address provided - only discover method will work.")
            return

        # Create underlying Bleak client for actual BLE communication
        self.bleak_client = BleakRootClient(address, **kwargs)

    def discover(self, **kwargs):  # pylint: disable=C0116
        """
        Discover nearby BLE devices using BleakScanner.

        Keyword arguments are forwarded to BleakScanner.discover (for example, `timeout` or `adapter`).

        Returns
        -------
            A list of discovered BLEDevice objects.

        """
        return self.async_await(BleakScanner.discover(**kwargs))

    def pair(self, **kwargs):  # pylint: disable=C0116
        """
        Pair the underlying BLE client with the remote device.

        Args:
        ----
            kwargs: Backend-specific pairing options forwarded to the underlying BLE client.

        Returns:
        -------
            `True` if pairing succeeded, `False` otherwise.

        """
        return self.async_await(self.bleak_client.pair(**kwargs))

    def connect(
        self, *, await_timeout: Optional[float] = None, **kwargs
    ):  # pylint: disable=C0116
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
        Determine if the underlying Bleak client is currently connected.

        Returns
        -------
            `true` if the underlying Bleak client reports it is connected, `false` otherwise (also `false` when no
                Bleak client exists or the connection state cannot be read).

        """
        bleak_client = getattr(self, "bleak_client", None)
        if bleak_client is None:
            return False

        def _check_connection():
            """
            Determine whether the current `bleak_client` reports an active connection.

            This handles either a boolean `is_connected` attribute or a callable `is_connected()` method on `bleak_client`.

            Returns
            -------
                bool: `True` if the bleak client is connected, `False` otherwise.

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
        Disconnect the underlying Bleak client and wait for the operation to finish.

        Args:
        ----
            await_timeout (float | None): Maximum seconds to wait for disconnect completion; if None, wait indefinitely.
            **kwargs: Additional keyword arguments forwarded to the Bleak client's disconnect method.

        """
        self.async_await(self.bleak_client.disconnect(**kwargs), timeout=await_timeout)

    def read_gatt_char(
        self, *args, timeout: Optional[float] = None, **kwargs
    ):  # pylint: disable=C0116
        """Read a GATT characteristic from the connected BLE device.

        Forwards all arguments to the underlying Bleak client's `read_gatt_char`.

        Args:
        ----
            *args: Positional arguments forwarded to `read_gatt_char` (typically the characteristic UUID or handle).
            timeout (float | None): Maximum seconds to wait for the read to complete.
            **kwargs: Keyword arguments forwarded to `read_gatt_char`.

        Returns:
        -------
            bytes: The raw bytes read from the characteristic.

        """
        return self.async_await(
            self.bleak_client.read_gatt_char(*args, **kwargs), timeout=timeout
        )

    def write_gatt_char(
        self, *args, timeout: Optional[float] = None, **kwargs
    ):  # pylint: disable=C0116
        """
        Write the given bytes to a GATT characteristic on the connected BLE device and wait for completion.

        Raises
        ------
            BLEClient.BLEError: If the write operation fails or times out.

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

    def has_characteristic(self, specifier):
        """
        Check whether the connected BLE device exposes the characteristic identified by `specifier`.

        Args:
        ----
            specifier (str | UUID): UUID string or UUID object identifying the characteristic to check.

        Returns:
        -------
            `true` if the characteristic is present, `false` otherwise.

        Note
        ----
            If services are not yet populated, this method will attempt to fetch them.

        """
        services = getattr(self.bleak_client, "services", None)
        if not services or not getattr(services, "get_characteristic", None):
            # Lambda is appropriate here for deferred execution in error handling
            self.error_handler.safe_execute(
                lambda: self.get_services(),
                error_msg="Unable to populate services before has_characteristic",
                reraise=False,
            )
            services = getattr(self.bleak_client, "services", None)
        return bool(services and services.get_characteristic(specifier))

    def start_notify(
        self, *args, timeout: Optional[float] = None, **kwargs
    ):  # pylint: disable=C0116
        """
        Subscribe to notifications for a BLE characteristic on the connected device.

        Args:
        ----
            *args: Additional arguments forwarded to the BLE backend's notification start call.
            timeout (Optional[float]): Timeout for the operation.
            **kwargs: Additional keyword arguments forwarded to the BLE backend's notification start call.

        """
        self.async_await(
            self.bleak_client.start_notify(*args, **kwargs), timeout=timeout
        )

    def close(self):  # pylint: disable=C0116
        """
        Shuts down the client's asyncio event loop and its background thread.

        Signals the internal event loop to stop, waits up to BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT for the thread to exit,
        and logs a warning if the thread does not terminate within that timeout.
        """
        self.async_run(self._stop_event_loop())
        self._eventThread.join(timeout=BLEConfig.BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT)
        if self._eventThread.is_alive():
            logger.warning(
                "BLE event thread did not exit within %.1fs",
                BLEConfig.BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT,
            )

    def __enter__(self):
        """
        Enter the context manager and provide the BLEClient instance for use within the with-block.

        Returns
        -------
            self: The BLEClient instance.

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
        Wait for the given coroutine to complete on the client's event loop and return its result.

        If the coroutine does not finish within `timeout` seconds the pending task is cancelled and a BLEClient.BLEError is raised.

        Args:
        ----
            coro: The coroutine to run on the client's internal event loop.
            timeout (float | None): Maximum seconds to wait for completion; `None` means wait indefinitely.

        Returns:
        -------
            The value produced by the completed coroutine.

        Raises:
        ------
            BLEClient.BLEError: If the wait times out.

        """
        # Exception mapping contract:
        #   - FutureTimeoutError -> BLEInterface.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT)
        #   - Bleak* exceptions propagate so BLEInterface wrappers can convert them consistently.
        future = self.async_run(coro)
        try:
            return future.result(timeout)
        except (FutureTimeoutError, RuntimeError) as e:
            try:
                future.cancel()  # Clean up pending task to avoid resource leaks
            except Exception:  # pragma: no cover - defensive
                logger.debug("Failed to cancel BLE future after timeout/loop-close", exc_info=True)
            # Consume any late exceptions to avoid "Task exception was never retrieved"
            future.add_done_callback(
                lambda f: f.exception()
                if not f.cancelled()
                else None  # pragma: no cover - best effort suppression
            )
            raise self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT) from e

    def async_run(self, coro):  # pylint: disable=C0116
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
        """Run the event loop in the dedicated thread until stopped."""
        self.error_handler.safe_execute(
            self._eventLoop.run_forever, error_msg="Error in event loop", reraise=False
        )
        self._eventLoop.close()  # Clean up resources when loop stops

    async def _stop_event_loop(self):
        """
        Request the internal event loop to stop.
        """
        self._eventLoop.stop()
