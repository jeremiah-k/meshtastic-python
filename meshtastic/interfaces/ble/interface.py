"""Main BLE interface class."""

import asyncio
import atexit
import contextlib
import io
import struct
import threading
import time
from queue import Empty
from threading import Event, Thread
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

from bleak import BleakClient as BleakRootClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakError

from meshtastic import publishingThread
from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.connection import (
    ClientManager,
    ConnectionOrchestrator,
    ConnectionValidator,
)
from meshtastic.interfaces.ble.constants import (
    CONNECTION_TIMEOUT,
    DISCONNECT_TIMEOUT_SECONDS,
    ERROR_CONNECTION_FAILED,
    ERROR_MULTIPLE_DEVICES,
    ERROR_NO_PERIPHERALS_FOUND,
    ERROR_READING_BLE,
    ERROR_TIMEOUT,
    ERROR_WRITING_BLE,
    FROMNUM_UUID,
    FROMRADIO_UUID,
    GATT_IO_TIMEOUT,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    MALFORMED_NOTIFICATION_THRESHOLD,
    NOTIFICATION_START_TIMEOUT,
    RECEIVE_THREAD_JOIN_TIMEOUT,
    SERVICE_UUID,
    TORADIO_UUID,
    BLEConfig,
    logger,
)
from meshtastic.interfaces.ble.coordination import ThreadCoordinator
from meshtastic.interfaces.ble.discovery import (
    DiscoveryManager,
    _ble_device_constructor_kwargs_support,
    _parse_scan_response,
)
from meshtastic.interfaces.ble.errors import BLEErrorHandler, DecodeError
from meshtastic.interfaces.ble.gating import (
    _addr_key,
    _addr_lock_context,
    _is_currently_connected_elsewhere,
    _mark_connected,
    _mark_disconnected,
)
from meshtastic.interfaces.ble.notifications import NotificationManager
from meshtastic.interfaces.ble.policies import RetryPolicy
from meshtastic.interfaces.ble.reconnection import ReconnectScheduler
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from meshtastic.interfaces.ble.utils import _sleep, sanitize_address
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2

