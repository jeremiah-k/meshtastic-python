"""BLE client management and async operations."""

import asyncio
import contextlib
import sys
import types
import warnings
import weakref
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import RLock
from typing import Any, Awaitable, Coroutine, Optional, Type, TypeVar, Union
from uuid import UUID

from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner

from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_ASYNC_TIMEOUT,
    DISCONNECT_TIMEOUT_SECONDS,
    ERROR_TIMEOUT,
    logger,
)
from meshtastic.interfaces.ble.errors import BLEErrorHandler
from meshtastic.interfaces.ble.runner import BLECoroutineRunner

T = TypeVar("T")


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
    async def _with_timeout(
        awaitable: Awaitable[T], timeout: Optional[float], label: str
    ) -> T:
        """
        Waits for the given awaitable to complete and raises a BLEClient.BLEError if it does not finish within the specified timeout.

        Parameters
        ----------
            awaitable (Awaitable): The awaitable to run.
            timeout (float | None): Maximum seconds to wait; if None, waits indefinitely.
            label (str): Short description inserted into the timeout error message.

        Returns
        -------
            The value returned by the awaitable.

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
        self,
        address: Optional[str] = None,
        *,
        log_if_no_address: bool = True,
        **kwargs: Any,
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
        self._close_lock = RLock()

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

    def discover(self, **kwargs: Any) -> Any:
        """
        Discover nearby BLE devices.

        Keyword arguments are passed to the scanner to configure discovery options (for example, `timeout` or `adapter`).

        Returns:
            A list of discovered `BLEDevice` objects.

        """
        return self.async_await(BleakScanner.discover(**kwargs))

    def pair(self, **kwargs: Any) -> Any:
        """
        Pair the BLE client with the remote device.

        Parameters
        ----------
            **kwargs (Any): Backend-specific pairing options forwarded to the underlying BLE client.

        Returns
        -------
            The backend pairing result (often `None`).

        Raises
        ------
            BLEError: If the BLE client is not initialized or the pairing operation fails.

        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot pair: BLE client not initialized")
        return self.async_await(self.bleak_client.pair(**kwargs))

    def connect(self, *, await_timeout: Optional[float] = None, **kwargs: Any) -> Any:
        """
        Connect to the remote BLE device.

        Parameters
        ----------
            await_timeout (float | None): Maximum seconds to wait for the connect operation to complete; None to wait indefinitely.
            **kwargs: Forwarded to the underlying Bleak client's `connect` call.

        Returns
        -------
            The value returned by the underlying Bleak client's `connect` call (typically `None`). The method raises an exception on failure.

        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot connect: BLE client not initialized")
        return self.async_await(
            self.bleak_client.connect(**kwargs), timeout=await_timeout
        )

    def is_connected(self) -> bool:
        """
        Report whether the underlying Bleak client currently has an active connection.

        Returns:
            `True` if the bleak client reports an active connection, `False`
            otherwise (also `False` when no bleak client exists or the
            connection state cannot be read).

        """
        # Keep getattr() defensive: some tests instantiate via object.__new__
        # and bypass __init__, so bleak_client may legitimately be absent.
        bleak_client = getattr(self, "bleak_client", None)
        if bleak_client is None:
            return False

        def _check_connection():
            """
            Check whether the configured Bleak client reports an active connection.

            This interprets either a boolean `is_connected` attribute or an `is_connected()` method on the client and coerces the result to a boolean.

            Returns:
                bool: `True` if the client reports an active connection, `False` otherwise.

            """
            connected = getattr(bleak_client, "is_connected", False)
            if callable(connected):
                connected = connected()  # pylint: disable=E1102
            return bool(connected)

        result = self.error_handler._safe_execute(
            _check_connection,
            default_return=False,
            error_msg="Unable to read bleak connection state",
            reraise=False,
        )
        return bool(result)  # type: ignore[arg-type]

    def disconnect(
        self, *, await_timeout: Optional[float] = None, **kwargs: Any
    ) -> None:
        """
        Disconnect from the remote BLE device and wait until the operation completes.

        Parameters
        ----------
            await_timeout (float | None): Maximum seconds to wait for disconnect to complete. If `None`, wait indefinitely.
            **kwargs: Additional keyword arguments forwarded to the underlying Bleak client's `disconnect` method.

        Note:
            In bleak >= 2.1.1, this method returns None. The operation raises
            an exception on failure; success is indicated by normal return.

        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot disconnect: BLE client not initialized")
        self.async_await(self.bleak_client.disconnect(**kwargs), timeout=await_timeout)

    def read_gatt_char(
        self, *args: Any, timeout: Optional[float] = None, **kwargs: Any
    ) -> bytes:
        """
        Read a GATT characteristic value from the connected BLE device.

        Parameters
        ----------
            *args: Positional identifier(s) for the characteristic (commonly a UUID string or integer handle).
            timeout (Optional[float]): Maximum seconds to wait for the read to complete; `None` means no timeout.
            **kwargs: Additional keyword arguments forwarded to the underlying read operation.

        Returns
        -------
            bytes: Raw bytes read from the characteristic.

        Raises
        ------
            BLEClient.BLEError: If the BLE client is not initialized or the operation times out.

        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot read: BLE client not initialized")
        return self.async_await(
            self.bleak_client.read_gatt_char(*args, **kwargs), timeout=timeout
        )

    def readGattChar(
        self, *args: Any, timeout: Optional[float] = None, **kwargs: Any
    ) -> bytes:
        """Compatibility wrapper for callers using camelCase."""
        warnings.warn(
            "readGattChar is deprecated; use read_gatt_char instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.read_gatt_char(*args, timeout=timeout, **kwargs)

    def write_gatt_char(
        self, *args: Any, timeout: Optional[float] = None, **kwargs: Any
    ) -> None:
        """
        Write bytes to a GATT characteristic on the connected device and wait for completion.

        Parameters
        ----------
            *args: Positional arguments identifying the characteristic and payload (typically a UUID or handle followed by the data bytes).
            timeout (Optional[float]): Maximum seconds to wait for the write to complete; None to wait indefinitely.
            **kwargs: Additional keyword arguments forwarded to the underlying write operation.

        Raises
        ------
            BLEClient.BLEError: If no Bleak client is initialized, the write fails, or the wait times out.

        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot write: BLE client not initialized")
        self.async_await(
            self.bleak_client.write_gatt_char(*args, **kwargs), timeout=timeout
        )

    def writeGattChar(
        self, *args: Any, timeout: Optional[float] = None, **kwargs: Any
    ) -> None:
        """
        Deprecated camelCase compatibility wrapper for writing a GATT characteristic.

        This function is retained for backwards compatibility and forwards all positional and keyword arguments to the canonical write_gatt_char implementation. Use write_gatt_char instead.
        """
        warnings.warn(
            "writeGattChar is deprecated; use write_gatt_char instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.write_gatt_char(*args, timeout=timeout, **kwargs)

    def get_services(self, **kwargs: Any) -> Any:
        """
        Retrieve the underlying Bleak client's discovered GATT services and characteristics.

        In Bleak 2.1.1+, services are automatically enumerated during connect() and accessed
        via the client.services property. Keyword arguments are ignored but accepted for
        backward compatibility.

        Returns:
            The services collection object from the underlying Bleak client containing
            discovered GATT services and their characteristics.

        Raises:
            BLEError: If the BLE client has not been initialized.

        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot get services: BLE client not initialized")
        # In Bleak 2.1.1+, services are auto-enumerated on connect and exposed as a property.
        return self.bleak_client.services

    def getServices(self, **kwargs: Any) -> Any:
        """
        Deprecated camelCase compatibility wrapper that returns discovered GATT services and characteristics; use `get_services` instead.

        Returns:
            The discovered GATT services and characteristics.

        """
        warnings.warn(
            "getServices is deprecated; use get_services instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.get_services(**kwargs)

    def has_characteristic(self, specifier: Union[str, UUID]) -> bool:
        """
        Determine whether the connected device exposes the GATT characteristic identified by `specifier`.

        If services are not populated, attempts to fetch services before checking.

        Parameters
        ----------
            specifier (str | UUID): UUID string or UUID object identifying the characteristic to check.

        Returns
        -------
            bool: `True` if the characteristic is present, `False` otherwise.

        """
        if self.bleak_client is None:
            return False

        services = getattr(self.bleak_client, "services", None)
        if not services or not getattr(services, "get_characteristic", None):
            services = self.error_handler._safe_execute(
                lambda: self.get_services(),
                error_msg="Unable to populate services before has_characteristic",
                reraise=False,
            )
            if not services:
                services = getattr(self.bleak_client, "services", None)
        return bool(services and services.get_characteristic(specifier))

    def hasCharacteristic(self, specifier: Union[str, UUID]) -> bool:
        """Compatibility wrapper for callers using camelCase."""
        warnings.warn(
            "hasCharacteristic is deprecated; use has_characteristic instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.has_characteristic(specifier)

    def start_notify(
        self, *args: Any, timeout: Optional[float] = None, **kwargs: Any
    ) -> None:
        """
        Subscribe to notifications for a GATT characteristic.

        Registers a notification callback for the connected device's characteristic and waits up to `timeout` seconds for the registration to complete.

        Parameters
        ----------
            *args: Positional arguments passed to the notification registration (typically the characteristic UUID or handle followed by a callback to receive byte payloads).
            timeout (float | None): Maximum seconds to wait for the operation to complete; if `None`, wait indefinitely.
            **kwargs: Additional keyword arguments forwarded to the notification registration.

        Raises
        ------
            BLEError: If no BLE client is initialized, if the registration fails, or if the operation times out.

        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot start notify: BLE client not initialized")
        self.async_await(
            self.bleak_client.start_notify(*args, **kwargs), timeout=timeout
        )

    def startNotify(
        self, *args: Any, timeout: Optional[float] = None, **kwargs: Any
    ) -> None:
        """Compatibility wrapper for callers using camelCase."""
        warnings.warn(
            "startNotify is deprecated; use start_notify instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.start_notify(*args, timeout=timeout, **kwargs)

    def stop_notify(
        self, *args: Any, timeout: Optional[float] = None, **kwargs: Any
    ) -> None:
        """
        Unsubscribe notifications for a GATT characteristic on the connected device.

        Parameters
        ----------
            *args: Positional arguments passed to the underlying notification stop
                call, typically the characteristic UUID (or handle).
            timeout (float | None): Maximum seconds to wait for the operation
                to complete; if None, wait indefinitely.
            **kwargs: Additional keyword arguments passed to the underlying
                notification stop call.

        Raises
        ------
            BLEError: If no BLE client is initialized or if the operation times out or fails.

        """
        if self.bleak_client is None:
            raise self.BLEError("Cannot stop notify: BLE client not initialized")
        self.async_await(
            self.bleak_client.stop_notify(*args, **kwargs), timeout=timeout
        )

    def stopNotify(
        self, *args: Any, timeout: Optional[float] = None, **kwargs: Any
    ) -> None:
        """
        Deprecated camelCase compatibility wrapper that stops notifications.

        Issues a DeprecationWarning when called and performs the same notification-stop operation using the provided arguments.
        """
        warnings.warn(
            "stopNotify is deprecated; use stop_notify instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.stop_notify(*args, timeout=timeout, **kwargs)

    def close(self) -> None:
        """
        Close the BLEClient, best-effort disconnecting transport first, then cancel pending operations.

        If an underlying Bleak client exists and currently reports connected,
        close() attempts a bounded disconnect first. Any disconnect errors are
        suppressed so shutdown remains best-effort and idempotent. After that,
        this marks the wrapper as closed and cancels pending futures to unblock
        waiting callers. This does not stop or affect any shared event loop
        used by other clients.
        """
        with self._close_lock:
            if getattr(self, "_closed", False):
                return

            # Best effort: disconnect active transport before closing this wrapper.
            if getattr(self, "bleak_client", None) is not None and self.is_connected():
                self.error_handler._safe_cleanup(
                    lambda: self.disconnect(await_timeout=DISCONNECT_TIMEOUT_SECONDS),
                    "client disconnect during close",
                )

            self._closed = True

            # Cancel any pending futures to unblock waiting threads immediately.
            if hasattr(self, "_pending_futures"):
                for future in list(self._pending_futures):
                    if not future.done():
                        future.cancel()

    def __enter__(self) -> "BLEClient":
        """
        Provide the BLEClient instance for use as a context manager.

        Returns:
            self: The BLEClient instance entered into the context.

        """
        return self

    def __exit__(
        self,
        _type: Optional[Type[BaseException]],
        _value: Optional[BaseException],
        _traceback: Optional[types.TracebackType],
    ) -> None:
        """
        Close the BLEClient when exiting a context manager.

        This method calls close() to release resources. Any exception
        information supplied by the context manager (`_type`, `_value`,
        `_traceback`) is ignored and exceptions are not suppressed.
        """
        self.close()

    def async_await(
        self, coro: Coroutine[Any, Any, Any], timeout: Optional[float] = None
    ) -> Any:
        """
        Wait for the given coroutine to complete and return its result.

        If the coroutine does not finish within `timeout` seconds the pending
        task is cancelled and a BLEClient.BLEError is raised.

        Parameters
        ----------
        coro : Coroutine[Any, Any, Any]
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
        future = self.async_run(coro)
        if hasattr(self, "_pending_futures"):
            self._pending_futures.add(future)
        try:
            # On macOS, CoreBluetooth requires occasional I/O operations for
            # callbacks to be properly delivered. Without debug logging, no I/O
            # was happening, causing callbacks to never be processed.
            # TODO: Remove this workaround once upstream bleak fixes callback
            # starvation on macOS CoreBluetooth.
            stdout = getattr(sys, "stdout", None)
            if stdout is not None and hasattr(stdout, "flush"):
                with contextlib.suppress(ValueError, OSError, AttributeError):
                    if not getattr(stdout, "closed", False):
                        stdout.flush()
            return future.result(timeout)
        except SystemExit:  # pylint: disable=W0706
            raise
        except KeyboardInterrupt:  # pylint: disable=W0706
            raise
        except FutureTimeoutError as e:
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
                    lambda f: (
                        f.exception() if not f.cancelled() else None
                    )  # pragma: no cover - best effort suppression
                )
            except Exception:
                # Event loop may be closing; ignore best-effort callback registration
                logger.debug(
                    "Skipping callback registration after timeout",
                    exc_info=True,
                )
            raise self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT) from e
        except RuntimeError as e:
            # RuntimeError here typically indicates loop shutdown/closure, not a timeout.
            try:
                future.cancel()
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    "Failed to cancel BLE future after runtime error",
                    exc_info=True,
                )
            raise self.BLEError(f"Async operation failed: {e}") from e
        except asyncio.CancelledError as e:
            # Propagate as timeout-style BLEError so callers handle uniformly
            raise self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT) from e
        finally:
            if hasattr(self, "_pending_futures"):
                self._pending_futures.discard(future)

    def asyncAwait(
        self, coro: Coroutine[Any, Any, Any], timeout: Optional[float] = None
    ) -> Any:
        """
        Deprecated compatibility wrapper that forwards the given coroutine to async_await.

        This method is provided for camelCase compatibility and emits a DeprecationWarning; use async_await instead.

        Parameters
        ----------
            coro (Coroutine[Any, Any, Any]): The coroutine to schedule and await.
            timeout (Optional[float]): Optional timeout in seconds for awaiting the coroutine.

        Returns
        -------
            Any: The result returned by the awaited coroutine.

        """
        warnings.warn(
            "asyncAwait is deprecated; use async_await instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.async_await(coro, timeout=timeout)

    def async_run(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        """
        Schedule the given coroutine to run on the shared BLE event loop.

        Parameters
        ----------
            coro (Coroutine): The coroutine to schedule on the shared BLE event loop.

        Returns
        -------
            concurrent.futures.Future: Future representing the scheduled coroutine's eventual result.

        Raises
        ------
            BLEClient.BLEError: If the BLEClient is closed or the coroutine cannot be scheduled.

        """
        if self._closed:
            raise self.BLEError("Cannot schedule operation: BLE client is closed")
        try:
            return self._runner.run_coroutine_threadsafe(coro)
        except RuntimeError as e:
            # Close the coroutine to prevent "coroutine was never awaited" warning
            with contextlib.suppress(Exception):
                coro.close()
            raise self.BLEError(f"Failed to schedule operation: {e}") from e

    def asyncRun(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        """
        Compatibility wrapper that schedules a coroutine on the shared BLE event loop using the camelCase name.

        Returns:
            Future[Any]: A Future representing the scheduled coroutine.

        """
        warnings.warn(
            "asyncRun is deprecated; use async_run instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.async_run(coro)


# Expose zombie tracking from runner module for backwards compatibility
def get_zombie_thread_count() -> int:
    """
    Get the number of zombie BLE runner threads.

    Returns:
        int: Number of zombie BLE runner threads.

    """
    from meshtastic.interfaces.ble.runner import get_zombie_runner_count

    return get_zombie_runner_count()


def getZombieThreadCount() -> int:
    """
    Report the number of BLE event threads that failed to stop cleanly.

    Returns:
        int: Number of zombie BLE event threads (typically 0 or 1 due to the singleton runner).

    """
    warnings.warn(
        "getZombieThreadCount is deprecated; use get_zombie_thread_count instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_zombie_thread_count()
