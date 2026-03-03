"""BLE client management and async operations."""

import asyncio
import contextlib
import sys
import time
import types
import weakref
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Lock, RLock, current_thread
from typing import Any, Awaitable, Callable, Coroutine, TypeVar, cast
from uuid import UUID

from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner
from bleak.exc import BleakError

from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_ASYNC_OPERATION_FAILED,
    BLECLIENT_ERROR_ASYNC_TIMEOUT,
    BLECLIENT_ERROR_CANCELLED,
    BLECLIENT_ERROR_CANNOT_CONNECT_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_DISCONNECT_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_GET_SERVICES_NOT_DISCOVERED,
    BLECLIENT_ERROR_CANNOT_GET_SERVICES_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_READ_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_SCHEDULE_CLOSED,
    BLECLIENT_ERROR_CANNOT_START_NOTIFY_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_STOP_NOTIFY_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_WRITE_NOT_INITIALIZED,
    BLECLIENT_ERROR_FAILED_TO_SCHEDULE,
    BLECLIENT_ERROR_RUNNER_THREAD_WAIT,
    DISCONNECT_TIMEOUT_SECONDS,
    ERROR_TIMEOUT,
    SERVICE_CHARACTERISTIC_RETRY_COUNT,
    SERVICE_CHARACTERISTIC_RETRY_DELAY,
    logger,
)
from meshtastic.interfaces.ble.errors import BLEErrorHandler
from meshtastic.interfaces.ble.runner import BLECoroutineRunner
from meshtastic.interfaces.ble.utils import with_timeout

T = TypeVar("T")

# macOS/CoreBluetooth workaround tracking:
# Bleak can starve callback delivery unless occasional I/O occurs on stdout.
# We apply a guarded stdout.flush() nudge in _async_await() on darwin hosts.
# This is limited when stdout is redirected or non-flushable.
# Upstream issue: https://github.com/hbldh/bleak/issues?q=is%3Aissue+CoreBluetooth+callback

_MACOS_WORKAROUND_LOG_LOCK = Lock()
_MACOS_WORKAROUND_LOGGED_UNAVAILABLE = False


def _log_macos_stdout_workaround_unavailable_once(reason: str) -> None:
    """Log once when the macOS stdout flush workaround cannot be applied."""
    global _MACOS_WORKAROUND_LOGGED_UNAVAILABLE  # pylint: disable=global-statement
    with _MACOS_WORKAROUND_LOG_LOCK:
        if _MACOS_WORKAROUND_LOGGED_UNAVAILABLE:
            return
        _MACOS_WORKAROUND_LOGGED_UNAVAILABLE = True
    logger.debug("macOS CoreBluetooth workaround skipped: %s.", reason)