T = TypeVar("T")
MAX_DRAIN_ITERATIONS = 10_000


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
        - BLEStateManager: Centralized connection state machine with shared locking
        - ThreadCoordinator: Thread/event lifecycle and coordination utilities
        - BLEErrorHandler: Standardized error handling patterns
        - NotificationManager: Tracks active notifications for reconnect-safe resubscription
        - DiscoveryManager: Scans and falls back to connected-device enumeration
        - ConnectionValidator: Enforces connection preconditions
        - ClientManager: Owns BLEClient lifecycle and cleanup operations
        - ConnectionOrchestrator: Coordinates connection establishment
        - ReconnectScheduler / ReconnectWorker: Policy-driven reconnect attempts

    Note: This interface requires appropriate Bluetooth permissions and may
    need platform-specific setup for BLE operations.
    """

    class BLEError(Exception):
        """An exception class for BLE errors."""

    def __init__(
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
        Create and initialize a BLEInterface, start its background receive thread, and attempt an initial connection and configuration to a Meshtastic BLE device.

        Initializes internal state, thread and event coordinators, notification and discovery managers, starts the receive thread, attempts to connect (using the provided address or any discovered device when None), performs protocol configuration when enabled, and registers an atexit handler to ensure orderly disconnect on process exit.

        Parameters
        ----------
            address (Optional[str]): BLE address or device name to connect to; if None, any discovered Meshtastic device may be used.
            auto_reconnect (bool): If True, schedule automatic reconnection after unexpected disconnects; if False, the interface will not attempt automatic reconnects and will begin shutdown on disconnect.

        Raises
        ------
            BLEInterface.BLEError: If the initial connection or configuration fails.

        """

        # Thread safety and state management
        # Unified state-based lock system replacing multiple locks and boolean flags
        #
        # Lock Ordering (to prevent deadlocks):
        #     When acquiring multiple locks, always acquire in this order:
        #     1. Global registry lock (_REGISTRY_LOCK in gating.py)
        #     2. Per-address locks (_ADDR_LOCKS in gating.py, via _addr_lock_context)
        #     3. Interface connect lock (_connect_lock)
        #     4. Interface state lock (_state_lock)
        #     5. Interface disconnect lock (_disconnect_lock)
        #
        # EXCEPTION: In _handle_disconnect, _disconnect_lock is acquired FIRST
        # (non-blocking) before _state_lock. This intentional inversion enables
        # early-return optimization for concurrent disconnect callbacks without
        # blocking on state_lock. Safe because _disconnect_lock uses non-blocking
        # acquire-if other code needs both locks, it acquires state_lock first,
        # then disconnect_lock, and _handle_disconnect's non-blocking acquire will
        # simply fail and return early.
        #
        # _connect_lock Purpose:
        #     Serializes connection attempts within a single interface instance.
        #     While _addr_lock_context provides process-wide serialization for the
        #     same address, _connect_lock ensures that within this interface,
        #     only one connection attempt can be in the critical section at a time.
        #     This prevents race conditions when checking existing_client and
        #     managing the connection state machine.
        self._state_manager = BLEStateManager()  # Centralized state tracking
        self._state_lock = (
            self._state_manager.lock
        )  # `lock` is a property returning the shared RLock instance
        self._connect_lock = threading.RLock()  # Serializes connection attempts
        self._disconnect_lock = threading.Lock()  # Serializes disconnect handling
        self._closed: bool = (
            False  # Tracks completion of shutdown for idempotent close()
        )
        self._exit_handler = None
        self.address = address
        self._last_connection_request: Optional[str] = sanitize_address(address)
        self.auto_reconnect = auto_reconnect
        self._disconnect_notified = False  # Prevents duplicate disconnect events
        self._connection_alias_key: Optional[str] = None  # Track alias for cleanup

        # Error handling infrastructure
        self.error_handler = BLEErrorHandler()

        # Thread management infrastructure
        self.thread_coordinator = ThreadCoordinator()
        self._notification_manager = NotificationManager()
        self._discovery_manager: Optional[DiscoveryManager] = DiscoveryManager()
        self._connection_validator = ConnectionValidator(
            self._state_manager, self._state_lock, self.BLEError
        )
        self._client_manager = ClientManager(
            self._state_manager,
            self._state_lock,
            self.thread_coordinator,
            self.error_handler,
        )
        self._connection_orchestrator = ConnectionOrchestrator(
            self,
            self._connection_validator,
            self._client_manager,
            self._discovery_manager,
            self._state_manager,
            self._state_lock,
            self.thread_coordinator,
        )
        self._reconnect_scheduler = ReconnectScheduler(
            self._state_manager,
            self._state_lock,
            self.thread_coordinator,
            self,
        )

        # Event coordination for reconnection and read operations
        self._read_trigger = self.thread_coordinator.create_event(
            "read_trigger"
        )  # Signals when data is available to read
        self._reconnected_event = self.thread_coordinator.create_event(
            "reconnected_event"
        )  # Signals when reconnection occurred
        self._shutdown_event = self.thread_coordinator.create_event("shutdown_event")
        self._malformed_notification_count = 0  # Tracks corrupted packets for threshold
        self._ever_connected = (
            False  # Track first successful connection to tune logging
        )

        # Initialize parent interface
        MeshInterface.__init__(
            self, debugOut=debugOut, noProto=noProto, noNodes=noNodes, timeout=timeout
        )

        # Initialize retry counter for transient read errors
        # Policies are immutable presets; cache instances to avoid churn in hot loops.
        self._empty_read_policy = RetryPolicy.empty_read()
        self._transient_read_policy = RetryPolicy.transient_error()
        self._read_retry_count = 0
        self._last_empty_read_warning = 0.0
        self._suppressed_empty_read_warnings = 0

        self.client: Optional["BLEClient"] = None

        # Start background receive thread for inbound packet processing
        logger.debug("Threads starting")
        with self._state_lock:
            self._want_receive = True
        self._receiveThread: Optional[Thread] = None
        self._start_receive_thread(name="BLEReceive")
        logger.debug("Threads running")
        try:
            logger.debug("BLE connecting to: %s", address if address else "any")
            self.connect(address)
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

    def __repr__(self) -> str:
        """
        Return a compact textual representation of the BLEInterface including its address and any non-default feature flags.

        Returns:
            str: A string in the form "BLEInterface(...)" containing `address`, `debugOut` when set, and boolean flags `noProto`, `noNodes`, and `auto_reconnect` only when they differ from their defaults.

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

    def _set_receive_wanted(self, want_receive: bool) -> None:
        """
        Set the receive-loop intent flag under the interface state lock.

        Parameters
        ----------
            want_receive (bool): Desired receive-loop run state.

        """
        with self._state_lock:
            self._want_receive = want_receive

    def _should_run_receive_loop(self) -> bool:
        """
        Return whether the receive loop should currently continue running.

        The check is performed under the state lock so callers observe a
        consistent shutdown tuple (`_closed`, `_want_receive`).
        """
        with self._state_lock:
            return self._want_receive and not self._closed

    def _start_receive_thread(self, *, name: str) -> None:
        """
        Create and start the background receive thread, updating `_receiveThread`.

        Parameters
        ----------
            name (str): Thread name to assign (for diagnostics/logging).

        """
        with self._state_lock:
            # Avoid reviving the receive loop after shutdown has begun.
            if self._closed or not self._want_receive:
                logger.debug(
                    "Skipping receive thread start (%s): interface is closing/stopped.",
                    name,
                )
                return
            # Prevent duplicate live receive threads except when the current thread
            # is replacing itself after an unexpected failure.
            existing = self._receiveThread
            if (
                existing is not None
                and existing is not threading.current_thread()
                and existing.is_alive()
            ):
                logger.debug(
                    "Skipping receive thread start (%s): %s is already running.",
                    name,
                    existing.name,
                )
                return
            thread = self.thread_coordinator.create_thread(
                target=self._receiveFromRadioImpl,
                name=name,
                daemon=True,
            )
            self._receiveThread = thread
        self.thread_coordinator.start_thread(thread)

    @staticmethod
    def _sorted_address_keys(*keys: Optional[str]) -> List[str]:
        """
        Produce a deterministically ordered list of unique, non-empty address keys.

        Parameters
        ----------
            keys (Optional[str]): One or more address key strings; `None` or empty values are ignored.

        Returns
        -------
            List[str]: Unique, non-empty address keys sorted in deterministic order.

        """
        return sorted({key for key in keys if key})

    def _lock_ordered_address_keys(
        self, ordered_keys: List[str]
    ) -> contextlib.ExitStack:
        """
        Acquire holder tracking and address locks for already-sorted keys.

        Returns
        -------
            contextlib.ExitStack: Active stack that must remain open while lock
            ownership is required.

        """
        stack = contextlib.ExitStack()
        for key in ordered_keys:
            addr_lock = stack.enter_context(_addr_lock_context(key))
            stack.enter_context(addr_lock)
        return stack

    def _mark_address_keys_connected(self, *keys: Optional[str]) -> None:
        """Mark one or more address keys as connected using deterministic lock ordering."""
        ordered_keys = self._sorted_address_keys(*keys)
        if not ordered_keys:
            return
        with self._lock_ordered_address_keys(ordered_keys):
            for key in ordered_keys:
                _mark_connected(key, owner=self)

    def _mark_address_keys_disconnected(self, *keys: Optional[str]) -> None:
        """
        Mark the given address keys as disconnected while acquiring per-address locks in a deterministic order.

        Acquires each address's lock (using deterministic ordering to avoid deadlocks) and marks the address key as disconnected. None or empty keys are ignored.

        Parameters
        ----------
            *keys (Optional[str]): One or more address key strings to mark disconnected; None or empty values are skipped.

        """
        ordered_keys = self._sorted_address_keys(*keys)
        if not ordered_keys:
            return
        with self._lock_ordered_address_keys(ordered_keys):
            for key in ordered_keys:
                _mark_disconnected(key, owner=self)

    def _handle_disconnect(
        self,
        source: str,
        client: Optional["BLEClient"] = None,
        bleak_client: Optional[BleakRootClient] = None,
    ) -> bool:
        """
        Handle a BLE client disconnection by updating state and either scheduling an automatic reconnect or initiating shutdown.

        Parameters
        ----------
            source (str): Short tag identifying where the disconnect originated (for logging).
            client (Optional[BLEClient]): The BLEClient instance associated with the disconnect, if available.
            bleak_client (Optional[BleakRootClient]): The underlying Bleak client instance, if available.

        Returns
        -------
            bool: `True` if the interface remains active and will attempt auto-reconnect, `False` if shutdown has begun.

        """
        if not self._disconnect_lock.acquire(blocking=False):
            # Another disconnect handler is active; this is expected during concurrent
            # disconnect callbacks. The active handler will process the disconnect.
            logger.debug(
                "Disconnect from %s skipped: another disconnect handler is active.",
                source,
            )
            return True
        disconnect_lock_released = False
        target_client = client
        previous_client: Optional["BLEClient"] = None
        should_reconnect = False
        should_schedule_reconnect = False
        address = "unknown"
        disconnect_keys: List[str] = []
        try:
            # Perform state checks and state mutation atomically under the state lock.
            with self._state_lock:
                current_state = self._state_manager.state
                current_client = self.client
                is_closing = self._state_manager.is_closing or self._closed

                if current_state == ConnectionState.CONNECTING:
                    logger.debug(
                        "Ignoring disconnect from %s while a connection is in progress.",
                        source,
                    )
                    # Early returns inside this try block are safe: the finally
                    # below releases _disconnect_lock when disconnect_lock_released
                    # is still False.
                    return True
                if is_closing:
                    logger.debug("Ignoring disconnect from %s during shutdown.", source)
                    return False

                # Resolve callback source against the currently active client.
                if target_client is None and bleak_client is not None:
                    if (
                        current_client
                        and getattr(current_client, "bleak_client", None)
                        is bleak_client
                    ):
                        target_client = current_client
                    elif current_client is not None:
                        logger.debug("Ignoring stale disconnect from %s.", source)
                        return True

                # Ignore stale disconnect callbacks from non-active clients.
                if (
                    target_client is not None
                    and current_client is not None
                    and target_client is not current_client
                ):
                    logger.debug("Ignoring stale disconnect from %s.", source)
                    return True

                # Prevent duplicate disconnect notifications.
                if self._disconnect_notified:
                    logger.debug("Ignoring duplicate disconnect from %s.", source)
                    return True

                previous_client = current_client
                alias_key = self._connection_alias_key
                self.client = None
                self._disconnect_notified = True
                self._connection_alias_key = None
                self._state_manager.transition_to(ConnectionState.DISCONNECTED)
                should_reconnect = self.auto_reconnect
                should_schedule_reconnect = should_reconnect and not self._closed

                if target_client:
                    address = getattr(target_client, "address", repr(target_client))
                elif bleak_client:
                    address = getattr(bleak_client, "address", repr(bleak_client))
                elif previous_client:
                    address = getattr(previous_client, "address", repr(previous_client))

                if should_reconnect:
                    if previous_client:
                        previous_address = getattr(
                            previous_client, "address", self.address
                        )
                        device_key = (
                            _addr_key(previous_address) if previous_address else None
                        )
                        disconnect_keys = self._sorted_address_keys(
                            device_key, alias_key
                        )
                    else:
                        fallback_key = _addr_key(self.address)
                        disconnect_keys = self._sorted_address_keys(
                            fallback_key, alias_key
                        )
                else:
                    # Guard against sentinel "unknown" address - don't pollute registry.
                    # Prefer the previous active client's address because callback metadata can be stale.
                    address_for_registry = (
                        getattr(previous_client, "address", None)
                        if previous_client
                        else (address if address != "unknown" else self.address)
                    )
                    addr_disconnect_key = _addr_key(address_for_registry)
                    disconnect_keys = self._sorted_address_keys(
                        addr_disconnect_key, alias_key
                    )

            # Release the disconnect lock before any address-lock operations.
            self._disconnect_lock.release()
            disconnect_lock_released = True
        finally:
            if not disconnect_lock_released:
                self._disconnect_lock.release()

        logger.debug("BLE client %s disconnected (source: %s).", address, source)

        if disconnect_keys:
            self._mark_address_keys_disconnected(*disconnect_keys)

        if previous_client:
            # Keep cleanup behavior consistent between reconnect paths.
            close_thread = self.thread_coordinator.create_thread(
                target=self._client_manager.safe_close_client,
                args=(previous_client,),
                name="BLEClientClose",
                daemon=True,
            )
            self.thread_coordinator.start_thread(close_thread)
        self._disconnected()

        if should_reconnect:
            # Event coordination for reconnection (only if not closed)
            if should_schedule_reconnect:
                self.thread_coordinator.clear_events(
                    "read_trigger", "reconnected_event"
                )
                self._schedule_auto_reconnect()
            return True

        logger.debug("Auto-reconnect disabled, staying disconnected.")
        return False

    def _on_ble_disconnect(self, client: BleakRootClient) -> None:
        """
        Notify the interface that a Bleak client has disconnected.

        Parameters
        ----------
            client (BleakRootClient): The Bleak client that experienced the disconnection.

        """
        self._handle_disconnect("bleak_callback", bleak_client=client)

    def _schedule_auto_reconnect(self) -> None:
        """
        Schedule the reconnect worker to repeatedly attempt BLE reconnection until a connection succeeds or shutdown begins.

        If auto-reconnect is disabled, or the interface is closing or already closed, this call does nothing. Otherwise it clears the internal shutdown event and delegates scheduling to the reconnect scheduler.
        """

        if not self.auto_reconnect:
            return
        # Never schedule reconnect once shutdown has started
        if self._closed:
            logger.debug(
                "Skipping auto-reconnect scheduling because interface is closed."
            )
            return
        # Use state manager instead of boolean flag
        if self._state_manager.is_closing:
            logger.debug(
                "Skipping auto-reconnect scheduling because interface is closing."
            )
            return

        self._shutdown_event.clear()
        self._reconnect_scheduler.schedule_reconnect(
            self.auto_reconnect, self._shutdown_event
        )

    def _handle_malformed_fromnum(self, reason: str, exc_info: bool = False) -> None:
        """
        Increment the malformed FROMNUM notification counter and log the reason; when the counter reaches the configured threshold, log a warning and reset the counter.

        Parameters
        ----------
            reason (str): Description of why the notification was considered malformed.
            exc_info (bool): If True, include exception traceback information in the log.

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
        Handle a FROMNUM characteristic notification and trigger the receive loop.

        Parses a 4-byte little-endian unsigned 32-bit integer from the notification payload.
        On successful parse the malformed-notification counter is reset and the parsed value is logged.
        On parse failure or unexpected length the malformed-notification counter is incremented and a warning may be emitted when a threshold is reached.
        Always sets the thread coordinator "read_trigger" event to wake the read loop.

        Parameters
        ----------
            _ : Any
                Unused sender parameter provided by the BLE library.
            b : bytearray
                Notification payload expected to contain a 4-byte little-endian unsigned 32-bit integer.

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

        Parameters
        ----------
        client : Any
            Connected BLE client to register notifications on.

        """

        def _safe_call(
            handler: Callable[[Any, Any], None],
            sender: Any,
            data: Any,
            error_msg: str,
        ) -> None:
            """
            Invoke a notification handler via the interface's error handler so any exceptions are caught and reported.

            Parameters
            ----------
                handler (callable): Function to call with (sender, data).
                sender (Any): Origin of the notification passed to the handler.
                data (Any): Payload delivered to the handler (bytes, bytearray, or object).
                error_msg (str): Message forwarded to the error handler if the handler raises an exception.

            """
            self.error_handler.safe_execute(
                lambda: handler(sender, data),
                error_msg=error_msg,
            )

        def _safe_legacy_handler(sender, data):
            """
            Invoke the legacy log-radio notification handler for a received BLE notification and capture/report any handler errors without letting them propagate.

            Parameters
            ----------
                sender: The notification source (characteristic or client) that produced the payload.
                data: Raw notification payload (bytes or bytearray) delivered by the BLE characteristic.

            """
            _safe_call(
                self.legacy_log_radio_handler,
                sender,
                data,
                "Error in legacy log notification handler",
            )

        def _safe_log_handler(sender, data):
            """
            Forward a BLE log-characteristic notification to the configured log handler and record an error if the handler fails.

            Parameters
            ----------
                sender (Any): Notification sender (characteristic or client); may be unused by the handler.
                data (bytes | bytearray): Raw notification payload from the BLE device.

            """
            _safe_call(
                self.log_radio_handler,
                sender,
                data,
                "Error in log notification handler",
            )

        def _safe_from_num_handler(sender, data):
            """
            Call the FROMNUM notification handler and capture/report any exceptions raised by it.

            Parameters
            ----------
                sender (Any): Source of the notification (typically the BLE characteristic or client).
                data (bytes): Raw payload bytes received from the FROMNUM characteristic.

            """
            _safe_call(
                self.from_num_handler,
                sender,
                data,
                "Error in FROMNUM notification handler",
            )

        def _get_or_create_handler(
            uuid: str, factory: Callable[[], Callable[[Any, Any], None]]
        ) -> Callable[[Any, Any], None]:
            """
            Return the notification handler for the given characteristic UUID, creating and subscribing one if none exists.

            Parameters
            ----------
                uuid (str): Characteristic UUID to look up or register.
                factory (Callable[[], Callable[[Any, Any], None]]): Factory that returns a handler callable which accepts (sender, data).

            Returns
            -------
                Callable[[Any, Any], None]: The existing or newly created notification handler.

            """
            handler = self._notification_manager.get_callback(uuid)
            if handler is None:
                handler = factory()
                self._notification_manager.subscribe(uuid, handler)
            return handler

        # Optional log notifications - failures are non-fatal
        try:
            if client.has_characteristic(LEGACY_LOGRADIO_UUID):
                legacy_handler = _get_or_create_handler(
                    LEGACY_LOGRADIO_UUID, lambda: _safe_legacy_handler
                )
                client.start_notify(
                    LEGACY_LOGRADIO_UUID,
                    legacy_handler,
                    timeout=NOTIFICATION_START_TIMEOUT,
                )
            if client.has_characteristic(LOGRADIO_UUID):
                log_handler = _get_or_create_handler(
                    LOGRADIO_UUID, lambda: _safe_log_handler
                )
                client.start_notify(
                    LOGRADIO_UUID,
                    log_handler,
                    timeout=NOTIFICATION_START_TIMEOUT,
                )
        except (
            BleakError,
            BleakDBusError,
            RuntimeError,
            BLEClient.BLEError,
            self.BLEError,
        ) as e:
            logger.debug("Failed to start optional log notifications: %s", e)

        # Critical notification for packet ingress
        from_num_handler = _get_or_create_handler(
            FROMNUM_UUID, lambda: _safe_from_num_handler
        )
        client.start_notify(
            FROMNUM_UUID,
            from_num_handler,
            timeout=NOTIFICATION_START_TIMEOUT,
        )

    def log_radio_handler(self, _, b: bytearray) -> None:  # pylint: disable=C0116
        """
        Handle a protobuf LogRecord notification and forward a formatted log line to the instance log handler.

        Parses the notification payload as a mesh_pb2.LogRecord and forwards its message to self._handleLogLine. If the record includes a `source` the message is prefixed with "[source] ". Malformed records are logged and ignored.

        Parameters
        ----------
            _ (Any): Unused sender/handle value provided by the BLE library.
            b (bytearray): Serialized mesh_pb2.LogRecord payload from the BLE notification.

        """
        log_record = mesh_pb2.LogRecord()  # type: ignore[attr-defined]
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
        Handle a legacy log-radio notification by delivering the decoded UTF-8 log line to the log handler.

        Decode the notification payload `b` as UTF-8, strip newline characters, and forward the resulting string to `self._handleLogLine`. If decoding fails, a warning is logged and the payload is ignored.

        Parameters
        ----------
            _ (Any): Unused sender/handle value provided by the BLE library.
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
    async def _with_timeout(
        awaitable: Awaitable[T], timeout: Optional[float], label: str
    ) -> T:
        """
        Await an awaitable and raise BLEError if it does not complete within the given timeout.

        Parameters
        ----------
            awaitable: The awaitable to await.
            timeout (Optional[float]): Maximum time in seconds to wait; if None, wait indefinitely.
            label (str): Short label used in the timeout error message.

        Returns
        -------
            The value produced by the awaited awaitable.

        Raises
        ------
            BLEInterface.BLEError: If the awaitable does not finish before the timeout elapses.

        """
        if timeout is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BLEInterface.BLEError(ERROR_TIMEOUT.format(label, timeout)) from exc

    @staticmethod
    def scan() -> List[BLEDevice]:
        """
        Scan for BLE devices advertising the Meshtastic service UUID.

        Performs a timed scan and returns discovered devices that advertise the Meshtastic service.
        Returns an empty list if no matching devices are found or if the scan fails for non-DBus reasons.

        Returns
        -------
            List[BLEDevice]: Discovered BLEDevice instances advertising the Meshtastic service.

        Raises
        ------
            BleakDBusError: If a DBus-level error occurs during scanning (propagated to callers).

        """
        with BLEClient(log_if_no_address=False) as client:
            logger.debug(
                "Scanning for BLE devices (takes %.0f seconds)...",
                BLEConfig.BLE_SCAN_TIMEOUT,
            )
            try:
                response = client.discover(
                    timeout=BLEConfig.BLE_SCAN_TIMEOUT,
                    return_adv=True,
                    service_uuids=[SERVICE_UUID],
                )
                return _parse_scan_response(response)
            except BleakDBusError:
                # Propagate DBus-level failures so callers can back off appropriately
                raise
            except (BleakError, RuntimeError) as e:
                logger.warning("Device scan failed: %s", e, exc_info=True)
                return []
            except Exception as e:  # pragma: no cover - defensive last resort
                logger.warning(
                    "Unexpected error during device scan: %s", e, exc_info=True
                )
                return []

    def find_device(self, address: Optional[str]) -> BLEDevice:
        """
        Locate a Meshtastic BLEDevice by address or device name.

        Parameters
        ----------
            address (Optional[str]): A device address or name to match. Separators (':', '-', '_', and spaces) are ignored when matching. If None, any discovered Meshtastic device may be returned.

        Returns
        -------
            BLEDevice: The matched BLE device. If `address` is None and multiple devices are discovered, the first discovered device is returned.

        Raises
        ------
            BLEInterface.BLEError: If no Meshtastic devices are found, if multiple matching devices are found when an `address` was provided, if the discovery manager is unavailable, or if a synthetic device cannot be created from the provided address.

        """

        target = address or getattr(self, "address", None)
        sanitized = sanitize_address(target)

        # Surface DBus failures to allow higher-level backoff
        if self._discovery_manager is None:
            raise self.BLEError("Discovery manager not available")
        addressed_devices = self._discovery_manager.discover_devices(sanitized)

        if len(addressed_devices) == 0:
            if address:
                logger.warning(
                    "No peripherals found for %s via scan; attempting direct address connect",
                    address,
                )
                # Create a synthetic BLEDevice only for direct address connection attempts
                # This allows the connection logic to attempt direct connect without verification
                if not sanitized:
                    raise self.BLEError(
                        "Address resolution failed, cannot create device"
                    )
                # Bleak BLEDevice constructor parameters vary across versions
                # (for example, some versions require/accept "details" while
                # others differ in optional metadata fields). Use signature
                # inspection to construct a compatible synthetic BLEDevice.
                supports_details, _ = _ble_device_constructor_kwargs_support()
                params: Dict[str, Any] = {"address": address, "name": address}
                if supports_details:
                    params["details"] = {}  # Empty details for synthetic device
                return BLEDevice(**params)
            raise self.BLEError(ERROR_NO_PERIPHERALS_FOUND)
        if len(addressed_devices) == 1:
            return addressed_devices[0]
        if address and len(addressed_devices) > 1:
            # Build a list of found devices for the error message
            device_list = "\n".join(
                [f"- {d.name or 'Unknown'} ({d.address})" for d in addressed_devices]
            )
            raise self.BLEError(ERROR_MULTIPLE_DEVICES.format(address, device_list))
        # No specific address provided and multiple devices found, return the first one
        return addressed_devices[0]

    @property
    def connection_state(self) -> ConnectionState:
        """
        Retrieve the current BLE connection state.

        Returns:
            ConnectionState: The current connection state held by the internal state manager.

        """
        return self._state_manager.state

    @property
    def is_connection_connected(self) -> bool:
        """
        Report whether the interface currently has an active BLE connection.

        Returns:
            True if a connection is active, False otherwise.

        """
        return self._state_manager.is_connected

    @property
    def is_connection_closing(self) -> bool:
        """
        Report whether the interface is shutting down or has already closed.

        Returns:
            `true` if the interface is shutting down or closed, `false` otherwise.

        """
        return self._state_manager.is_closing or self._closed

    @property
    def can_initiate_connection(self) -> bool:
        """
        Indicates whether the interface may start a new BLE connection.

        Returns:
            bool: True if a new connection may be started, False otherwise.

        """
        return self._state_manager.can_connect and not self._closed

    def connect(self, address: Optional[str] = None) -> "BLEClient":
        """
        Connects to a Meshtastic device over BLE by explicit address or by performing device discovery.

        Attempts to establish and return a connected BLE client for the requested device. If an existing compatible client is already connected, that client is returned. The method uses per-address gating to suppress duplicate concurrent connects and updates the interface's stored address and client reference on success. On failure any client opened during the attempt is closed before the error is propagated.

        Parameters
        ----------
            address (Optional[str]): BLE address or device name to connect to; if None, discovery is used to select a device.

        Returns
        -------
            BLEClient: The connected BLE client for the selected device.

        Raises
        ------
            BLEInterface.BLEError: If the interface is closing, if connection is suppressed due to a recent connect elsewhere, or if the connection attempt fails.

        """

        # EARLY CHECK: Fail fast if interface is closing before acquiring any locks
        # This prevents blocking close() which needs the address lock to proceed
        if self._closed or self.is_connection_closing:
            raise self.BLEError("Cannot connect while interface is closing")

        requested_identifier = address if address is not None else self.address
        normalized_request = sanitize_address(requested_identifier)

        # Only use address registry for explicit addresses, not discovery mode (None)
        addr_key = _addr_key(requested_identifier) if requested_identifier else None
        connected_client: Optional["BLEClient"] = None
        connected_device_key: Optional[str] = None
        connection_alias_key: Optional[str] = None

        # Apply address gating only for explicit-address connects.
        # Discovery-mode connects intentionally skip gating to avoid holding the
        # global registry lock during long-running scan/discovery operations.
        with contextlib.ExitStack() as stack:
            if addr_key is not None:
                addr_lock = stack.enter_context(_addr_lock_context(addr_key))
                stack.enter_context(addr_lock)

            # Fast suppression if a recent connect happened elsewhere.
            # Check is performed inside addr_lock to ensure consistent lock ordering.
            # Skip suppression check for discovery mode (addr_key is None)
            if (
                addr_key
                and _is_currently_connected_elsewhere(addr_key, owner=self)
                and not self._state_manager.is_connected
            ):
                logger.info(
                    "Suppressing duplicate connect to %s: recently connected elsewhere.",
                    addr_key or "unknown",
                )
                raise self.BLEError(
                    "Connection suppressed: recently connected elsewhere"
                )

            with self._connect_lock:
                # Re-check closing state inside connect_lock for extra safety
                if self._closed or self.is_connection_closing:
                    raise self.BLEError("Cannot connect while interface is closing")

                with self._state_lock:
                    existing_client = self.client
                    if (
                        existing_client
                        and existing_client.is_connected()
                        and self._connection_validator.check_existing_client(
                            existing_client,
                            normalized_request,
                            self._last_connection_request,
                            self.address,
                        )
                    ):
                        logger.debug("Already connected, skipping connect call.")
                        return existing_client

                client = self._connection_orchestrator.establish_connection(
                    address,
                    self.address,
                    self._register_notifications,
                    self._connected,
                    self._on_ble_disconnect,
                )

                device_address = getattr(
                    getattr(client, "bleak_client", None), "address", None
                )
                previous_client = None
                with self._state_lock:
                    previous_client = self.client
                    self.address = device_address
                    self.client = client
                    self._disconnect_notified = False
                    normalized_device_address = sanitize_address(device_address or "")
                    if normalized_request is not None:
                        self._last_connection_request = normalized_request
                    else:
                        self._last_connection_request = normalized_device_address

                if previous_client and previous_client is not client:
                    self._client_manager.update_client_reference(
                        client, previous_client
                    )

                # Record keys for gate marking after releasing the initial request lock.
                # This avoids nested per-address lock acquisition for different keys.
                connected_device_key = (
                    _addr_key(device_address) if device_address else None
                )
                connection_alias_key = (
                    addr_key
                    if connected_device_key
                    and addr_key
                    and addr_key != connected_device_key
                    else None
                )
                # Mark that at least one successful connection has been established
                self._ever_connected = True
                self._read_retry_count = 0
                connected_client = client

        if connected_client is None:
            # Defensive: establish_connection should have returned a client or raised.
            raise self.BLEError("Connection failed: no BLE client established")

        # Mark connected keys with deterministic lock ordering only if the same client
        # is still active; this avoids stale claim resurrection on rapid disconnects.
        with self._state_lock:
            still_active = (
                self.client is connected_client and self._state_manager.is_connected
            )

        if still_active:
            self._mark_address_keys_connected(
                connected_device_key, connection_alias_key
            )
            with self._state_lock:
                if self.client is connected_client and self._state_manager.is_connected:
                    self._connection_alias_key = connection_alias_key
        else:
            logger.debug(
                "Skipping connect gate marking for stale client result (%s).",
                getattr(connected_client, "address", "unknown"),
            )

        return connected_client

    def _handle_read_loop_disconnect(
        self, error_message: str, previous_client: "BLEClient"
    ) -> bool:
        """
        Determine whether the receive loop should continue after a BLE client disconnect.

        Parameters
        ----------
            error_message (str): Human-readable description of the disconnection cause.
            previous_client (BLEClient): The BLE client that disconnected and may be closed.

        Returns
        -------
            `true` if the read loop should continue to allow auto-reconnect, `false` otherwise.

        """
        logger.debug("Device disconnected: %s", error_message)
        should_continue = self._handle_disconnect(
            f"read_loop: {error_message}", client=previous_client
        )
        if not should_continue:
            # End our read loop immediately
            self._set_receive_wanted(False)
        return should_continue

    def _receiveFromRadioImpl(self) -> None:
        """
        Run the receive loop that reads FROMRADIO packets and delivers them to the packet handler.

        This method waits on the internal read trigger and reconnection events, performs GATT reads when a BLE client is available (with retry/backoff for transient empty reads), and forwards non-empty payloads to _handleFromRadio. It discards malformed protobufs with a warning, applies the retry policy for transient BLE errors, and on persistent or fatal BLE/OS errors initiates a safe shutdown or reconnection flow so the interface can recover or stop. The loop runs until the interface is closing or the receive thread is requested to stop.
        """
        coordinator = self.thread_coordinator
        wait_timeout = BLEConfig.RECEIVE_WAIT_TIMEOUT
        try:
            while self._should_run_receive_loop():
                # Wait for data to read, but also check periodically for reconnection
                if not coordinator.wait_for_event("read_trigger", timeout=wait_timeout):
                    # Timeout occurred, check if we were reconnected during this time
                    if self._ever_connected and coordinator.check_and_clear_event(
                        "reconnected_event"
                    ):
                        logger.debug("Detected reconnection, resuming normal operation")
                    continue
                coordinator.clear_event("read_trigger")

                while self._should_run_receive_loop():
                    # Use unified state lock
                    with self._state_lock:
                        client = self.client
                    if client is None:
                        if self.auto_reconnect:
                            logger.debug(
                                "BLE client is None; waiting for auto-reconnect"
                            )
                            # Wait briefly for reconnect or shutdown signal, then re-check
                            coordinator.wait_for_event(
                                "reconnected_event",
                                timeout=wait_timeout,
                            )
                            break  # Return to outer loop to re-check state
                        logger.debug("BLE client is None, shutting down")
                        self._set_receive_wanted(False)
                        break
                    try:
                        payload = self._read_from_radio_with_retries(client)
                        if not payload:
                            break  # Too many empty reads; exit to recheck state
                        logger.debug("FROMRADIO read: %s", payload.hex())
                        try:
                            self._handleFromRadio(payload)
                        except DecodeError as e:
                            # Log and continue on protobuf decode errors
                            logger.warning(
                                "Failed to parse FromRadio packet, discarding: %s", e
                            )
                            self._read_retry_count = 0
                            continue
                        self._read_retry_count = 0
                    except (BleakDBusError, BLEClient.BLEError) as e:
                        # Handle expected BLE disconnect/read failures.
                        if self._handle_read_loop_disconnect(repr(e), client):
                            break
                        return
                    except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
                        raise
                    except BleakError as e:
                        # Try to recover from transient BLE errors via retry policy
                        try:
                            self._handle_transient_read_error(e)
                            # If handler returns, retry is allowed - continue the read loop
                            continue
                        except self.BLEError:
                            # Retry policy exhausted, treat as fatal
                            logger.error("Fatal BLE read error after retries: %s", e)
                            if not self._state_manager.is_closing:
                                self.close()
                            return
                    except (RuntimeError, OSError) as e:
                        # Treat these as fatal errors that should close the interface
                        logger.error("Fatal error in BLE receive thread: %s", e)
                        if not self._state_manager.is_closing:
                            self.close()
                        return
                    except Exception as e:  # pragma: no cover - defensive catch-all
                        logger.exception("Unexpected error in BLE read loop")
                        if self._handle_read_loop_disconnect(repr(e), client):
                            break
                        return
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            raise
        except Exception:
            # Defensive catch-all for the receive thread; keep BLE runtime alive.
            logger.exception("Unexpected fatal error in BLE receive thread")
            if not self._state_manager.is_closing:
                with self._state_lock:
                    current_client = self.client
                should_continue = self._handle_disconnect(
                    "receive_thread_fatal", client=current_client
                )
                if not should_continue:
                    self._set_receive_wanted(False)
                    return
                # If disconnect handling requests continuation (auto-reconnect path),
                # replace this crashed receive thread so reads resume after reconnect.
                if self._should_run_receive_loop():
                    self._start_receive_thread(name="BLEReceiveRecovery")

    def _read_from_radio_with_retries(self, client: "BLEClient") -> Optional[bytes]:
        """
        Read a non-empty payload from the FROMRADIO characteristic, retrying on transient empty reads.

        Attempts up to BLEConfig.EMPTY_READ_MAX_RETRIES retries with backoff when reads yield empty payloads. Resets the suppressed-empty-read counter on a successful read and triggers a throttled empty-read warning when all retries are exhausted.

        Returns:
            bytes or None: The payload bytes if a non-empty read occurred, or `None` if no non-empty payload was obtained after retries.

        """
        for attempt in range(BLEConfig.EMPTY_READ_MAX_RETRIES + 1):
            payload = client.read_gatt_char(FROMRADIO_UUID, timeout=GATT_IO_TIMEOUT)
            if payload:
                self._suppressed_empty_read_warnings = 0
                return payload
            if attempt < BLEConfig.EMPTY_READ_MAX_RETRIES:
                _sleep(self._empty_read_policy.get_delay(attempt))
        self._log_empty_read_warning()
        return None

    def _handle_transient_read_error(self, error: BleakError) -> None:
        """
        Apply the configured transient-read retry policy for a BLE read error.

        May increment an internal retry counter and sleep for the policy delay to allow the caller to retry the read. If the policy indicates retries are exhausted, the internal counter is reset and a BLEInterface.BLEError is raised.

        Parameters
        ----------
            error (BleakError): The BLE read error to evaluate.

        Raises
        ------
            BLEInterface.BLEError: When the retry policy is exhausted and the read should be treated as persistent.

        """
        transient_policy = self._transient_read_policy
        if transient_policy.should_retry(self._read_retry_count):
            attempt_index = self._read_retry_count
            self._read_retry_count += 1
            logger.debug(
                "Transient BLE read error, retrying (%d/%d)",
                self._read_retry_count,
                BLEConfig.TRANSIENT_READ_MAX_RETRIES,
            )
            _sleep(transient_policy.get_delay(attempt_index))
            return
        self._read_retry_count = 0
        logger.debug("Persistent BLE read error after retries", exc_info=True)
        raise self.BLEError(ERROR_READING_BLE) from error

    def _log_empty_read_warning(self) -> None:
        """
        Throttle and emit warnings for repeated empty FROMRADIO BLE reads.

        When empty read events occur repeatedly, this method either logs a warning (including the number
        of suppressed repeats since the last warning) if the configured cooldown has elapsed, or
        increments an internal suppressed counter and emits a debug-level message while still within the
        cooldown window.
        """
        now = time.monotonic()
        cooldown = BLEConfig.EMPTY_READ_WARNING_COOLDOWN
        if now - self._last_empty_read_warning >= cooldown:
            suppressed = self._suppressed_empty_read_warnings
            message = f"Exceeded max retries for empty BLE read from {FROMRADIO_UUID}"
            if suppressed:
                message = (
                    f"{message} (suppressed {suppressed} repeats in the last "
                    f"{cooldown:.0f}s)"
                )
            logger.warning(message)
            self._last_empty_read_warning = now
            self._suppressed_empty_read_warnings = 0
            return

        self._suppressed_empty_read_warnings += 1
        logger.debug(
            "Suppressed repeated empty BLE read warning (%d within %.0fs window)",
            self._suppressed_empty_read_warnings,
            cooldown,
        )

    def _sendToRadioImpl(self, toRadio) -> None:
        """
        Send a protobuf message to the radio over the TORADIO BLE characteristic.

        Parameters
        ----------
            toRadio: Protobuf message exposing SerializeToString(); represents the outbound radio packet.

        Raises
        ------
            BLEInterface.BLEError: If the BLE write operation fails.

        Notes
        -----
            The call is a no-op if the serialized payload is empty or the interface is closing.

        """
        b: bytes = toRadio.SerializeToString()
        if not b:
            return

        write_successful = False
        # Grab the current client under the shared lock, but perform the blocking write outside
        with self._state_lock:
            client = self.client

        if not client or (self.is_connection_closing and not toRadio.disconnect):
            logger.debug(
                "Skipping TORADIO write: no BLE client or interface is closing."
            )
            return

        logger.debug("TORADIO write: %s", b.hex())
        try:
            # Use write-with-response to ensure delivery is acknowledged by the peripheral
            client.write_gatt_char(
                TORADIO_UUID, b, response=True, timeout=GATT_IO_TIMEOUT
            )
            write_successful = True
        except (BleakError, BLEClient.BLEError, RuntimeError, OSError) as e:
            # Log detailed error information and wrap in our interface exception
            logger.debug(
                "Error during write operation: %s",
                type(e).__name__,
                exc_info=True,
            )
            raise self.BLEError(ERROR_WRITING_BLE) from e

        if write_successful:
            # Brief delay to allow write to propagate before triggering read
            _sleep(BLEConfig.SEND_PROPAGATION_DELAY)
            self.thread_coordinator.set_event(
                "read_trigger"
            )  # Wake receive loop to process any response

    def close(self) -> None:
        """
        Shuts down the BLE interface and releases all associated resources.

        This method performs an idempotent, orderly shutdown: it stops background threads and reconnection activity, disconnects and closes any active BLE client, cleans up notification and discovery managers, unregisters the atexit handler, and publishes a final disconnected notification while waiting briefly for pending disconnect-related notifications to flush.
        """
        # Use unified state lock
        with self._state_lock:
            if self._closed:
                logger.debug(
                    "BLEInterface.close called on already closed interface; ignoring"
                )
                return
            was_closing = self._state_manager.is_closing
            # Mark closed immediately to prevent overlapping cleanup in concurrent calls
            self._closed = True
            if was_closing:
                logger.debug(
                    "BLEInterface.close called while another shutdown is in progress; continuing with cleanup"
                )
            # Transition to DISCONNECTING only if we're not already fully disconnected or mid-disconnect
            if self._state_manager.state not in (
                ConnectionState.DISCONNECTED,
                ConnectionState.DISCONNECTING,
            ):
                self._state_manager.transition_to(ConnectionState.DISCONNECTING)

        # Release lock before calling MeshInterface.close() to avoid deadlock
        # If MeshInterface.close() acquires locks that other paths also acquire, holding state_lock would cause lock inversion
        if self._shutdown_event:
            self._shutdown_event.set()

        self._set_receive_wanted(False)  # Tell the thread we want it to stop
        self.thread_coordinator.wake_waiting_threads(
            "read_trigger", "reconnected_event"
        )  # Wake all waiting threads
        if self._receiveThread:
            if self._receiveThread is threading.current_thread():
                logger.debug("close() called from receive thread; skipping self-join")
            else:
                self.thread_coordinator.join_thread(
                    self._receiveThread, timeout=RECEIVE_THREAD_JOIN_TIMEOUT
                )
                if self._receiveThread.is_alive():
                    logger.warning(
                        "BLE receive thread did not exit within %.1fs",
                        RECEIVE_THREAD_JOIN_TIMEOUT,
                    )
            self._receiveThread = None

        # Close parent interface (stops publishing thread, etc.)
        self.error_handler.safe_execute(
            lambda: MeshInterface.close(self), error_msg="Error closing mesh interface"
        )

        if self._exit_handler:
            with contextlib.suppress(ValueError):
                atexit.unregister(self._exit_handler)
            self._exit_handler = None

        # Use unified state lock
        with self._state_lock:
            client = self.client
            # Don't close client if it was already replaced (race condition)
            # Only close if it's still the current client
            if client is not None:
                self.client = None
        # Only close the client if we were the one who replaced it
        # If it was replaced by a reconnection, the new connection path will clean it up
        if client is not None:
            self._notification_manager.unsubscribe_all(
                client, timeout=NOTIFICATION_START_TIMEOUT
            )
            self._disconnect_and_close_client(client)
        self._notification_manager.cleanup_all()

        # Use unified state lock
        # Send disconnected indicator if not already notified
        notify = False
        with self._state_lock:
            if not self._disconnect_notified:
                self._disconnect_notified = True
                notify = True

        if notify:
            self._disconnected()  # send the disconnected indicator up to clients
            self._wait_for_disconnect_notifications()

        if self._discovery_manager is not None:
            self.error_handler.safe_cleanup(
                self._discovery_manager.close, "discovery manager close"
            )
            self._discovery_manager = None

        # Clean up thread coordinator
        self.thread_coordinator.cleanup()
        # Use unified state lock
        with self._state_lock:
            # Record final state as DISCONNECTED for observers; instance remains closed.
            self._state_manager.transition_to(ConnectionState.DISCONNECTED)
            alias_key = self._connection_alias_key
            self._connection_alias_key = None
        close_key = _addr_key(self.address)
        self._mark_address_keys_disconnected(close_key, alias_key)

    def _wait_for_disconnect_notifications(
        self, timeout: Optional[float] = None
    ) -> None:
        """
        Wait up to `timeout` seconds for the publish queue to flush before continuing.

        Triggers a flush request on the publishing thread and waits for a flush event. If `timeout` is reached and the publishing thread is alive, a debug message is logged. If the publishing thread is not running when the timeout elapses, the publish queue is drained synchronously on the current thread. Exceptions raised while requesting the flush are caught and logged.

        Parameters
        ----------
            timeout (float | None): Maximum seconds to wait for the publish queue to flush. If `None`, uses `DISCONNECT_TIMEOUT_SECONDS`.

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

    def _disconnect_and_close_client(self, client: "BLEClient") -> None:
        """
        Ensure the given BLE client is disconnected and its resources are released.

        Parameters
        ----------
            client (BLEClient): BLE client to disconnect and close; operation is idempotent and safe to call on already-closed clients.

        """
        self._client_manager.safe_close_client(client)

    def _drain_publish_queue(self, flush_event: Event) -> None:
        """
        Drain and run pending publish callbacks on the current thread until the queue is empty or the provided event is set.

        Each callback is executed via the interface's error handler; exceptions raised by callbacks are caught and logged so draining continues.

        Parameters
        ----------
            flush_event (Event): When set, stop draining immediately.

        """
        queue = getattr(publishingThread, "queue", None)
        if queue is None:
            return
        iterations = 0
        while not flush_event.is_set():
            if iterations >= MAX_DRAIN_ITERATIONS:
                logger.debug(
                    "Stopping publish queue drain after %d callbacks to avoid shutdown starvation",
                    MAX_DRAIN_ITERATIONS,
                )
                break
            try:
                runnable = queue.get_nowait()
            except Empty:
                break
            iterations += 1
            self.error_handler.safe_execute(
                runnable, error_msg="Error in deferred publish callback", reraise=False
            )

    def _disconnected(self) -> None:
        """
        Publish the legacy meshtastic.connection.status event when the interface disconnects.

        Enqueues a publish of the connection status (interface=self) to maintain backward compatibility with tests and integrations. Any exceptions raised while queueing or during publish are suppressed and logged at debug level.
        """
        super()._disconnected()
        # Also publish connection.status event for test compatibility
        # Import from mesh_interface to respect test monkeypatching
        from meshtastic.mesh_interface import (
            pub as mesh_pub,  # type: ignore[attr-defined]
        )

        def _publish_status():
            """
            Publish a disconnected connection status to the mesh publisher.

            Sends a "meshtastic.connection.status" message with `connected=False` via `mesh_pub`. Any exception raised while publishing is suppressed and logged at debug level.
            """
            try:
                mesh_pub.sendMessage(
                    "meshtastic.connection.status", interface=self, connected=False
                )
            except Exception:
                logger.debug("Error publishing disconnect status", exc_info=True)

        try:
            publishingThread.queueWork(_publish_status)
        except Exception:
            logger.debug("Error queuing disconnect status publish", exc_info=True)

    def _connected(self) -> None:
        """Override to also publish connection status event for backwards compatibility."""
        super()._connected()
        # Also publish connection.status event for test compatibility
        from meshtastic.mesh_interface import (
            pub as mesh_pub,  # type: ignore[attr-defined]
        )

        def _publish_status():
            """
            Publish a meshtastic.connection.status notification indicating this interface is connected.

            Calls the mesh publisher with `interface=self` and `connected=True`. Any exceptions raised while publishing are suppressed and logged at debug level.
            """
            try:
                mesh_pub.sendMessage(
                    "meshtastic.connection.status", interface=self, connected=True
                )
            except Exception:
                logger.debug("Error publishing connect status", exc_info=True)

        try:
            publishingThread.queueWork(_publish_status)
        except Exception:
            logger.debug("Error queuing connect status publish", exc_info=True)
