"""BLE client management and async operations."""

import asyncio
import contextlib
import sys
import threading
import weakref
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Awaitable, Coroutine, Optional, Union
from uuid import UUID

from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner

from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_ASYNC_TIMEOUT,
    ERROR_TIMEOUT,
    logger,
)
from meshtastic.interfaces.ble.errors import BLEErrorHandler
from meshtastic.interfaces.ble.runner import BLECoroutineRunner
from meshtastic.interfaces.ble.utils import sanitize_address


class BLEClient:
    """
    Client wrapper for managing BLE device connections with thread-safe async operations.

    This class provides a synchronous interface to Bleak's async operations by using
    a shared singleton event loop (BLECoroutineRunner) for all BLE operations. This
    approach:

    - Reduces resource usage by sharing one event loop thread across all clients
    - Eliminates per-instance thread/loop creation overhead
    - Simplifies cleanup and prevents "zombie thread" accumulation
    - Provides consistent async operation handling

    Thread Safety:
        All methods are thread-safe. The underlying singleton runner manages all
        async-to-thread synchronization.
    """

    # Class-level fallback so callers using __new__ still get the right exception type
    class BLEError(Exception):
        """An exception class for BLE errors in the client."""

    @staticmethod
    def _sanitize_address(address: Optional[str]) -> Optional[str]:
        """
        Normalize a BLE address or identifier by removing common separators and lowercasing.

        Parameters
        ----------
        address : Any
            Address or identifier to normalize; may be None or consist only of whitespace.

        Returns
        -------
            The normalized address with dashes, underscores, colons, and spaces removed and converted to lowercase, or None if the input is None or only whitespace.
        """
        return sanitize_address(address)

    @staticmethod
    async def _with_timeout(awaitable, timeout: Optional[float], label: str):
        """
        Await an awaitable, applying an optional timeout.

        Parameters
        ----------
        awaitable : Any
            An awaitable to execute.
        timeout : Any
            Maximum seconds to wait; if None, wait indefinitely.
        label : Any
            Short description used in the timeout error message.

        Returns
        -------
            The result returned by the awaitable.

        Raises
        ------
            BLEClient.BLEError: If the awaitable does not complete before the timeout elapses.
        """
        if timeout is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BLEClient.BLEError(ERROR_TIMEOUT.format(label, timeout)) from exc

    def __init__(
        self, address: Optional[str] = None, *, log_if_no_address: bool = True, **kwargs
    ) -> None:
        """
        Initialize the BLEClient using the singleton BLECoroutineRunner.

        Parameters
        ----------
        address : Optional[str]
            BLE device address to attach a Bleak client to. If None, no Bleak client
            is created and the instance operates in discovery-only mode.
        log_if_no_address : bool
            If True and `address` is None, emit a debug message indicating
            discovery-only mode.
        **kwargs : dict
            Keyword arguments forwarded to the underlying Bleak client constructor
            when `address` is provided.

        Side Effects:
            - Obtains the singleton BLECoroutineRunner instance (creating it if needed)
            - Creates an error handler and exposes the BLEError exception type
            - Instantiates a Bleak client bound to `address` when `address` is provided
        """
        # Error handling infrastructure
        self.error_handler = BLEErrorHandler()

        self.bleak_client: Optional[BleakRootClient] = None
        self.address = address
        self._closed = False
        self._pending_futures: weakref.WeakSet[Future] = weakref.WeakSet()

        # Use singleton runner for all BLE operations
        self._runner = BLECoroutineRunner()

        if not address:
            if log_if_no_address:
                logger.debug("No address provided - only discover method will work.")
            # Discovery-only instances won't have a connected bleak_client
            self.bleak_client = None
            return

        # Create underlying Bleak client for actual BLE communication
        self.bleak_client = BleakRootClient(address, **kwargs)

    def discover(self, **kwargs):  # pylint: disable=C0116
        """
        Discover nearby BLE devices.

        Keyword arguments are forwarded to BleakScanner.discover (for example, `timeout` or `adapter`).

        Parameters
        ----------
        **kwargs : dict
            Keyword arguments forwarded to BleakScanner.discover (e.g., `timeout` or `adapter`).

        Returns
        -------
            A list of discovered Bleak `BLEDevice` objects.
        """
        return self.async_await(BleakScanner.discover(**kwargs))

    def pair(self, **kwargs):  # pylint: disable=C0116
        """
        Pair the underlying BLE client with the remote device.

        Parameters
        ----------
        **kwargs : dict
            Backend-specific pairing options forwarded to the underlying BLE client.

        Returns
        -------
            `True` if pairing succeeded, `False` otherwise.

        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot pair: BLE client not initialized")
        return self.async_await(self.bleak_client.pair(**kwargs))

    def connect(self, *, await_timeout: Optional[float] = None, **kwargs):  # pylint: disable=C0116
        """
        Establish a connection to the remote BLE device using the underlying Bleak client.

        Parameters
        ----------
        await_timeout : Any
            | None Maximum seconds to wait for the connect operation to complete; `None` to wait indefinitely.
        **kwargs : dict
            Forwarded to the underlying Bleak client's `connect` call.

        Returns
        -------
            The value returned by the underlying Bleak client's `connect` call.
        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot connect: BLE client not initialized")
        return self.async_await(
            self.bleak_client.connect(**kwargs), timeout=await_timeout
        )

    def is_connected(self) -> bool:
        """
        Determine whether the underlying Bleak client is currently connected.

        Returns
        -------
            `True` if the underlying Bleak client reports it is connected; `False` otherwise (also `False` when no Bleak client exists or the connection state cannot be read).
        """
        bleak_client = getattr(self, "bleak_client", None)
        if bleak_client is None:
            return False

        def _check_connection():
            """
                Check whether the current `bleak_client` reports an active connection.

                This accepts either a boolean `is_connected` attribute or a callable `is_connected()` method on the `bleak_client` and returns the interpreted boolean result.

                Returns
                -------
            bool: `True` if the bleak client reports an active connection, `False` otherwise.
            """
            connected = getattr(bleak_client, "is_connected", False)
            if callable(connected):
                connected = connected()  # pylint: disable=E1102
            return bool(connected)

        result = self.error_handler.safe_execute(
            _check_connection,
            default_return=False,
            error_msg="Unable to read bleak connection state",
            reraise=False,
        )
        return bool(result)  # type: ignore[arg-type]

    def disconnect(self, *, await_timeout: Optional[float] = None, **kwargs):  # pylint: disable=C0116
        """
        Disconnect from the remote BLE device and wait for completion.

        Parameters
        ----------
        await_timeout : Any
            | None Maximum seconds to wait for disconnect to complete; if None, wait indefinitely.
        **kwargs : dict
            Additional keyword arguments forwarded to the underlying Bleak client's `disconnect` method.
        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot disconnect: BLE client not initialized")
        self.async_await(self.bleak_client.disconnect(**kwargs), timeout=await_timeout)

    def read_gatt_char(self, *args, timeout: Optional[float] = None, **kwargs):  # pylint: disable=C0116
        """
        Read a GATT characteristic from the connected BLE device.

        Parameters
        ----------
        *args : tuple
            Positional arguments identifying the characteristic (typically a UUID string or handle).
        timeout : Any
            | None Maximum seconds to wait for the read to complete; if None, no timeout is applied.
        **kwargs : dict
            Additional keyword arguments passed to the read operation.

        Returns
        -------
        bytes: Raw bytes read from the characteristic.
        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot read: BLE client not initialized")
        return self.async_await(
            self.bleak_client.read_gatt_char(*args, **kwargs), timeout=timeout
        )

    def write_gatt_char(self, *args, timeout: Optional[float] = None, **kwargs):  # pylint: disable=C0116
        """
        Write bytes to a GATT characteristic on the connected device and wait for completion.

        Parameters
        ----------
        *args : tuple
            Positional arguments identifying the characteristic and payload (typically UUID and data bytes).
        timeout : Any
            Maximum seconds to wait for the write to complete; None for no timeout.
        **kwargs : dict
            Additional keyword arguments passed to the write operation.

        Raises
        ------
            BLEClient.BLEError: If the write operation fails or the wait times out.
        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot write: BLE client not initialized")
        self.async_await(
            self.bleak_client.write_gatt_char(*args, **kwargs), timeout=timeout
        )

    def get_services(self, **kwargs):
        """
        Actively retrieve the client's discovered GATT services and characteristics.

        Parameters
        ----------
        **kwargs : dict
            Keyword arguments forwarded to the underlying BLE client's `get_services` call.

        Returns
        -------
            The services collection object provided by the underlying BLE client, containing discovered GATT services and their characteristics.
        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot get services: BLE client not initialized")
        return self.async_await(self.bleak_client.get_services(**kwargs))  # type: ignore[attr-defined]

    def has_characteristic(self, specifier: Union[str, UUID]):  # pylint: disable=C0116
        """
        Determine whether the connected device exposes the GATT characteristic identified by `specifier`.

        Parameters
        ----------
        specifier : Any
            | UUID UUID string or UUID object identifying the characteristic to check. If services are not yet discovered, this method will attempt to populate them before checking.

        Returns
        -------
            `True` if the characteristic is present, `False` otherwise.
        """
        if self.bleak_client is None:
            return False

        services = getattr(self.bleak_client, "services", None)
        if not services or not getattr(services, "get_characteristic", None):
            services = self.error_handler.safe_execute(
                lambda: self.get_services(),
                error_msg="Unable to populate services before has_characteristic",
                reraise=False,
            )
            if not services:
                services = getattr(self.bleak_client, "services", None)
        return bool(services and services.get_characteristic(specifier))

    def start_notify(self, *args, timeout: Optional[float] = None, **kwargs):  # pylint: disable=C0116
        """
        Subscribe to notifications for a BLE characteristic on the connected device.

        Parameters
        ----------
        *args : tuple
            Positional arguments forwarded to the BLE backend's `start_notify` call (e.g., characteristic UUID and callback).
        timeout : Any
            Maximum seconds to wait for the operation; if None, no timeout is applied.
        **kwargs : dict
            Keyword arguments forwarded to the BLE backend's `start_notify` call.
        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot start notify: BLE client not initialized")
        self.async_await(
            self.bleak_client.start_notify(*args, **kwargs), timeout=timeout
        )

    def close(self):  # pylint: disable=C0116
        """
        Close the BLEClient and cancel any pending operations.

        Since the event loop is now managed by a singleton runner, this method
        primarily cancels pending futures for this instance. The shared event
        loop continues running for other clients.

        This method is idempotent - calling it multiple times has no additional
        effect after the first call.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True

        # Cancel any pending futures to unblock waiting threads immediately
        if hasattr(self, "_pending_futures"):
            for future in list(self._pending_futures):
                if not future.done():
                    future.cancel()

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
        Ensure the BLEClient is closed when exiting a context manager.

        The exception information passed to the context manager is ignored; this method does not suppress exceptions.
        """
        self.close()

    def async_await(self, coro: "Awaitable", timeout: Optional[float] = None):  # pylint: disable=C0116
        """
        Wait for the given coroutine to complete and return its result.

        If the coroutine does not finish within `timeout` seconds the pending
        task is cancelled and a BLEClient.BLEError is raised.

        Parameters
        ----------
        coro : Awaitable
            The coroutine to run on the shared BLE event loop.
        timeout : Optional[float]
            Maximum seconds to wait for completion; `None` means wait indefinitely.

        Returns
        -------
        Any
            The value produced by the completed coroutine.

        Raises
        ------
        BLEClient.BLEError
            If the wait times out or the client is closed.
        """
        # Check if client is closed before scheduling work
        if self._closed:
            raise self.BLEError("Cannot schedule operation: BLE client is closed")

        # Exception mapping contract:
        #   - FutureTimeoutError -> self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT)
        #   - Bleak* exceptions propagate so interface wrappers can convert them consistently.
        future = self.async_run(coro)  # type: ignore[arg-type]
        if hasattr(self, "_pending_futures"):
            self._pending_futures.add(future)
        try:
            # On macOS, CoreBluetooth requires occasional I/O operations for
            # callbacks to be properly delivered. Without debug logging, no I/O
            # was happening, causing callbacks to never be processed.
            with contextlib.suppress(ValueError, OSError):
                sys.stdout.flush()
            return future.result(timeout)
        except SystemExit:  # pylint: disable=W0706
            raise
        except KeyboardInterrupt:  # pylint: disable=W0706
            raise
        except (FutureTimeoutError, RuntimeError) as e:
            try:
                future.cancel()  # Clean up pending task to avoid resource leaks
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    "Failed to cancel BLE future after timeout",
                    exc_info=True,
                )
            # Consume any late exceptions to avoid "Task exception was never retrieved"
            try:
                future.add_done_callback(
                    lambda f: f.exception()
                    if not f.cancelled()
                    else None  # pragma: no cover - best effort suppression
                )
            except Exception:
                # Event loop may be closing; ignore best-effort callback registration
                logger.debug(
                    "Skipping callback registration after timeout",
                    exc_info=True,
                )
            raise self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT) from e
        except (CancelledError, asyncio.CancelledError) as e:
            # Propagate as timeout-style BLEError so callers handle uniformly
            raise self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT) from e
        finally:
            if hasattr(self, "_pending_futures"):
                self._pending_futures.discard(future)

    def async_run(self, coro: "Coroutine") -> "Future":  # pylint: disable=C0116
        """
        Schedule a coroutine on the shared BLE event loop.

        Parameters
        ----------
        coro : Coroutine
            The coroutine to schedule.

        Returns
        -------
        concurrent.futures.Future
            Future representing the scheduled coroutine's eventual result.

        Raises
        ------
        BLEClient.BLEError
            If the client is closed or the runner is not available.
        """
        if self._closed:
            raise self.BLEError("Cannot schedule operation: BLE client is closed")
        try:
            return self._runner.run_coroutine_threadsafe(coro)
        except RuntimeError as e:
            raise self.BLEError(f"Failed to schedule operation: {e}") from e


# Expose zombie tracking from runner module for backwards compatibility
def get_zombie_thread_count() -> int:
    """
    Return the number of BLE event threads that failed to stop cleanly.

    Note: With the singleton runner architecture, this should typically be 0 or 1.
    """
    from meshtastic.interfaces.ble.runner import get_zombie_runner_count

    return get_zombie_runner_count()