class BLEClient:
    """Client wrapper for managing BLE device connections with thread-safe async operations.

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
        awaitable: Awaitable[T], timeout: float | None, label: str
    ) -> T:
        """Await an awaitable and raise BLEClient.BLEError if it does not complete within the given timeout.

        Parameters
        ----------
        awaitable : Awaitable[T]
            The awaitable to run.
        timeout : float | None
            Maximum seconds to wait; if None, wait indefinitely.
        label : str
            Short description used in the timeout error message.

        Returns
        -------
        T
            The value produced by the awaitable.

        Raises
        ------
        BLEClient.BLEError
            If the awaitable does not complete before the timeout elapses.
        """
        return await with_timeout(
            awaitable,
            timeout,
            label,
            timeout_error_factory=lambda timeout_label, timeout_seconds: BLEClient.BLEError(
                ERROR_TIMEOUT.format(timeout_label, timeout_seconds)
            ),
        )

    def __init__(
        self,
        address: str | None = None,
        *,
        log_if_no_address: bool = True,
        **kwargs: Any,
    ) -> None:
        """Initialize the BLEClient, set up its error-handling and runner infrastructure, and optionally create a Bleak client bound to the given address.

        Parameters
        ----------
        address : str | None
            BLE device address to attach a Bleak client to. If None, the instance is created in discovery-only mode and no Bleak client is instantiated. (Default value = None)
        log_if_no_address : bool
            If True and `address` is None, emit a debug message indicating discovery-only mode. (Default value = True)
        **kwargs : Any
            Keyword arguments forwarded to the underlying Bleak client constructor when `address` is provided.
        """
        # Error handling infrastructure
        self.error_handler = BLEErrorHandler()

        self.bleak_client: BleakRootClient | None = None
        self.address = address
        self._closed = False
        self._pending_futures: weakref.WeakSet[Future[Any]] = weakref.WeakSet()
        self._pending_futures_lock = RLock()
        self._close_lock = RLock()

        # Use singleton runner for all BLE operations
        self._runner = BLECoroutineRunner()

        if address is None:
            if log_if_no_address:
                logger.debug("No address provided - only discover method will work.")
            return

        # Create underlying Bleak client for actual BLE communication
        self.bleak_client = BleakRootClient(address, **kwargs)

    def _discover(self, **kwargs: Any) -> Any:
        """Discover nearby BLE devices.

        Keyword arguments are forwarded to BleakScanner.discover (for example: `timeout`, `adapter`) to configure discovery.

        Parameters
        ----------
        **kwargs : Any
            Keyword arguments forwarded to BleakScanner.discover.

        Returns
        -------
        Any
            A list of discovered `BLEDevice` objects.
        """
        return self._async_await(BleakScanner.discover(**kwargs))

    def discover(self, **kwargs: Any) -> Any:
        """Discover nearby BLE devices.

        Parameters
        ----------
        **kwargs : Any
            Keyword arguments forwarded to BleakScanner.discover.

        Returns
        -------
        Any
            A sequence of discovered BLE device objects (backend-specific device representations).
        """
        return self._discover(**kwargs)

    def pair(self, **kwargs: Any) -> Any:
        """Pair the BLE client with the remote device.

        Parameters
        ----------
        **kwargs : Any
            Backend-specific pairing options forwarded to the underlying BLE client.

        Returns
        -------
        Any
            The backend pairing result (often `None`).

        Raises
        ------
        BLEError
            If the BLE client is not initialized or the pairing operation fails.
        """
        bleak_client = self.bleak_client
        if bleak_client is None:
            raise self.BLEError(BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED)
        return self._async_await(bleak_client.pair(**kwargs))

    def connect(self, *, await_timeout: float | None = None, **kwargs: Any) -> Any:
        """Connect to the remote BLE device.

        Parameters
        ----------
        await_timeout : float | None
            Maximum seconds to wait for the connect operation to complete; None to wait indefinitely. (Default value = None)
        **kwargs : Any
            Forwarded to the underlying Bleak client's `connect` call.

        Returns
        -------
        Any
            The value returned by the underlying Bleak client's `connect` call (typically `None`). The method raises an exception on failure.

        Raises
        ------
        BLEError
            If the BLE client is not initialized or the connection operation fails.
        """
        bleak_client = self.bleak_client
        if bleak_client is None:
            raise self.BLEError(BLECLIENT_ERROR_CANNOT_CONNECT_NOT_INITIALIZED)
        return self._async_await(bleak_client.connect(**kwargs), timeout=await_timeout)

    def isConnected(self) -> bool:
        """Return whether the underlying Bleak client currently has an active connection.

        Returns
        -------
        bool
            `True` if the bleak client reports an active connection; `False` if no bleak client exists or the connection state cannot be read.
        """
        # Keep getattr() defensive: some tests instantiate via object.__new__
        # and bypass __init__, so bleak_client may legitimately be absent.
        bleak_client = getattr(self, "bleak_client", None)
        if bleak_client is None:
            return False

        def _check_connection() -> bool:
            """Return whether the configured Bleak client reports an active connection.

            This interprets either a boolean `is_connected` attribute or an `is_connected()` method on the client and coerces the result to a boolean.

            Returns
            -------
            bool
                `True` if the client reports an active connection, `False` otherwise.
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
        return bool(result)

    # COMPAT_STABLE_SHIM: snake_case alias for isConnected
    def is_connected(self) -> bool:
        """Return whether the BLE client is currently connected.

        Returns
        -------
        bool
            `True` if the client is connected, `False` otherwise.
        """
        return self.isConnected()

    def disconnect(self, *, await_timeout: float | None = None, **kwargs: Any) -> None:
        """Disconnect from the remote BLE device and wait for the operation to complete.

        Parameters
        ----------
        await_timeout : float | None
            Maximum seconds to wait for the disconnect to finish. If None, wait indefinitely. (Default value = None)
        **kwargs : Any
            Additional keyword arguments forwarded to the underlying Bleak client's disconnect method.

        Raises
        ------
        BLEError
            If the BLE client is not initialized or if the underlying disconnect fails.
        """
        bleak_client = self.bleak_client
        if bleak_client is None:
            raise self.BLEError(BLECLIENT_ERROR_CANNOT_DISCONNECT_NOT_INITIALIZED)
        self._async_await(bleak_client.disconnect(**kwargs), timeout=await_timeout)

    def read_gatt_char(
        self, *args: Any, timeout: float | None = None, **kwargs: Any
    ) -> bytes:
        """Read the value of a GATT characteristic from the connected BLE device.

        Parameters
        ----------
        *args : Any
            Identifier(s) for the characteristic (commonly a UUID string or integer handle).
        timeout : float | None
            Maximum seconds to wait for the read to complete; None means no timeout. (Default value = None)
        **kwargs : Any
            Additional keyword arguments forwarded to the underlying BLE library.

        Returns
        -------
        bytes
            Raw bytes read from the characteristic.

        Raises
        ------
        BLEClient.BLEError
            If the BLE client is not initialized or the read operation times out.
        BLEError
            If the read operation fails for any other reason.
        """
        bleak_client = self.bleak_client
        if bleak_client is None:
            raise self.BLEError(BLECLIENT_ERROR_CANNOT_READ_NOT_INITIALIZED)
        return cast(
            bytes,
            self._async_await(
                bleak_client.read_gatt_char(*args, **kwargs), timeout=timeout
            ),
        )

    def write_gatt_char(
        self, *args: Any, timeout: float | None = None, **kwargs: Any
    ) -> None:
        """Write bytes to a GATT characteristic on the connected device and wait for completion.

        Parameters
        ----------
        *args : Any
            Positional arguments identifying the characteristic and payload (typically a UUID or handle followed by the data bytes).
        timeout : float | None
            Maximum seconds to wait for the write to complete; None to wait indefinitely. (Default value = None)
        **kwargs : Any
            Additional keyword arguments forwarded to the underlying write operation.

        Raises
        ------
        BLEClient.BLEError
            If no Bleak client is initialized, the write fails, or the wait times out.
        BLEError
            If the write operation fails for any other reason.
        """
        bleak_client = self.bleak_client
        if bleak_client is None:
            raise self.BLEError(BLECLIENT_ERROR_CANNOT_WRITE_NOT_INITIALIZED)
        self._async_await(
            bleak_client.write_gatt_char(*args, **kwargs), timeout=timeout
        )

    def _get_services(self, **_kwargs: Any) -> Any:
        """Return the underlying Bleak client's discovered GATT services collection.

        Keyword arguments are accepted for caller convenience but ignored. The returned object exposes discovered GATT services and their characteristics (as provided by Bleak).

        Parameters
        ----------
        **_kwargs : Any
            Keyword arguments accepted for compatibility but ignored.

        Returns
        -------
        Any
            The services collection object from the underlying Bleak client.

        Raises
        ------
        BLEError
            If the BLE client has not been initialized or services cannot be
            retrieved from Bleak (for example, if discovery has not completed).
        """
        bleak_client = self.bleak_client
        if bleak_client is None:
            raise self.BLEError(BLECLIENT_ERROR_CANNOT_GET_SERVICES_NOT_INITIALIZED)
        # In Bleak 2.1.1+, services are auto-enumerated on connect and exposed
        # as a property, but the property can still raise during discovery.
        try:
            return bleak_client.services
        except BleakError as exc:
            raise self.BLEError(
                BLECLIENT_ERROR_CANNOT_GET_SERVICES_NOT_DISCOVERED
            ) from exc

    def has_characteristic(self, specifier: str | UUID) -> bool:
        """Return whether the connected device exposes the GATT characteristic identified by specifier.

        If services are not populated, attempts to fetch services before checking.

        Parameters
        ----------
        specifier : str | UUID
            UUID string or UUID object identifying the characteristic to check.

        Returns
        -------
        bool
            `True` if the characteristic is present, `False` otherwise.
        """
        bleak_client = self.bleak_client
        if bleak_client is None:
            return False

        def _read_services_property() -> Any:
            """Read Bleak services property, treating BleakError as unavailable."""
            try:
                return bleak_client.services
            except BleakError:
                return None

        def _refresh_services(error_msg: str) -> Any:
            """Refresh services via _get_services() and fallback property read."""
            services = self.error_handler._safe_execute(
                lambda: self._get_services(),
                error_msg=error_msg,
                reraise=False,
            )
            return services if services else _read_services_property()

        def _has_get_characteristic(services: Any) -> bool:
            """Return True if services object has a characteristic lookup callable."""
            return bool(
                services and callable(getattr(services, "get_characteristic", None))
            )

        services = _read_services_property()
        if not _has_get_characteristic(services):
            services = _refresh_services(
                "Unable to populate services before has_characteristic"
            )

        for attempt in range(SERVICE_CHARACTERISTIC_RETRY_COUNT):
            if not _has_get_characteristic(services):
                return False
            try:
                return bool(services.get_characteristic(specifier))
            except BleakError:
                services = _refresh_services(
                    "Unable to populate services before has_characteristic after "
                    "BleakError from get_characteristic"
                )
                if attempt + 1 < SERVICE_CHARACTERISTIC_RETRY_COUNT:
                    time.sleep(SERVICE_CHARACTERISTIC_RETRY_DELAY)
        return False

    def start_notify(
        self, *args: Any, timeout: float | None = None, **kwargs: Any
    ) -> None:
        """Subscribe to notifications for a GATT characteristic.

        Registers a notification callback for the connected device's characteristic and waits up to `timeout` seconds for the registration to complete.

        Parameters
        ----------
        *args : Any
            Positional arguments forwarded to the underlying notification registration (typically the characteristic UUID or handle followed by a callback to receive byte payloads).
        timeout : float | None
            Maximum seconds to wait for the operation to complete; if `None`, wait indefinitely. (Default value = None)
        **kwargs : Any
            Additional keyword arguments forwarded to the notification registration.

        Raises
        ------
        BLEError
            If the BLE client is not initialized, the registration fails, or the operation times out.
        """
        bleak_client = self.bleak_client
        if bleak_client is None:
            raise self.BLEError(BLECLIENT_ERROR_CANNOT_START_NOTIFY_NOT_INITIALIZED)
        self._async_await(bleak_client.start_notify(*args, **kwargs), timeout=timeout)

    def stopNotify(
        self, *args: Any, timeout: float | None = None, **kwargs: Any
    ) -> None:
        """Unsubscribe notifications for a GATT characteristic on the connected device.

        Parameters
        ----------
        *args : Any
            Positional arguments passed to the underlying notification stop call, typically the characteristic UUID (or handle).
        timeout : float | None
            Maximum seconds to wait for the operation to complete; if None, wait indefinitely. (Default value = None)
        **kwargs : Any
            Additional keyword arguments passed to the underlying notification stop call.

        Raises
        ------
        BLEError
            If no BLE client is initialized or if the operation times out or fails.
        """
        bleak_client = self.bleak_client
        if bleak_client is None:
            raise self.BLEError(BLECLIENT_ERROR_CANNOT_STOP_NOTIFY_NOT_INITIALIZED)
        self._async_await(bleak_client.stop_notify(*args, **kwargs), timeout=timeout)

    # COMPAT_STABLE_SHIM: snake_case alias for stopNotify
    def stop_notify(
        self, *args: Any, timeout: float | None = None, **kwargs: Any
    ) -> None:
        """Backward-compatible snake_case alias for stopNotify.

        Parameters
        ----------
        *args : Any
            Positional arguments passed to stopNotify.
        timeout : float | None
            Maximum seconds to wait for the operation to complete; if None, wait indefinitely. (Default value = None)
        **kwargs : Any
            Keyword arguments passed to stopNotify.

        Returns
        -------
        None
            The return value from stopNotify (typically None).
        """
        return self.stopNotify(*args, timeout=timeout, **kwargs)

    def close(self) -> None:
        """Close the BLEClient and perform a best-effort shutdown.

        If an underlying Bleak client exists and is connected, this attempts a bounded disconnect and suppresses any disconnect errors so shutdown remains best-effort and idempotent. The method is thread-safe (uses an internal close lock), marks the wrapper as closed, and cancels any tracked pending futures to unblock waiting callers. This does not stop or affect the shared BLE event loop used by other clients.
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

            # Explicitly clear the transport reference to make closed-state semantics
            # unambiguous and prevent accidental reuse after close().
            self.bleak_client = None
            self._closed = True

            # Cancel any pending futures to unblock waiting threads immediately.
            def _cancel_pending(pending_futures: weakref.WeakSet[Future[Any]]) -> None:
                for future in list(pending_futures):
                    if not future.done():
                        future.cancel()

            self._with_pending_futures(_cancel_pending)

    def __enter__(self) -> "BLEClient":
        """Enter a context and return this BLEClient instance.

        Returns
        -------
        'BLEClient'
            The same BLEClient instance.
        """
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: types.TracebackType | None,
    ) -> None:
        """Close the BLEClient when exiting a context manager.

        If an exception occurred within the with-block, any exception raised by
        close() is logged and suppressed so the original exception propagates.
        If no with-block exception occurred, close() exceptions propagate normally.
        """
        try:
            self.close()
        except Exception:
            if _type is not None:
                logger.warning(
                    "close() failed while unwinding an existing exception.",
                    exc_info=True,
                )
            else:
                raise

    def _cleanup_future_best_effort(self, future: Future[Any], context: str) -> None:
        """Best-effort cleanup for a failed/cancelled future.

        Parameters
        ----------
        future : Future[Any]
            The future to clean up.
        context : str
            Context string for logging (e.g., "timeout", "cancellation").
        """
        try:
            future.cancel()
        except Exception:  # pragma: no cover  # noqa: BLE001 - defensive
            logger.debug(
                "Failed to cancel BLE future after %s",
                context,
                exc_info=True,
            )
        # Consume any late exceptions to avoid "Task exception was never retrieved"
        try:
            future.add_done_callback(
                lambda f: f.exception() if not f.cancelled() else None
            )  # pragma: no cover - best effort suppression
        except Exception:  # noqa: BLE001 - best effort
            # Event loop may be closing; ignore best-effort callback registration
            logger.debug(
                "Skipping callback registration after %s",
                context,
                exc_info=True,
            )

    @staticmethod
    def _close_coroutine_safely(coro: Coroutine[Any, Any, Any]) -> None:
        """Best-effort close to suppress never-awaited coroutine warnings."""
        with contextlib.suppress(Exception):
            coro.close()

    def _async_await(
        self, coro: Coroutine[Any, Any, Any], timeout: float | None = None
    ) -> Any:
        """Wait for a coroutine scheduled on the shared BLE event loop and return its result.

        Parameters
        ----------
        coro : Coroutine[Any, Any, Any]
            The coroutine to run on the shared BLE event loop.
        timeout : float | None
            Maximum seconds to wait for completion; `None` means wait indefinitely. (Default value = None)

        Returns
        -------
        Any
            The value produced by the completed coroutine.

        Raises
        ------
        BLEError
            If the client is closed, the wait times out, the operation is cancelled,
            or the async operation fails.
        RuntimeError
            If the event loop is closed or cannot be accessed.
        """
        # Hold close lock so close() cannot interleave between closed-check,
        # scheduling, and pending-future registration.
        with self._close_lock:
            if self._closed:
                self._close_coroutine_safely(coro)
                raise self.BLEError(BLECLIENT_ERROR_CANNOT_SCHEDULE_CLOSED)
            runner_thread = getattr(self._runner, "_thread", None)
            if runner_thread is current_thread():
                self._close_coroutine_safely(coro)
                raise self.BLEError(BLECLIENT_ERROR_RUNNER_THREAD_WAIT)

            # Exception mapping contract:
            #   - FutureTimeoutError -> self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT)
            #   - FutureCancelledError / asyncio.CancelledError ->
            #       self.BLEError(BLECLIENT_ERROR_CANCELLED)
            #   - Bleak* exceptions propagate so interface wrappers can convert them consistently.
            future = self._async_run(coro)
            self._with_pending_futures(
                lambda pending_futures: pending_futures.add(future)
            )
        try:
            # On macOS, CoreBluetooth requires occasional I/O operations for
            # callbacks to be properly delivered. Without debug logging, no I/O
            # was happening, causing callbacks to never be processed.
            # TODO: Track and remove once the upstream Bleak CoreBluetooth callback
            # starvation issue is fixed and released:
            # https://github.com/hbldh/bleak/issues?q=is%3Aissue+CoreBluetooth+callback
            # Limitation: this only helps when stdout exists and supports flush();
            # redirected/non-flushable outputs (e.g., /dev/null wrappers) may not
            # provide the I/O nudge needed for callback progress.
            if sys.platform == "darwin":
                stdout = getattr(sys, "stdout", None)
                if stdout is None:
                    _log_macos_stdout_workaround_unavailable_once(
                        "stdout is unavailable"
                    )
                elif not hasattr(stdout, "flush"):
                    _log_macos_stdout_workaround_unavailable_once(
                        "stdout is not flushable"
                    )
                elif getattr(stdout, "closed", False):
                    _log_macos_stdout_workaround_unavailable_once("stdout is closed")
                else:
                    with contextlib.suppress(ValueError, OSError, AttributeError):
                        stdout.flush()
            return future.result(timeout)
        except SystemExit:  # pylint: disable=W0706
            raise
        except KeyboardInterrupt:  # pylint: disable=W0706
            raise
        except FutureTimeoutError as e:
            self._cleanup_future_best_effort(future, "timeout")
            raise self.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT) from e
        except FutureCancelledError as e:
            self._cleanup_future_best_effort(future, "cancellation")
            raise self.BLEError(BLECLIENT_ERROR_CANCELLED) from e
        except RuntimeError as e:
            # RuntimeError here typically indicates loop shutdown/closure, not a timeout.
            self._cleanup_future_best_effort(future, "runtime error")
            raise self.BLEError(BLECLIENT_ERROR_ASYNC_OPERATION_FAILED.format(e)) from e
        except asyncio.CancelledError as e:
            # Defensive coverage: Future.result() typically raises
            # concurrent.futures.CancelledError (handled above), but this keeps
            # direct coroutine cancellation mapping consistent.
            raise self.BLEError(BLECLIENT_ERROR_CANCELLED) from e
        finally:
            self._with_pending_futures(
                lambda pending_futures: pending_futures.discard(future)
            )

    # COMPAT_STABLE_SHIM (2.7.7): historical public BLEClient API.
    # Keep callable without deprecation warning.
    def async_await(
        self, coro: Coroutine[Any, Any, Any], timeout: float | None = None
    ) -> Any:
        """Backward-compatible wrapper for legacy BLEClient async_await().

        Parameters
        ----------
        coro : Coroutine[Any, Any, Any]
            Coroutine to run on the shared BLE event loop.
        timeout : float | None
            Maximum seconds to wait for completion; `None` means wait indefinitely.
            (Default value = None)

        Returns
        -------
        Any
            The value produced by the completed coroutine.
        """
        return self._async_await(coro, timeout=timeout)

    def _async_run(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        """Schedule a coroutine to run on the shared BLE event loop.

        Parameters
        ----------
        coro : Coroutine[Any, Any, Any]
            Coroutine to schedule on the shared BLE event loop.

        Returns
        -------
        Future[Any]
            Future representing the scheduled coroutine's eventual result.

        Raises
        ------
        BLEClient.BLEError
            If the BLEClient is closed or the coroutine cannot be scheduled.
        BLEError
            If the event loop is not running or scheduling fails.
        """
        with self._close_lock:
            if self._closed:
                self._close_coroutine_safely(coro)
                raise self.BLEError(BLECLIENT_ERROR_CANNOT_SCHEDULE_CLOSED)
            try:
                return self._runner._run_coroutine_threadsafe(coro)
            except RuntimeError as e:
                self._close_coroutine_safely(coro)
                raise self.BLEError(BLECLIENT_ERROR_FAILED_TO_SCHEDULE.format(e)) from e

    # COMPAT_STABLE_SHIM (2.7.7): historical public BLEClient API.
    # Keep callable without deprecation warning.
    def async_run(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        """Backward-compatible wrapper for legacy BLEClient async_run().

        Parameters
        ----------
        coro : Coroutine[Any, Any, Any]
            Coroutine to schedule on the shared BLE event loop.

        Returns
        -------
        Future[Any]
            Future representing the scheduled coroutine.
        """
        return self._async_run(coro)

    def _with_pending_futures(
        self, operation: Callable[[weakref.WeakSet[Future[Any]]], None]
    ) -> None:
        """Run an operation against `_pending_futures` under its lock when available."""
        pending_futures = getattr(self, "_pending_futures", None)
        if pending_futures is None:
            return
        pending_futures_lock = getattr(self, "_pending_futures_lock", None)
        if pending_futures_lock is not None:
            with pending_futures_lock:
                operation(pending_futures)
            return
        operation(pending_futures)
