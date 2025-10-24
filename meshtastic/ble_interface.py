"""Bluetooth interface."""

import asyncio
import atexit
import contextlib
import importlib.metadata
import io
import logging
import random
import re
import struct
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from enum import Enum
from queue import Empty
from threading import Event, Lock, RLock, Thread, current_thread
from typing import List, Optional, Tuple

from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner, BLEDevice
from bleak.exc import BleakDBusError, BleakError
from google.protobuf.message import DecodeError

from meshtastic import publishingThread
from meshtastic.mesh_interface import MeshInterface

from .protobuf import mesh_pb2

# Get bleak version using importlib.metadata (reliable method)
BLEAK_VERSION = importlib.metadata.version("bleak")

SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"
LEGACY_LOGRADIO_UUID = "6c6fd238-78fa-436b-aacf-15c5be1ef2e2"
LOGRADIO_UUID = "5a3d6e49-06e6-4423-9944-e9de8cdf9547"
MALFORMED_NOTIFICATION_THRESHOLD = 10
logger = logging.getLogger("meshtastic.ble")

"""
Exception Handling Philosophy for BLE Interface

This interface follows a structured approach to exception handling:

1. **Expected, recoverable failures**: Caught, logged, and operation continues
   - Corrupted protobuf packets (DecodeError) - discarded gracefully
   - Optional notification failures (log notifications) - logged but non-critical
   - Temporary BLE disconnections - handled by auto-reconnect logic

2. **Expected, non-recoverable failures**: Caught, logged, and re-raised or handled gracefully
   - Connection establishment failures - clear error messages to user
   - Service discovery failures - retry with fallback mechanisms

3. **Critical failures**: Let bubble up to notify developers
   - FROMNUM_UUID notification setup failure - essential for packet reception
   - Unexpected BLE errors during critical operations

4. **Cleanup operations**: Failures are logged but don't raise exceptions
   - Thread shutdown, client close operations - best-effort cleanup

This approach ensures the library is resilient to expected wireless communication
issues while making critical failures visible to developers.
"""

DISCONNECT_TIMEOUT_SECONDS = 5.0
RECEIVE_THREAD_JOIN_TIMEOUT = (
    2.0  # Bounded join keeps shutdown responsive for CLI users and tests
)
EVENT_THREAD_JOIN_TIMEOUT = (
    2.0  # Matches receive thread join window for consistent teardown
)

# BLE timeout and retry constants
BLE_SCAN_TIMEOUT = 10.0
RECEIVE_WAIT_TIMEOUT = 0.5
EMPTY_READ_RETRY_DELAY = 0.1
EMPTY_READ_MAX_RETRIES = 5
TRANSIENT_READ_MAX_RETRIES = 3
TRANSIENT_READ_RETRY_DELAY = 0.1
SEND_PROPAGATION_DELAY = 0.01
GATT_IO_TIMEOUT = 10.0
NOTIFICATION_START_TIMEOUT = 10.0
CONNECTION_TIMEOUT = 60.0
AUTO_RECONNECT_INITIAL_DELAY = 1.0
AUTO_RECONNECT_MAX_DELAY = 30.0
AUTO_RECONNECT_BACKOFF = 2.0
AUTO_RECONNECT_JITTER_RATIO = (
    0.15  # ±15% jitter prevents reconnect stampedes across hosts
)

BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION: Tuple[int, int, int] = (1, 1, 0)

# Error message constants
ERROR_READING_BLE = "Error reading BLE"
ERROR_NO_PERIPHERAL_FOUND = "No Meshtastic BLE peripheral with identifier or address '{0}' found. Try --ble-scan to find it."

ERROR_WRITING_BLE = (
    "Error writing BLE. This is often caused by missing Bluetooth "
    "permissions (e.g. not being in the 'bluetooth' group) or pairing issues."
)
ERROR_CONNECTION_FAILED = "Connection failed: {0}"
ERROR_NO_PERIPHERALS_FOUND = (
    "No Meshtastic BLE peripherals found. Try --ble-scan to find them."
)

# BLEClient-specific constants
BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT = (
    2.0  # Ensures client.close() does not block shutdown indefinitely
)
BLECLIENT_ERROR_ASYNC_TIMEOUT = "Async operation timed out"


def _parse_version_triplet(version_str: str) -> Tuple[int, int, int]:
    """
    Extract a three-part integer version tuple from `version_str`.

    This helper is intentionally permissive — non-numeric segments are ignored and
    missing components are treated as zeros.
    """
    matches = re.findall(r"\d+", version_str or "")
    while len(matches) < 3:
        matches.append("0")
    try:
        return tuple(int(segment) for segment in matches[:3])  # type: ignore[return-value]
    except ValueError:
        return 0, 0, 0


def _bleak_supports_connected_fallback() -> bool:
    """
    Determine whether the installed bleak version supports the connected-device fallback.
    """
    return (
        _parse_version_triplet(BLEAK_VERSION)
        >= BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION
    )


