"""BLE client management and async operations."""

import asyncio
import contextlib
import weakref
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Thread
from typing import Awaitable, Coroutine, Optional, Union
from uuid import UUID

from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner
from bleak.exc import BleakDBusError

from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_ASYNC_TIMEOUT,
    ERROR_TIMEOUT,
    BLEConfig,
    logger,
)
from meshtastic.interfaces.ble.errors import BLEErrorHandler
from meshtastic.interfaces.ble.utils import sanitize_address

_zombie_thread_count = 0
_ZOMBIE_THREAD_WARN_THRESHOLD = 5


class BLEClient:
    """
    Client wrapper for managing BLE device connections with thread-safe async operations.

    This class provides a synchronous interface to Bleak's async operations by running
    an internal event loop in a dedicated thread. It handles the complexity of
    asyncio-to-thread synchronization while providing a simple API for BLE operations.
    When the underlying BLE stack blocks indefinitely the event thread may fail to exit;
    such occurrences are counted via get_zombie_thread_count() so operators can decide
    when a process restart is needed to reclaim BLE/DBus resources.
    """

    # Class-level fallback so callers using __new__ still get the right exception type
    class BLEError(Exception):
        """An exception class for BLE errors in the client."""

        pass

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
        Initialize the BLEClient, creating a dedicated asyncio event loop and background thread and optionally attaching a Bleak client for a specific device address.

        Parameters
        ----------
        address : Any
            BLE device address to attach a Bleak client to. If None, no Bleak client is created and the instance operates in discovery-only mode.
        log_if_no_address : Any
            If True and `address` is None, emit a debug message indicating discovery-only mode.
        **kwargs : dict
            Keyword arguments forwarded to the underlying Bleak client constructor when `address` is provided.

        Side effects:
            - Starts a background thread running an internal asyncio event loop for scheduling coroutines.
            - Creates an error handler and exposes the BLEError exception type on the instance.
            - Instantiates a Bleak client bound to `address` when `address` is provided.
        """
        # Error handling infrastructure
        self.error_handler = BLEErrorHandler()

        self.bleak_client: Optional[BleakRootClient] = None
        self.address = address
        self._closed = False
        self._pending_futures: weakref.WeakSet[Future] = weakref.WeakSet()
        # Create dedicated event loop for this client instance
        self._eventLoop = asyncio.new_event_loop()
        self._eventLoop.set_exception_handler(self._handle_loop_exception)
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
        return self.async_await(self.bleak_client.get_services(**kwargs))

    def has_characteristic(self, specifier: "Union[str, 'UUID']"):
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

    def _handle_loop_exception(self, loop, context):
        """
        Handle asyncio event loop exceptions and suppress benign errors during shutdown/disconnect.
        """
        exception = context.get("exception")
        if exception and isinstance(exception, BleakDBusError):
            # Suppress DBus errors that happen as unretrieved task exceptions,
            # especially "Operation failed with ATT error: 0x0e" which happens on disconnect.
            logger.debug("Suppressing BleakDBusError in BLE event loop: %s", exception)
            return

        # Default behavior for other exceptions
        loop.default_exception_handler(context)

    def close(self):  # pylint: disable=C0116
        """
        Shuts down the client's asyncio event loop and its background thread.

        Signals the internal event loop to stop, waits up to BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT for the thread to exit,
        and logs a warning if the thread does not terminate within that timeout.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True

        # Cancel any pending futures to unblock waiting threads immediately
        if hasattr(self, "_pending_futures"):
            for future in list(self._pending_futures):
                if not future.done():
                    future.cancel()

        loop = getattr(self, "_eventLoop", None)
        thread = getattr(self, "_eventThread", None)

        # Attempt to cancel all tasks and stop the loop cleanly
        if loop and not loop.is_closed() and loop.is_running():
            shutdown_coro = self._shutdown_event_loop(loop)
            try:
                fut = asyncio.run_coroutine_threadsafe(shutdown_coro, loop)
                fut.result(timeout=BLEConfig.BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT)
            except (RuntimeError, FutureTimeoutError):
                # Loop is already stopping/closed or did not respond in time; fall back to stop request.
                shutdown_coro.close()
                with contextlib.suppress(Exception):
                    loop.call_soon_threadsafe(loop.stop)
            except Exception:
                shutdown_coro.close()
                logger.debug(
                    "Error scheduling BLE loop shutdown; forcing stop", exc_info=True
                )
                with contextlib.suppress(Exception):
                    loop.call_soon_threadsafe(loop.stop)
        elif loop and not loop.is_closed():
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(loop.stop)

        if thread:
            thread.join(timeout=BLEConfig.BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT)
        if thread and thread.is_alive():
            global _zombie_thread_count
            _zombie_thread_count += 1
            logger.error(
                "BLE event thread did not exit within %.1fs and may leak resources (total zombies: %d)",
                BLEConfig.BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT,
                _zombie_thread_count,
            )
            if _zombie_thread_count >= _ZOMBIE_THREAD_WARN_THRESHOLD:
                logger.warning(
                    "Multiple zombie BLE threads detected (%d). Consider restarting the process to recover resources.",
                    _zombie_thread_count,
                )
        elif loop and not loop.is_closed():
            # Ensure loop resources are released when the thread exits normally
            loop.close()

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
        Wait for the given coroutine to complete on the client's event loop and return its result.

        If the coroutine does not finish within `timeout` seconds the pending task is cancelled and a BLEClient.BLEError is raised.

        Parameters
        ----------
        coro : Any
            The coroutine to run on the client's internal event loop.
        timeout : Any
            | None Maximum seconds to wait for completion; `None` means wait indefinitely.

        Returns
        -------
            The value produced by the completed coroutine.

        Raises
        ------
            BLEClient.BLEError: If the wait times out.

        """
        # Exception mapping contract:
        #   - FutureTimeoutError -> self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT)
        #   - Bleak* exceptions propagate so interface wrappers can convert them consistently.
        future = self.async_run(coro)  # type: ignore[arg-type]
        if hasattr(self, "_pending_futures"):
            self._pending_futures.add(future)
        try:
            return future.result(timeout)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            raise
        except (FutureTimeoutError, RuntimeError) as e:
            loop_closed = self._eventLoop.is_closed()
            if not loop_closed:
                try:
                    future.cancel()  # Clean up pending task to avoid resource leaks
                except Exception:  # pragma: no cover - defensive
                    logger.debug(
                        "Failed to cancel BLE future after timeout/loop-close",
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
                        "Skipping callback registration after timeout; event loop may be closing",
                        exc_info=True,
                    )
            else:
                logger.debug("Event loop already closed; skipping future cancellation")
            raise self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT) from e
        except (CancelledError, asyncio.CancelledError) as e:
            # Propagate as timeout-style BLEError so callers handle uniformly
            raise self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT) from e
        finally:
            if hasattr(self, "_pending_futures"):
                self._pending_futures.discard(future)

    def async_run(self, coro: "Coroutine") -> "Future":  # pylint: disable=C0116
        """
        Schedule a coroutine on the client's internal asyncio event loop.

        Parameters
        ----------
        coro : Any
            coroutine The coroutine to schedule.

        Returns
        -------
            concurrent.futures.Future: Future representing the scheduled coroutine's eventual result.

        """
        return asyncio.run_coroutine_threadsafe(coro, self._eventLoop)  # type: ignore[arg-type]

    def _run_event_loop(self):
        """
        Run the client's asyncio event loop in the background thread until it is stopped.

        When the loop exits it is closed to release resources; runtime errors raised while the loop runs are captured and not re-raised.
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

    async def _shutdown_event_loop(self, loop: asyncio.AbstractEventLoop):
        """
        Cancel pending tasks on the given loop and stop it once they are drained.
        """
        tasks = [
            t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task(loop)
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()


def get_zombie_thread_count() -> int:
    """
    Return the number of BLE event threads that failed to stop cleanly.
    """
    return _zombie_thread_count