# TODO: ConnectionState enum is part of planned future refactoring to implement
# a proper state machine for connection management (see ble_refactoring_plan.md).
# Currently unused but retained for future implementation.
class ConnectionState(Enum):
    """Enum for managing BLE connection states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class ThreadCoordinator:
    """
    Simplified thread management for BLE operations.

    This class provides centralized thread and event management for BLE interface
    operations. It ensures proper cleanup, prevents resource leaks, and provides
    a consistent API for thread coordination patterns.

    Features:
        - Thread lifecycle management (create, start, join, cleanup)
        - Event coordination for thread synchronization
        - Automatic resource cleanup on shutdown
        - Thread-safe operations with RLock
        - Helper methods for common coordination patterns
    """

    def __init__(self):
        """
        Create a ThreadCoordinator used to track and manage threads and events.

        Initializes:
            _lock (RLock): reentrant lock protecting internal state.
            _threads (List[Thread]): list of tracked Thread objects.
            _events (dict[str, Event]): mapping of event names to threading.Event objects for coordination.
        """
        self._lock = RLock()
        self._threads: List[Thread] = []
        self._events: dict[str, Event] = {}

    def create_thread(
        self, target, name: str, *, daemon: bool = True, args=(), kwargs=None
    ) -> Thread:
        """
        Create and register a new Thread with target, name, daemon, args, and kwargs.

        Args:
            target (callable): The callable to be executed by the thread.
            name (str): The thread's name.
            daemon (bool): Whether the thread should be a daemon thread.
            args (tuple): Positional arguments to pass to `target`.
            kwargs (dict): Keyword arguments to pass to `target`.

        Returns:
            Thread: The created Thread instance (added to the coordinator's tracked threads, not started).

        """
        with self._lock:
            thread = Thread(
                target=target, name=name, daemon=daemon, args=args, kwargs=kwargs
            )
            self._threads.append(thread)
            return thread

    def create_event(self, name: str) -> Event:
        """
        Create and register a new Event with the specified name.

        Args:
            name (str): Key under which the event will be stored and retrievable.

        Returns:
            Event: The newly created Event instance.

        """
        with self._lock:
            event = Event()
            self._events[name] = event
            return event

    def get_event(self, name: str) -> Optional[Event]:
        """
        Retrieve a previously created Event by the specified name.

        Args:
            name (str): The identifier of the tracked event.

        Returns:
            Optional[Event]: The Event instance if found, otherwise `None`.

        """
        with self._lock:
            return self._events.get(name)

    def start_thread(self, thread: Thread):
        """
        Start the given thread if it is tracked by this coordinator.

        Only threads previously added to the coordinator's tracking list will be started; otherwise the call has no effect.
        """
        with self._lock:
            if thread in self._threads:
                thread.start()

    def join_thread(self, thread: Thread, timeout: Optional[float] = None):
        """
        Join a tracked thread with the specified thread and timeout.

        Args:
                thread (Thread): The thread to join; only joined if it is currently tracked and alive.
                timeout (float | None): Maximum seconds to wait for the thread to finish. If None, wait indefinitely.

        """
        with self._lock:
            if (
                thread in self._threads
                and thread.is_alive()
                and thread is not current_thread()
            ):
                thread.join(timeout=timeout)

    def join_all(self, timeout: Optional[float] = None):
        """
        Wait for all tracked threads with the specified timeout.

        Args:
            timeout (Optional[float]): Maximum number of seconds to wait for each thread to join.
                If `None`, wait indefinitely for each thread.

        """
        with self._lock:
            current = current_thread()
            for thread in self._threads:
                if thread.is_alive() and thread is not current:
                    thread.join(timeout=timeout)

    def set_event(self, name: str):
        """
        Set the tracked event with the specified name.

        If no event exists with that name, this is a no-op.

        Args:
            name (str): Name of the tracked event to set.

        Returns:
            None.

        """
        with self._lock:
            if name in self._events:
                self._events[name].set()

    def clear_event(self, name: str):
        """
        Clear the tracked event with the specified name.

        If an event with `name` is being tracked, clear its internal flag; otherwise do nothing.

        Args:
        ----
            name (str): The identifier of the tracked event to clear.

        """
        with self._lock:
            if name in self._events:
                self._events[name].clear()

    def wait_for_event(self, name: str, timeout: Optional[float] = None) -> bool:
        """
        Wait until the named tracked event is set or until the timeout elapses.

        Args:
        ----
            name (str): Name of the tracked event to wait for.
            timeout (Optional[float]): Maximum time in seconds to wait; None means wait indefinitely.

        Returns:
        -------
            bool: True if the event was set before the timeout, False otherwise or if the event is not tracked.

        """
        event = self.get_event(name)
        if event:
            return event.wait(timeout=timeout)
        return False

    def check_and_clear_event(self, name: str) -> bool:
        """
        Check whether a tracked event is set and clear it if set.

        Args:
        ----
            name (str): The tracked event's name.

        Returns:
        -------
            bool: `True` if the event was set (and was cleared), `False` otherwise.

        """
        event = self.get_event(name)
        if event and event.is_set():
            event.clear()
            return True
        return False

    def wake_waiting_threads(self, *event_names: str):
        """
        Set the named events so any threads waiting on them are woken.

        Args:
        ----
            event_names (str): One or more names of events previously created via create_event; each named event will be set.

        """
        for name in event_names:
            self.set_event(name)

    def clear_events(self, *event_names: str):
        """
        Clear multiple tracked events by name.

        Args:
        ----
            event_names (str): One or more event names to clear; names that are not tracked are ignored.

        """
        for name in event_names:
            self.clear_event(name)

    def cleanup(self):
        """
        Signal all tracked events, join and stop tracked threads, and clear coordinator state.

        Sets every tracked Event, joins each tracked Thread (except current) with a short timeout,
        then clears internal thread and event registries so the coordinator no longer tracks them.
        """
        with self._lock:
            # Signal all events
            for event in self._events.values():
                event.set()

            # Join threads with timeout (except current thread)
            current = current_thread()
            for thread in self._threads:
                if thread.is_alive() and thread is not current:
                    thread.join(timeout=EVENT_THREAD_JOIN_TIMEOUT)

            # Clear tracking
            self._threads.clear()
            self._events.clear()


class BLEErrorHandler:
    """
    Helper class for consistent error handling in BLE operations.

    This class provides static methods for standardized error handling patterns
    throughout the BLE interface. It centralizes error logging and recovery strategies.

    Features:
        - Safe execution with fallback return values
        - Consistent error logging and classification
        - Cleanup operations that never raise exceptions
    """

    @staticmethod
    def safe_execute(
        func,
        default_return=None,
        log_error: bool = True,
        error_msg: str = "Error in operation",
        reraise: bool = False,
    ):
        """
        Execute a callable and return its result while converting handled exceptions into a default value.

        Args:
        ----
            func (callable): A zero-argument callable to execute.
            default_return: Value to return if execution fails; defaults to None.
            log_error (bool): If True, log caught exceptions; defaults to True.
            error_msg (str): Message used when logging errors; defaults to "Error in operation".
            reraise (bool): If True, re-raise any caught exception instead of returning default_return.

        Returns:
        -------
            The value returned by `func()` on success, or `default_return` if a handled exception occurs.

        Raises:
        ------
            Exception: Re-raises the original exception if `reraise` is True.

        Notes:
        -----
            Handled exceptions include BleakError, BleakDBusError, DecodeError, and FutureTimeoutError;
        all other exceptions are also caught and treated the same.

        """
        try:
            return func()
        except (BleakError, BleakDBusError, DecodeError, FutureTimeoutError) as e:
            if log_error:
                logger.debug("%s: %s", error_msg, e)
            if reraise:
                raise
            return default_return
        except Exception:
            if log_error:
                logger.exception("%s", error_msg)
            if reraise:
                raise
            return default_return

    @staticmethod
    def safe_cleanup(func, cleanup_name: str = "cleanup operation"):
        """Safely execute cleanup operations without raising exceptions."""
        try:
            func()
        except Exception as e:
            logger.debug("Error during %s: %s", cleanup_name, e)


class BLEInterface(MeshInterface):
    """
    MeshInterface using BLE to connect to Meshtastic devices.

    This class provides a complete BLE interface for Meshtastic communication,
    handling connection management, packet transmission/reception, error recovery,
    and automatic reconnection. It extends MeshInterface with BLE-specific
    functionality while maintaining API compatibility.

    Key Features:
        - Automatic connection management and recovery
        - Thread-safe operations with centralized thread coordination
        - Unified error handling and logging
        - Configurable timeouts and retry behavior
        - Support for both legacy and modern BLE characteristics
        - Comprehensive state management

    Architecture:
        - ThreadCoordinator: Centralized thread and event management
        - Module-level constants: Configurable timeouts, UUIDs, and error messages
        - BLEErrorHandler: Standardized error handling patterns
        - ConnectionState: Enum-based state tracking

    Note: This interface requires appropriate Bluetooth permissions and may
    need platform-specific setup for BLE operations.
    """

    # Lock acquisition order (outermost first): _connect_lock -> _client_lock -> _closing_lock.
    # Maintaining this order prevents deadlocks when multiple locks are needed.

    class BLEError(Exception):
        """An exception class for BLE errors."""

    def __init__(  # pylint: disable=R0917
        self,
        address: Optional[str],
        noProto: bool = False,
        debugOut: Optional[io.TextIOWrapper] = None,
        noNodes: bool = False,
        timeout: int = 300,
        *,
        auto_reconnect: bool = True,
    ) -> None:
        """
        Initialize a BLEInterface, start its background receive thread, and attempt an initial connection to a Meshtastic BLE device.

        Args:
        ----
            address (Optional[str]): BLE address to connect to; if None, any available Meshtastic device may be used.
            noProto (bool): If True, do not initialize protobuf-based protocol handling.
            debugOut (Optional[io.TextIOWrapper]): Stream to emit debug output to, if provided.
            noNodes (bool): If True, do not attempt to read the device's node list on startup.
            auto_reconnect (bool): If True, keep the interface alive across unexpected disconnects and attempt
                automatic reconnection; if False, close the interface on disconnect.
            timeout (int): How long to wait for replies (default: 300 seconds).

        """

        # Thread safety and state management
        # Note: We maintain three separate locks instead of a single RLock as originally planned
        # to preserve the current battle-tested lock hierarchy and minimize refactoring risk.
        # Future consolidation to a single lock may be considered in a later phase.
        self._closing_lock: Lock = Lock()  # Prevents concurrent close operations
        self._client_lock: Lock = Lock()  # Protects client access during reconnection
        self._connect_lock: Lock = Lock()  # Prevents concurrent connection attempts
        self._closing: bool = False  # Indicates shutdown in progress
        self._closed: bool = (
            False  # Tracks completion of shutdown for idempotent close()
        )
        self._exit_handler = None
        self.address = address
        self._last_connection_request: Optional[str] = BLEInterface._sanitize_address(
            address
        )
        self.auto_reconnect = auto_reconnect
        self._disconnect_notified = False  # Prevents duplicate disconnect events

        # Error handling infrastructure
        self.error_handler = BLEErrorHandler()

        # Thread management infrastructure
        self.thread_coordinator = ThreadCoordinator()

        # Event coordination for reconnection and read operations
        self._read_trigger = self.thread_coordinator.create_event(
            "read_trigger"
        )  # Signals when data is available to read
        self._reconnected_event = self.thread_coordinator.create_event(
            "reconnected_event"
        )  # Signals when reconnection occurred
        self._malformed_notification_count = 0  # Tracks corrupted packets for threshold
        self._reconnect_thread: Optional[Thread] = None

        # Initialize parent interface
        MeshInterface.__init__(
            self, debugOut=debugOut, noProto=noProto, noNodes=noNodes, timeout=timeout
        )

        # Initialize retry counter for transient read errors
        self._read_retry_count = 0

        # Start background receive thread for inbound packet processing
        logger.debug("Threads starting")
        self._want_receive = True
        self._receiveThread: Optional[Thread] = self.thread_coordinator.create_thread(
            target=self._receiveFromRadioImpl, name="BLEReceive", daemon=True
        )
        self.thread_coordinator.start_thread(self._receiveThread)
        logger.debug("Threads running")

        self.client: Optional["BLEClient"] = None
        try:
            logger.debug("BLE connecting to: %s", address if address else "any")
            client = self.connect(address)
            with self._client_lock:
                self.client = client
            logger.debug("BLE connected")

            logger.debug("Mesh configure starting")
            self._startConfig()
            if not self.noProto:
                self._waitConnected(timeout=CONNECTION_TIMEOUT)
                self.waitForConfig()

            # FROMNUM notification is set in _register_notifications

            # We MUST run atexit (if we can) because otherwise (at least on linux) the BLE device is not disconnected
            # and future connection attempts will fail.  (BlueZ kinda sucks)
            # Note: the on disconnected callback will call our self.close which will make us nicely wait for threads to exit
            self._exit_handler = atexit.register(self.close)
        except Exception as e:
            self.close()
            if isinstance(e, BLEInterface.BLEError):
                raise
            raise BLEInterface.BLEError(ERROR_CONNECTION_FAILED.format(e)) from e

    def __repr__(self):
        """
        Produce a compact textual representation of the BLEInterface including its address and relevant feature flags.

        Returns:
            repr_str (str): A string of the form "BLEInterface(...)" that includes the address, `debugOut` if set,
                and boolean flags for `noProto`, `noNodes`, and `auto_reconnect` when applicable.

        """
        parts = [f"address={self.address!r}"]
        if self.debugOut is not None:
            parts.append(f"debugOut={self.debugOut!r}")
        if self.noProto:
            parts.append("noProto=True")
        if self.noNodes:
            parts.append("noNodes=True")
        if not self.auto_reconnect:
            parts.append("auto_reconnect=False")
        return f"BLEInterface({', '.join(parts)})"

    def _handle_disconnect(
        self,
        source: str,
        client: Optional["BLEClient"] = None,
        bleak_client: Optional[BleakRootClient] = None,
    ) -> bool:
        """
        Handle a BLE client disconnection and initiate either reconnection or shutdown.

        Args:
        ----
            source (str): Short tag indicating the origin of the disconnect (e.g., "bleak_callback", "read_loop", "explicit").
            client (Optional[BLEClient]): The BLEClient instance related to the disconnect, if available.
            bleak_client (Optional[BleakRootClient]): The underlying Bleak client associated with the disconnect, if available.

        Returns:
        -------
            bool: `true` if the interface should remain running and (when enabled) attempt auto-reconnect,
              `false` if the interface has begun shutdown.

        """
        # Ignore disconnects during shutdown to avoid race conditions
        if self._closing:
            logger.debug("Ignoring disconnect from %s during shutdown.", source)
            return False

        # Determine which client we're dealing with
        target_client = client
        if not target_client and bleak_client:
            # Find the BLEClient that contains this bleak_client
            target_client = self.client
            if (
                target_client
                and getattr(target_client, "bleak_client", None) is not bleak_client
            ):
                target_client = None

        address = "unknown"
        if target_client:
            address = getattr(target_client, "address", repr(target_client))
        elif bleak_client:
            address = getattr(bleak_client, "address", repr(bleak_client))

        logger.debug("BLE client %s disconnected (source: %s).", address, source)

        if self.auto_reconnect:
            previous_client = None
            with self._client_lock:
                # Prevent duplicate disconnect notifications
                if self._disconnect_notified:
                    logger.debug("Ignoring duplicate disconnect from %s.", source)
                    return True

                current_client = self.client
                # Ignore stale client disconnects (from previous connections)
                if (
                    target_client
                    and current_client
                    and target_client is not current_client
                ):
                    logger.debug("Ignoring stale disconnect from %s.", source)
                    return True

                previous_client = current_client
                self.client = None
                self._disconnect_notified = True

            # Notify parent interface of disconnection
            if previous_client:
                self._disconnected()
                # Close previous client asynchronously
                close_thread = self.thread_coordinator.create_thread(
                    target=self._safe_close_client,
                    args=(previous_client,),
                    name="BLEClientClose",
                    daemon=True,
                )
                self.thread_coordinator.start_thread(close_thread)

            # Event coordination for reconnection
            self.thread_coordinator.clear_events("read_trigger", "reconnected_event")
            self._schedule_auto_reconnect()
            return True
        else:
            # Auto-reconnect disabled - close the interface
            logger.debug("Auto-reconnect disabled, closing interface.")
            self.close()
            return False

    def _on_ble_disconnect(self, client: BleakRootClient) -> None:
        """
        Handle a Bleak client disconnection callback from bleak.

        This is a wrapper around the unified _handle_disconnect method for callback
        notifications from the BLE library.

        Args:
        ----
            client (BleakRootClient): The Bleak client instance that triggered the disconnect callback.

        """
        self._handle_disconnect("bleak_callback", bleak_client=client)

    def _schedule_auto_reconnect(self) -> None:
        """
        Start a background thread that repeatedly attempts to reconnect over BLE until a connection succeeds or shutdown is initiated.

        This schedules a single auto-reconnect worker (no-op if auto-reconnect is disabled, the interface is closing,
        or a reconnect thread is already running). The worker repeatedly tries to connect to the stored address,
        backing off with jitter between attempts, and stops when a connection succeeds, auto-reconnect is disabled,
        or the interface is closing. When the worker exits it clears the internal reconnect-thread reference.
        """

        if not self.auto_reconnect:
            return
        if self._closing:
            logger.debug(
                "Skipping auto-reconnect scheduling because interface is closing."
            )
            return

        with self._client_lock:
            existing_thread = self._reconnect_thread
            if existing_thread and existing_thread.is_alive():
                logger.debug(
                    "Auto-reconnect already in progress; skipping new attempt."
                )
                return

            def _attempt_reconnect() -> None:
                """
                Attempt to re-establish a BLE connection in a background reconnect loop.

                Keeps trying to connect to the interface's target address until a connection succeeds, the interface
                is closed, or auto-reconnect is disabled. Retries use exponential backoff with jitter between attempts
                and log each outcome. When the loop exits, clears the internal reconnect-thread tracker.
                """
                delay = AUTO_RECONNECT_INITIAL_DELAY
                try:
                    while True:
                        if self._closing or not self.auto_reconnect:
                            logger.debug(
                                "Auto-reconnect aborted because interface is closing or disabled."
                            )
                            return
                        try:
                            logger.debug("Attempting BLE auto-reconnect.")
                            self.connect(self.address)
                            logger.info("BLE auto-reconnect succeeded.")
                            return
                        except self.BLEError as err:
                            if self._closing or not self.auto_reconnect:
                                logger.debug(
                                    "Auto-reconnect cancelled after failure due to shutdown/disable."
                                )
                                return
                            logger.warning("Auto-reconnect attempt failed: %s", err)
                        except Exception:
                            if self._closing or not self.auto_reconnect:
                                logger.debug(
                                    "Auto-reconnect cancelled after unexpected failure due to shutdown/disable."
                                )
                                return
                            logger.exception(
                                "Unexpected error during auto-reconnect attempt"
                            )

                        if self._closing or not self.auto_reconnect:
                            return
                        jitter = AUTO_RECONNECT_JITTER_RATIO * (
                            (random.random() * 2.0) - 1.0
                        )
                        sleep_seconds = delay * (1.0 + jitter)
                        time.sleep(sleep_seconds)
                        delay = min(
                            delay * AUTO_RECONNECT_BACKOFF, AUTO_RECONNECT_MAX_DELAY
                        )
                finally:
                    with self._client_lock:
                        if self._reconnect_thread is current_thread():
                            self._reconnect_thread = None

            thread = self.thread_coordinator.create_thread(
                target=_attempt_reconnect,
                name="BLEAutoReconnect",
                daemon=True,
            )
            self._reconnect_thread = thread

        self.thread_coordinator.start_thread(thread)

    def _handle_malformed_fromnum(self, reason: str, exc_info: bool = False):
        """
        Increment and track malformed FROMNUM notification occurrences and emit a warning when a threshold is reached.

        Increments the internal malformed-notification counter, logs the provided reason (optionally with exception info),
        and when the counter reaches MALFORMED_NOTIFICATION_THRESHOLD logs a warning and resets the counter to zero.

        Args:
        ----
                reason (str): Human-readable message describing why the notification was considered malformed.
                exc_info (bool): If True, include exception traceback information in the debug log.

        """
        self._malformed_notification_count += 1
        logger.debug("%s", reason, exc_info=exc_info)
        if self._malformed_notification_count >= MALFORMED_NOTIFICATION_THRESHOLD:
            logger.warning(
                "Received %d malformed FROMNUM notifications. Check BLE connection stability.",
                self._malformed_notification_count,
            )
            self._malformed_notification_count = 0

    def from_num_handler(self, _, b: bytearray) -> None:  # pylint: disable=C0116
        """
        Handle notifications from the FROMNUM characteristic and wake the read loop.

        Parses a 4-byte little-endian unsigned integer from the notification payload `b`. On a successful parse, resets the
        malformed-notification counter and logs the parsed value. On parse failure, increments the malformed-notification
        counter and logs; if the counter reaches config.malformed_notification_threshold, emits a warning and resets the
        counter. Always sets self._read_trigger to signal the read loop.

        Args:
        ----
            _ (Any): Unused sender/handle parameter supplied by the BLE library.
            b (bytearray): Notification payload expected to be exactly 4 bytes containing a little-endian unsigned 32-bit integer.

        """
        try:
            if len(b) != 4:
                self._handle_malformed_fromnum(
                    f"FROMNUM notify has unexpected length {len(b)}; ignoring"
                )
                return
            from_num = struct.unpack("<I", b)[0]
            logger.debug("FROMNUM notify: %d", from_num)
            # Successful parse: reset malformed counter
            self._malformed_notification_count = 0
        except (struct.error, ValueError):
            self._handle_malformed_fromnum(
                "Malformed FROMNUM notify; ignoring", exc_info=True
            )
            return
        finally:
            self.thread_coordinator.set_event("read_trigger")

    def _register_notifications(self, client: "BLEClient") -> None:
        """
        Register BLE characteristic notification handlers on the given client.

        Registers optional log notification handlers for legacy and current log characteristics; failures to start these optional
        handlers are caught and logged at debug level. Also registers the critical FROMNUM notification handler for
        incoming packets — failures to register this notification are not suppressed and will propagate.

        Args:
        ----
            client (BLEClient): Connected BLE client to register notifications on.

        """

        # Optional log notifications - failures are not critical
        def _start_log_notifications():
            """
            Register log notification handlers for available radio log characteristics on the current client.

            If the legacy text log characteristic is present, subscribes the legacy log handler; if the protobuf-based
            log characteristic is present, subscribes the protobuf log handler.
            """
            if client.has_characteristic(LEGACY_LOGRADIO_UUID):
                client.start_notify(
                    LEGACY_LOGRADIO_UUID,
                    self.legacy_log_radio_handler,
                    timeout=NOTIFICATION_START_TIMEOUT,
                )
            if client.has_characteristic(LOGRADIO_UUID):
                client.start_notify(
                    LOGRADIO_UUID,
                    self.log_radio_handler,
                    timeout=NOTIFICATION_START_TIMEOUT,
                )

        # Start optional log notifications; failures here are non-fatal.
        try:
            _start_log_notifications()
        except (BleakError, BleakDBusError, RuntimeError) as e:
            logger.debug("Failed to start optional log notifications: %s", e)

        # Critical notification for packet ingress
        client.start_notify(
            FROMNUM_UUID,
            self.from_num_handler,
            timeout=NOTIFICATION_START_TIMEOUT,
        )

    def log_radio_handler(self, _, b: bytearray) -> None:  # pylint: disable=C0116
        """
        Handle a protobuf LogRecord notification and forward a formatted log line to the instance log handler.

        Parses the notification payload as a `mesh_pb2.LogRecord`. If the record contains a `source`, the forwarded line is prefixed
        with `[source] `; otherwise the record `message` is forwarded as-is. Malformed records are logged as a warning
        and ignored.

        Args:
        ----
            _ (Any): Unused sender/handle parameter supplied by the BLE library.
            b (bytearray): Serialized `mesh_pb2.LogRecord` payload from the BLE notification.

        """
        log_record = mesh_pb2.LogRecord()
        try:
            log_record.ParseFromString(bytes(b))

            message = (
                f"[{log_record.source}] {log_record.message}"
                if log_record.source
                else log_record.message
            )
            self._handleLogLine(message)
        except DecodeError:
            logger.warning("Malformed LogRecord received. Skipping.")

    def legacy_log_radio_handler(self, _, b: bytearray) -> None:
        """
        Handle a legacy log-radio notification by decoding and forwarding the UTF-8 log line.

        Decodes `b` as UTF-8, strips newline characters, and passes the resulting string to `self._handleLogLine`.
        If `b` is not valid UTF-8, a warning is logged and the notification is ignored.

        Args:
        ----
            _ (Any): Unused sender/handle parameter supplied by the BLE library.
            b (bytearray): Raw notification payload expected to contain a UTF-8 encoded log line.

        """
        try:
            log_radio = b.decode("utf-8").replace("\n", "")
            self._handleLogLine(log_radio)
        except UnicodeDecodeError:
            logger.warning(
                "Malformed legacy LogRecord received (not valid utf-8). Skipping."
            )

    @staticmethod
    async def _with_timeout(awaitable, timeout: Optional[float], label: str):
        """
        Await `awaitable`, enforcing `timeout` seconds if provided.

        Raises:
            BLEInterface.BLEError: when the awaitable does not finish before the timeout elapses.

        """
        if timeout is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BLEInterface.BLEError(
                f"{label} timed out after {timeout:.1f} seconds"
            ) from exc

    @staticmethod
    def scan() -> List[BLEDevice]:
        """
        Scan for BLE devices advertising the Meshtastic service UUID.

        Performs a timed BLE scan, handles variations in BleakScanner.discover() return formats, and returns devices whose
        advertisements include the Meshtastic service UUID.

        Returns:
            List[BLEDevice]: Devices whose advertisements include the Meshtastic service UUID; empty list if none are found.

        """
        with BLEClient() as client:
            logger.debug(
                "Scanning for BLE devices (takes %.0f seconds)...", BLE_SCAN_TIMEOUT
            )
            response = client.discover(
                timeout=BLE_SCAN_TIMEOUT, return_adv=True, service_uuids=[SERVICE_UUID]
            )

            devices: List[BLEDevice] = []
            # With return_adv=True, BleakScanner.discover() returns a dict
            if response is None:
                logger.warning("BleakScanner.discover returned None")
                return devices
            if not isinstance(response, dict):
                logger.warning(
                    "BleakScanner.discover returned unexpected type: %s",
                    type(response),
                )
                return devices
            for _, value in response.items():
                if isinstance(value, tuple):
                    device, adv = value
                else:
                    logger.warning(
                        "Unexpected return type from BleakScanner.discover: %s",
                        type(value),
                    )
                    continue
                suuids = getattr(adv, "service_uuids", None)
                if suuids and SERVICE_UUID in suuids:
                    devices.append(device)
            return devices

    def find_device(self, address: Optional[str]) -> BLEDevice:
        """
        Find the Meshtastic BLE device matching an optional address or device name.

        Args:
        ----
            address (Optional[str]): Address or device name to match; comparison ignores case and common separators
                (':', '-', '_', and spaces). If None, any discovered Meshtastic device may be returned.

        Returns:
        -------
            BLEDevice: The matched BLE device. If no address is provided and multiple devices are discovered,
                the first discovered device is returned.

        Raises:
        ------
            BLEInterface.BLEError: If no Meshtastic devices are found, or if an address was provided and multiple matching devices are found.

        """

        addressed_devices = BLEInterface.scan()

        # If scan didn't find any devices and we have a specific address, try fallback
        if not addressed_devices and address:
            logger.debug(
                "Scan found no devices, trying fallback to already-connected devices"
            )
            addressed_devices = self._find_connected_devices(address)

        if address:
            sanitized_address = BLEInterface._sanitize_address(address)
            if sanitized_address is None:
                logger.debug(
                    "Empty/whitespace address provided; treating as 'any device'"
                )
            else:
                filtered_devices = []
                for device in addressed_devices:
                    sanitized_name = BLEInterface._sanitize_address(device.name)
                    sanitized_device_address = BLEInterface._sanitize_address(
                        device.address
                    )
                    if sanitized_address in (sanitized_name, sanitized_device_address):
                        filtered_devices.append(device)
                addressed_devices = filtered_devices

        if len(addressed_devices) == 0:
            if address:
                raise self.BLEError(ERROR_NO_PERIPHERAL_FOUND.format(address))
            raise self.BLEError(ERROR_NO_PERIPHERALS_FOUND)
        if len(addressed_devices) == 1:
            return addressed_devices[0]
        if address and len(addressed_devices) > 1:
            # Build a list of found devices for the error message
            device_list = "\n".join(
                [f"- {d.name} ({d.address})" for d in addressed_devices]
            )
            raise self.BLEError(
                f"Multiple Meshtastic BLE peripherals found matching '{address}'. Please specify one:\n{device_list}"
            )
        # No specific address provided and multiple devices found, return the first one
        return addressed_devices[0]

    @staticmethod
    def _sanitize_address(address: Optional[str]) -> Optional[str]:
        """
        Normalize a BLE address by removing common separators and converting to lowercase.

        Args:
        ----
            address (Optional[str]): BLE address or identifier; may be None or empty/whitespace.

        Returns:
        -------
            Optional[str]: The normalized address with all "-", "_", ":" removed, trimmed of surrounding whitespace,
                and lowercased, or `None` if `address` is None or contains only whitespace.

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

    def _find_connected_devices(self, address: Optional[str]) -> List[BLEDevice]:
        """
        Fallback method to find already-connected Meshtastic devices when scanning fails.

        This method uses the system's Bluetooth adapter to check for devices that are
        already connected and have the Meshtastic service UUID, which may not be
        discoverable via normal scanning when already connected.

        Args:
        ----
            address (Optional[str]): Specific address to look for, if None returns all connected Meshtastic devices

        Returns:
        -------
            List[BLEDevice]: List of connected Meshtastic devices

        """
        if not _bleak_supports_connected_fallback():
            logger.debug(
                "Skipping fallback connected-device scan; bleak %s < %s",
                BLEAK_VERSION,
                ".".join(
                    str(part) for part in BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION
                ),
            )
            return []
        try:
            # Try to use BleakScanner to get cached device information
            # This works even when scanning fails due to adapter issues

            # NOTE: Using private API scanner._backend.get_devices() as bleak 1.1.1
            # doesn't provide a public API to enumerate already-connected devices.
            # BleakScanner.discover() only returns actively advertising devices,
            # which won't find already-connected devices that aren't broadcasting.
            # TODO: Revisit if bleak adds public support for connected device enumeration.
            # Trade-off: Using private API is unstable but provides needed fallback functionality.

            async def _get_devices_async_and_filter(
                address_to_find: Optional[str],
            ) -> List[BLEDevice]:
                """
                Retrieve connected Meshtastic BLE devices and optionally filter by address or name.

                Args:
                    address_to_find (Optional[str]): Bluetooth address or device name to match; separators (":", "-", "_",
                        spaces) are ignored when comparing. If None, returns all connected devices advertising the
                        Meshtastic service.

                Returns:
                -------
                    List[BLEDevice]: Devices advertising the Meshtastic service that match the optional filter, or an empty list if none found.

                """
                # Get devices from the scanner's cached data
                scanner = BleakScanner()
                devices_found = []

                # Try to get device information from the backend
                if hasattr(scanner, "_backend") and hasattr(
                    scanner._backend, "get_devices"
                ):
                    backend_devices = await BLEInterface._with_timeout(
                        scanner._backend.get_devices(),
                        BLE_SCAN_TIMEOUT,
                        "connected-device enumeration",
                    )

                    for device in backend_devices:
                        # Check if device has Meshtastic service UUID
                        if hasattr(device, "metadata") and device.metadata:
                            uuids = device.metadata.get("uuids", [])
                            if SERVICE_UUID in uuids:
                                # If specific address requested, filter by it
                                if address_to_find:
                                    sanitized_target = BLEInterface._sanitize_address(
                                        address_to_find
                                    )
                                    sanitized_addr = BLEInterface._sanitize_address(
                                        device.address
                                    )
                                    sanitized_name = (
                                        BLEInterface._sanitize_address(device.name)
                                        if device.name
                                        else ""
                                    )

                                    if sanitized_target in (
                                        sanitized_addr,
                                        sanitized_name,
                                    ):
                                        devices_found.append(
                                            BLEDevice(
                                                device.address,
                                                device.name,
                                                device.metadata,
                                            )
                                        )
                                else:
                                    # Add all connected Meshtastic devices
                                    devices_found.append(
                                        BLEDevice(
                                            device.address, device.name, device.metadata
                                        )
                                    )
                return devices_found

            # Library-friendly async execution: handle both cases where event loop is running or not
            # This pattern is necessary to support environments with existing event loops
            try:
                asyncio.get_running_loop()
                # A loop is running in this thread, run async code in separate thread
                # to avoid interference with the existing event loop
                future: Future[List[BLEDevice]] = Future()

                def _run_async_in_thread():
                    """
                    Run the device discovery coroutine and set the result or exception on a future.
                    """
                    try:
                        result = asyncio.run(
                            BLEInterface._with_timeout(
                                _get_devices_async_and_filter(address),
                                BLE_SCAN_TIMEOUT,
                                "connected-device fallback",
                            )
                        )
                        future.set_result(result)
                    except Exception as e:
                        future.set_exception(e)

                thread = Thread(target=_run_async_in_thread, daemon=True)
                thread.start()
                try:
                    devices = future.result(timeout=BLE_SCAN_TIMEOUT)
                except FutureTimeoutError:
                    logger.debug(
                        "Fallback connected-device discovery thread exceeded %.1fs timeout",
                        BLE_SCAN_TIMEOUT,
                    )
                    thread.join(timeout=0.1)
                    return []
                except Exception as e:
                    logger.debug(
                        "Fallback device discovery thread failed with exception: %s", e
                    )
                    thread.join(timeout=0.1)
                    return []
            except RuntimeError:
                # No running loop in this thread
                devices = asyncio.run(
                    BLEInterface._with_timeout(
                        _get_devices_async_and_filter(address),
                        BLE_SCAN_TIMEOUT,
                        "connected-device fallback",
                    )
                )

            logger.debug(
                "Found %d connected Meshtastic devices via fallback", len(devices)
            )
            return devices

        except (
            BleakError,
            BleakDBusError,
            AttributeError,
            RuntimeError,
            asyncio.TimeoutError,
            self.BLEError,
        ) as e:
            logger.debug("Fallback device discovery failed: %s", e)
            return []

    def connect(self, address: Optional[str] = None) -> "BLEClient":
        """
        Establish a BLE connection to the Meshtastic device identified by address.

        If address is provided it will be used to select the peripheral; otherwise a scan is performed to find a suitable device.
            On success returns a connected BLEClient with notifications registered and internal reconnect state updated.
            On failure, the created client is closed before the exception is propagated.

        Args:
        ----
            address (Optional[str]): BLE address or device name to connect to; may be None to allow automatic discovery.

        Returns:
        -------
            BLEClient: A connected BLEClient instance for the selected device.

        """

        with self._connect_lock:
            # Invariant: BLEClient lifecycle (create/close) stays under _connect_lock to avoid concurrent loop manipulation.
            requested_identifier = address if address is not None else self.address
            normalized_request = BLEInterface._sanitize_address(requested_identifier)

            with self._client_lock:
                existing_client = self.client
                if existing_client and existing_client.is_connected():
                    bleak_client = getattr(existing_client, "bleak_client", None)
                    bleak_address = getattr(bleak_client, "address", None)
                    normalized_known_targets = {
                        self._last_connection_request,
                        BLEInterface._sanitize_address(self.address),
                        BLEInterface._sanitize_address(bleak_address),
                    }
                    if (
                        normalized_request is None
                        or normalized_request in normalized_known_targets
                    ):
                        logger.debug("Already connected, skipping connect call.")
                        return existing_client

            # Bleak docs recommend always doing a scan before connecting (even if we know addr)
            device = self.find_device(address or self.address)
            self.address = device.address  # Keep address in sync for auto-reconnect
            client = BLEClient(
                device.address, disconnected_callback=self._on_ble_disconnect
            )
            previous_client = None
            try:
                client.connect(await_timeout=CONNECTION_TIMEOUT)
                services = getattr(client.bleak_client, "services", None)
                if not services or not getattr(services, "get_characteristic", None):
                    logger.debug(
                        "BLE services not available immediately after connect; getting services"
                    )
                    client.get_services(timeout=GATT_IO_TIMEOUT)
                # Ensure notifications are always active for this client (reconnect-safe)
                self._register_notifications(client)
                # Publish the new client before waking any waiters
                with self._client_lock:
                    previous_client = self.client
                    self.client = client
                    self._disconnect_notified = False
                    normalized_device_address = BLEInterface._sanitize_address(
                        device.address
                    )
                    if normalized_request is not None:
                        self._last_connection_request = normalized_request
                    else:
                        self._last_connection_request = normalized_device_address
                if previous_client and previous_client is not client:
                    close_thread = self.thread_coordinator.create_thread(
                        target=self._safe_close_client,
                        args=(previous_client,),
                        name="BLEClientClose",
                        daemon=True,
                    )
                    self.thread_coordinator.start_thread(close_thread)
                # Signal successful reconnection to waiting threads
                self.thread_coordinator.set_event("reconnected_event")
                self._read_retry_count = (
                    0  # Reset transient error counter on successful connect
                )
            except Exception:  # Intentional blanket catch for connection cleanup
                logger.debug(
                    "Failed to connect, closing BLEClient thread.", exc_info=True
                )
                self.error_handler.safe_cleanup(client.close)
                raise

            return client

    def _handle_read_loop_disconnect(
        self, error_message: str, previous_client: "BLEClient"
    ) -> bool:
        """
        Decides whether the receive loop should continue after a BLE disconnection.

        Args:
        ----
            error_message (str): Human-readable description of the disconnection cause.
            previous_client (BLEClient): The BLEClient instance that observed the disconnect and may be closed.

        Returns:
        -------
            bool: `true` if the read loop should continue to allow auto-reconnect, `false` otherwise.

        """
        logger.debug("Device disconnected: %s", error_message)
        should_continue = self._handle_disconnect(
            f"read_loop: {error_message}", client=previous_client
        )
        if not should_continue:
            # End our read loop immediately
            self._want_receive = False
        return should_continue

    def _safe_close_client(self, c: "BLEClient", event: Optional[Event] = None) -> None:
        """
        Close the provided BLEClient and suppress common close-time errors.

        Attempts to close the given BLEClient and ignores BleakError, RuntimeError, and OSError raised during close
            to avoid propagating shutdown-related exceptions.

        Args:
        ----
            c (BLEClient): The BLEClient instance to close; may be None or already-closed.
            event (Optional[Event]): An optional threading.Event to set after the client is closed.

        """
        self.error_handler.safe_cleanup(c.close, "client close")
        if event:
            event.set()

    def _receiveFromRadioImpl(self) -> None:
        """
        Run loop that reads inbound packets from the BLE FROMRADIO characteristic and delivers them to the interface packet handler.

        Waits on an internal read trigger, performs GATT reads when a BLE client is available, and forwards non-empty payloads
            to _handleFromRadio. Coordinates with the reconnection logic when the client becomes unavailable and tolerates
            transient empty reads. On a fatal error, initiates a safe shutdown of the interface.
        """
        try:
            while self._want_receive:
                # Wait for data to read, but also check periodically for reconnection
                if not self.thread_coordinator.wait_for_event(
                    "read_trigger", timeout=RECEIVE_WAIT_TIMEOUT
                ):
                    # Timeout occurred, check if we were reconnected during this time
                    if self.thread_coordinator.check_and_clear_event(
                        "reconnected_event"
                    ):
                        logger.debug("Detected reconnection, resuming normal operation")
                    continue
                self.thread_coordinator.clear_event("read_trigger")

                # Retry loop for handling empty reads and transient BLE issues
                retries: int = 0
                while self._want_receive:
                    with self._client_lock:
                        client = self.client
                    if client is None:
                        if self.auto_reconnect:
                            logger.debug(
                                "BLE client is None; waiting for auto-reconnect"
                            )
                            # Wait briefly for reconnect or shutdown signal, then re-check
                            self.thread_coordinator.wait_for_event(
                                "reconnected_event", timeout=RECEIVE_WAIT_TIMEOUT
                            )
                            break  # Return to outer loop to re-check state
                        logger.debug("BLE client is None, shutting down")
                        self._want_receive = False
                        break
                    try:
                        # Read from the FROMRADIO characteristic for incoming packets
                        b = client.read_gatt_char(
                            FROMRADIO_UUID, timeout=GATT_IO_TIMEOUT
                        )
                        if not b:
                            # Handle empty reads with limited retries to avoid busy-looping
                            if retries < EMPTY_READ_MAX_RETRIES:
                                time.sleep(EMPTY_READ_RETRY_DELAY)
                                retries += 1
                                continue
                            logger.warning(
                                "Exceeded max retries for empty BLE read from %s",
                                FROMRADIO_UUID,
                            )
                            break  # Too many empty reads, exit to recheck state
                        logger.debug("FROMRADIO read: %s", b.hex())
                        self._handleFromRadio(b)
                        retries = 0  # Reset retry counter on successful read
                        self._read_retry_count = (
                            0  # Reset transient error retry counter on successful read
                        )
                    except BleakDBusError as e:
                        # Handle D-Bus specific BLE errors (common on Linux)
                        if self._handle_read_loop_disconnect(str(e), client):
                            break
                        return
                    except BleakError as e:
                        # Handle general BLE errors, check if client disconnected
                        if client and not client.is_connected():
                            if self._handle_read_loop_disconnect(str(e), client):
                                break
                            return
                        # Client is still connected, this might be a transient error
                        # Retry a few times before escalating
                        retry_count = getattr(self, "_read_retry_count", 0)
                        if retry_count < TRANSIENT_READ_MAX_RETRIES:
                            self._read_retry_count = retry_count + 1
                            logger.debug(
                                "Transient BLE read error, retrying (%d/%d)",
                                retry_count + 1,
                                TRANSIENT_READ_MAX_RETRIES,
                            )
                            time.sleep(TRANSIENT_READ_RETRY_DELAY)
                            continue
                        self._read_retry_count = 0
                        logger.debug(
                            "Persistent BLE read error after retries", exc_info=True
                        )
                        raise self.BLEError(ERROR_READING_BLE) from e
        except Exception:
            logger.exception("Fatal error in BLE receive thread, closing interface.")
            if not self._closing:
                # Use a thread to avoid deadlocks if close() waits for this thread
                error_close_thread = self.thread_coordinator.create_thread(
                    target=self.close, name="BLECloseOnError", daemon=True
                )
                self.thread_coordinator.start_thread(error_close_thread)

    def _sendToRadioImpl(self, toRadio) -> None:
        """
        Send a protobuf message to the radio over the TORADIO BLE characteristic.

        Serializes `toRadio` to bytes and writes them with write-with-response to the TORADIO characteristic when a BLE client
            is available. If the serialized payload is empty or no client is present (e.g., during shutdown), the call is a
            no-op. After a successful write, the method waits briefly to allow propagation and then signals the read trigger.

        Args:
        ----
            toRadio: A protobuf message with a SerializeToString() method representing the outbound radio packet.

        Raises:
        ------
            BLEInterface.BLEError: If the write operation fails.

        """
        b: bytes = toRadio.SerializeToString()
        if not b:
            return

        write_successful = False
        with self._client_lock:
            client = self.client
            if client:  # Silently ignore writes while shutting down to avoid errors
                logger.debug("TORADIO write: %s", b.hex())
                try:
                    # Use write-with-response to ensure delivery is acknowledged by the peripheral
                    client.write_gatt_char(
                        TORADIO_UUID, b, response=True, timeout=GATT_IO_TIMEOUT
                    )
                    write_successful = True
                except (BleakError, RuntimeError, OSError) as e:
                    # Log detailed error information and wrap in our interface exception
                    logger.debug(
                        "Error during write operation: %s",
                        type(e).__name__,
                        exc_info=True,
                    )
                    raise self.BLEError(ERROR_WRITING_BLE) from e
            else:
                logger.debug(
                    "Skipping TORADIO write: no BLE client (closing or disconnected)."
                )

        if write_successful:
            # Brief delay to allow write to propagate before triggering read
            time.sleep(SEND_PROPAGATION_DELAY)
            self.thread_coordinator.set_event(
                "read_trigger"
            )  # Wake receive loop to process any response

    def close(self) -> None:
        """
        Shut down the BLE interface, stop background activity, disconnect the BLE client, and perform cleanup.

        This method is idempotent and safe to call multiple times. It stops the receive loop and reconnection waits, unregisters the
            atexit handler, disconnects and closes any active BLE client, emits a disconnected notification if not already sent,
            and waits briefly for pending disconnect-related notifications and the receive thread to finish.
        """
        with self._closing_lock:
            if self._closed:
                logger.debug(
                    "BLEInterface.close called on already closed interface; ignoring"
                )
                return
            if self._closing:
                logger.debug(
                    "BLEInterface.close called while another shutdown is in progress; ignoring"
                )
                return
            self._closing = True

        # Close parent interface (stops publishing thread, etc.)
        self.error_handler.safe_execute(
            lambda: MeshInterface.close(self), error_msg="Error closing mesh interface"
        )

        if self._want_receive:
            self._want_receive = False  # Tell the thread we want it to stop
            self.thread_coordinator.wake_waiting_threads(
                "read_trigger", "reconnected_event"
            )  # Wake all waiting threads
            if self._receiveThread:
                self.thread_coordinator.join_thread(
                    self._receiveThread, timeout=RECEIVE_THREAD_JOIN_TIMEOUT
                )
                if self._receiveThread.is_alive():
                    logger.warning(
                        "BLE receive thread did not exit within %.1fs",
                        RECEIVE_THREAD_JOIN_TIMEOUT,
                    )
                self._receiveThread = None

        if self._exit_handler:
            with contextlib.suppress(ValueError):
                atexit.unregister(self._exit_handler)
            self._exit_handler = None

        with self._client_lock:
            client = self.client
            self.client = None
        if client:
            self._disconnect_and_close_client(client)

        # Send disconnected indicator if not already notified
        notify = False
        with self._client_lock:
            if not self._disconnect_notified:
                self._disconnect_notified = True
                notify = True

        if notify:
            self._disconnected()  # send the disconnected indicator up to clients
            self._wait_for_disconnect_notifications()

        # Clean up thread coordinator
        self.thread_coordinator.cleanup()
        with self._closing_lock:
            self._closed = True

    def _wait_for_disconnect_notifications(
        self, timeout: Optional[float] = None
    ) -> None:
        """
        Wait up to `timeout` seconds for queued publish notifications to flush before proceeding.

        If the queue does not flush within `timeout`, this method logs the timeout if the publishing thread is still alive;
        if the publishing thread is not running, it drains the publish queue synchronously. Errors raised while attempting
        the flush are caught and logged.

        Args:
        ----
            timeout (float | None): Maximum seconds to wait for the publish queue to flush. If None, uses DISCONNECT_TIMEOUT_SECONDS.

        """
        if timeout is None:
            timeout = DISCONNECT_TIMEOUT_SECONDS
        flush_event = Event()
        self.error_handler.safe_execute(
            lambda: publishingThread.queueWork(flush_event.set),
            error_msg="Runtime error during disconnect notification flush (possible threading issue)",
            reraise=False,
        )

        if not flush_event.wait(timeout=timeout):
            thread = getattr(publishingThread, "thread", None)
            if thread is not None and thread.is_alive():
                logger.debug("Timed out waiting for publish queue flush")
            else:
                self._drain_publish_queue(flush_event)

    def _disconnect_and_close_client(self, client: "BLEClient"):
        """
        Disconnect the specified BLEClient and ensure its resources are released.

        Attempts to disconnect the client within DISCONNECT_TIMEOUT_SECONDS; if the disconnect times out or fails,
        forces a close to release underlying resources.

        Args:
        ----
            client (BLEClient): The BLE client instance to disconnect and close.

        """
        try:
            self.error_handler.safe_cleanup(
                lambda: client.disconnect(await_timeout=DISCONNECT_TIMEOUT_SECONDS)
            )
        finally:
            self._safe_close_client(client)

    def _drain_publish_queue(self, flush_event: Event) -> None:
        """
        Drain and run any pending publish callbacks inline until the queue is empty or `flush_event` is set.

        This executes queued callables from the publishing thread's queue on the current thread, catching and logging exceptions from
            each callable so draining continues. If the publishing thread has no accessible queue, the method returns immediately.

        Args:
        ----
            flush_event (Event): When set, stops draining and causes the function to return promptly.

        """
        queue = getattr(publishingThread, "queue", None)
        if queue is None:
            return
        while not flush_event.is_set():
            try:
                runnable = queue.get_nowait()
            except Empty:
                break
            self.error_handler.safe_execute(
                runnable, error_msg="Error in deferred publish callback", reraise=False
            )


class BLEClient:
    """
    Client wrapper for managing BLE device connections with thread-safe async operations.

    This class provides a synchronous interface to Bleak's async operations by running
    an internal event loop in a dedicated thread. It handles the complexity of
    asyncio-to-thread synchronization while providing a simple API for BLE operations.
"""

    # Class-level fallback so callers using __new__ still get the right exception type
    BLEError = BLEInterface.BLEError

    def __init__(self, address=None, **kwargs) -> None:
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
        self.BLEError = BLEInterface.BLEError

        # Create dedicated event loop for this client instance
        self._eventLoop = asyncio.new_event_loop()
        # Start event loop in background thread for async operations
        self._eventThread = Thread(
            target=self._run_event_loop, name="BLEClient", daemon=True
        )
        self._eventThread.start()

        if not address:
            logger.debug("No address provided - only discover method will work.")
            return

        # Create underlying Bleak client for actual BLE communication
        self.bleak_client = BleakRootClient(address, **kwargs)

    def discover(self, **kwargs):  # pylint: disable=C0116
        """
        Discover nearby BLE devices using BleakScanner.

        Keyword arguments are forwarded to BleakScanner.discover (for example, `timeout` or `adapter`).

        Returns:
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

        Returns:
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

            Returns:
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

        Raises:
            BLEInterface.BLEError: If the write operation fails or times out.

        """
        self.async_await(
            self.bleak_client.write_gatt_char(*args, **kwargs), timeout=timeout
        )

    def get_services(self, timeout: Optional[float] = None):
        """
        Retrieve the discovered GATT services and characteristics for the connected device.

        Returns:
            The device's GATT services and their characteristics as returned by the underlying BLE library.

        """
        # services is a property, not an async method, so we access it directly
        # timeout parameter is kept for API compatibility but not used
        _ = timeout  # Mark as intentionally unused for API compatibility
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

        """
        services = getattr(self.bleak_client, "services", None)
        if not services or not getattr(services, "get_characteristic", None):
            self.error_handler.safe_execute(
                lambda: self.get_services(timeout=GATT_IO_TIMEOUT),
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
        self._eventThread.join(timeout=BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT)
        if self._eventThread.is_alive():
            logger.warning(
                "BLE event thread did not exit within %.1fs",
                BLECLIENT_EVENT_THREAD_JOIN_TIMEOUT,
            )

    def __enter__(self):
        """
        Enter the context manager and provide the BLEClient instance for use within the with-block.

        Returns:
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

        If the coroutine does not finish within `timeout` seconds the pending task is cancelled and a BLEInterface.BLEError is raised.

        Args:
        ----
            coro: The coroutine to run on the client's internal event loop.
            timeout (float | None): Maximum seconds to wait for completion; `None` means wait indefinitely.

        Returns:
        -------
            The value produced by the completed coroutine.

        Raises:
        ------
            BLEInterface.BLEError: If the wait times out.

        """
        # Exception mapping contract:
        #   - FutureTimeoutError -> BLEInterface.BLEError(BLECLIENT_ERROR_ASYNC_TIMEOUT)
        #   - Bleak* exceptions propagate so BLEInterface wrappers can convert them consistently.
        future = self.async_run(coro)
        try:
            return future.result(timeout)
        except FutureTimeoutError as e:
            future.cancel()  # Clean up pending task to avoid resource leaks
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
