"""Main BLE interface class."""

import asyncio
import atexit
import contextlib
import io
import logging
import struct
import time
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from queue import Empty
from threading import Event, Thread
from typing import Any, Callable, List, Optional, TYPE_CHECKING, cast

from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner, BLEDevice
from bleak.exc import BleakDBusError, BleakError

from meshtastic import publishingThread
from meshtastic.mesh_interface import MeshInterface

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.connection import (
    ClientManager,
    ConnectionOrchestrator,
    ConnectionValidator,
)
from meshtastic.interfaces.ble.constants import (
    BLEAK_VERSION,
    BLEConfig,
    CONNECTION_TIMEOUT,
    DISCONNECT_TIMEOUT_SECONDS,
    ERROR_CONNECTION_FAILED,
    ERROR_MULTIPLE_DEVICES,
    ERROR_NO_PERIPHERAL_FOUND,
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
    _bleak_supports_connected_fallback,
    logger,
)
from meshtastic.interfaces.ble.coordination import ThreadCoordinator
from meshtastic.interfaces.ble.discovery import DiscoveryManager, parse_scan_response
from meshtastic.interfaces.ble.errors import BLEErrorHandler, DecodeError
from meshtastic.interfaces.ble.notifications import NotificationManager
from meshtastic.interfaces.ble.policies import RetryPolicy
from meshtastic.interfaces.ble.reconnection import ReconnectScheduler
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from meshtastic.interfaces.ble.utils import sanitize_address
from meshtastic.interfaces.ble.utils import _sleep

from meshtastic.protobuf import mesh_pb2


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
        Initialize the BLEInterface, start its background receive thread, and attempt an initial connection to a Meshtastic BLE device.

        Parameters:
            address (Optional[str]): BLE address or name to connect to; if None, any available Meshtastic device may be used.
            noProto (bool): If True, disable protobuf-based protocol handling.
            debugOut (Optional[io.TextIOWrapper]): Stream for debug output, if provided.
            noNodes (bool): If True, skip reading the device's node list on startup.
            timeout (int): Timeout in seconds used for connection/config waits.
            auto_reconnect (bool): If True, keep the interface alive across unexpected disconnects and schedule automatic reconnection; if False, close the interface on disconnect.
        """

        # Thread safety and state management
        # Unified state-based lock system replacing multiple locks and boolean flags
        self._state_manager = BLEStateManager()  # Centralized state tracking
        self._state_lock = self._state_manager.lock  # Direct access to unified lock
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
        self._notification_manager = NotificationManager()
        self._discovery_manager = DiscoveryManager()
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
        self._ever_connected = False  # Track first successful connection to tune logging

        # Initialize parent interface
        MeshInterface.__init__(
            self, debugOut=debugOut, noProto=noProto, noNodes=noNodes, timeout=timeout
        )

        # Initialize retry counter for transient read errors
        self._read_retry_count = 0
        self._last_empty_read_warning = 0.0
        self._suppressed_empty_read_warnings = 0

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
            with self._state_lock:
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
        except BaseException:
            # Allow system-exiting exceptions to propagate
            raise

    def __repr__(self):
        """
        Produce a compact textual representation of the BLEInterface including its address and enabled feature flags.

        Returns:
            repr_str (str): A string of the form `BLEInterface(...)` containing the address, `debugOut` when set, and boolean flags for `noProto`, `noNodes`, and `auto_reconnect` when those differ from their defaults.
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
        Handle a BLE client disconnection and either schedule an automatic reconnect or start shutdown.

        Parameters:
            source (str): Short tag identifying where the disconnect originated (e.g., "bleak_callback", "read_loop", "explicit").
            client (Optional[BLEClient]): Associated BLEClient, if available.
            bleak_client (Optional[BleakRootClient]): Underlying Bleak client, if available.

        Returns:
            bool: `True` if the interface remains active and will attempt auto-reconnect, `False` if the interface has begun shutdown.
        """
        # Use state manager for disconnect validation
        current_state = self._state_manager.state
        if current_state == ConnectionState.CONNECTING:
            logger.debug(
                "Ignoring disconnect from %s while a connection is in progress.", source
            )
            return True
        if self.is_connection_closing:
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
            # Use unified state lock
            with self._state_lock:
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

            # Transition to DISCONNECTED state on disconnect
            if previous_client:
                self._state_manager.transition_to(ConnectionState.DISCONNECTED)
                self._disconnected()
                # Close previous client asynchronously
                close_thread = self.thread_coordinator.create_thread(
                    target=self._client_manager.safe_close_client,
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
            # Transition to DISCONNECTING state when closing
            self._state_manager.transition_to(ConnectionState.DISCONNECTING)
            # Auto-reconnect disabled - close interface
            logger.debug("Auto-reconnect disabled, closing interface.")
            self.close()
            return False

    def _on_ble_disconnect(self, client: BleakRootClient) -> None:
        """
        Handle a disconnect event invoked by the Bleak library.

        Signals the interface to process a BLE disconnect for the provided Bleak client.

        Parameters:
            client (BleakRootClient): The Bleak client instance that was disconnected.
        """
        self._handle_disconnect("bleak_callback", bleak_client=client)

    def _schedule_auto_reconnect(self) -> None:
        """
        Schedule an auto-reconnect worker to repeatedly attempt BLE reconnection until a connection succeeds or the interface shuts down.

        This is a no-op when auto-reconnect is disabled or the interface is closing. Clears the internal shutdown event and delegates the actual scheduling to the reconnect scheduler.
        """

        if not self.auto_reconnect:
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

    def _handle_malformed_fromnum(self, reason: str, exc_info: bool = False):
        """
        Track malformed FROMNUM notifications and warn when a threshold is reached.

        Increments an internal malformed-notification counter, logs the provided reason (including exception traceback if requested),
        and when the counter reaches MALFORMED_NOTIFICATION_THRESHOLD logs a warning and resets the counter to zero.

        Parameters:
                reason (str): Description of why the notification was considered malformed.
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
        Handle a FROMNUM characteristic notification and wake the read loop.

        Parses a 4-byte little-endian unsigned 32-bit integer from the notification payload and updates malformed-notification tracking:
        - On successful parse, resets the malformed-notification counter and logs the parsed value.
        - On parse failure or unexpected length, increments the malformed-notification counter and may emit a warning when a threshold is reached.

        Also signals the receive/read loop by setting the "read_trigger" event on the thread coordinator.

        Parameters:
            _ (Any): Unused sender parameter provided by the BLE library.
            b (bytearray): Notification payload expected to contain a 4-byte little-endian unsigned 32-bit integer.
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

        def _safe_call(handler, sender, data, error_msg):
            """
            Invoke a notification handler via the interface's error handler to ensure exceptions are caught and reported.

            Parameters:
                handler (callable): Function to invoke with (sender, data).
                sender (object): Origin of the notification passed to the handler.
                data (bytes | bytearray | object): Payload delivered to the handler.
                error_msg (str): Message used by the error handler if the handler raises an exception.
            """
            self.error_handler.safe_execute(
                lambda: handler(sender, data),
                error_msg=error_msg,
            )

        def _safe_legacy_handler(sender, data):
            """
            Call the legacy log-radio notification handler with the given notification payload, ensuring any handler errors are captured and reported.

            Parameters:
                sender: The notification source (characteristic or client) that produced the payload.
                data (bytes or bytearray): Raw notification payload delivered by the BLE characteristic.
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

            Parameters:
                sender: The notification sender (characteristic or client) — may be unused by the handler.
                data (bytes or bytearray): Raw notification payload from the BLE device.

            Returns:
                None
            """
            _safe_call(
                self.log_radio_handler,
                sender,
                data,
                "Error in log notification handler",
            )

        def _safe_from_num_handler(sender, data):
            """
            Invoke the FROMNUM notification handler through a safe wrapper that catches and reports handler errors.

            Parameters:
                sender: The notification source (typically the BLE characteristic or client).
                data: The raw payload bytes received from the FROMNUM characteristic.
            """
            _safe_call(
                self.from_num_handler,
                sender,
                data,
                "Error in FROMNUM notification handler",
            )

        def _get_or_create_handler(
            uuid: str, factory: Callable[[], Callable[[Any, Any], None]]
        ):
            """
            Ensure a notification handler exists for the given UUID, creating and subscribing one if necessary.

            Parameters:
                uuid (str): The notification characteristic UUID to get or create a handler for.
                factory (Callable[[], Callable[[Any, Any], None]]): Factory that returns a handler callable accepting (sender, data).

            Returns:
                handler (Callable[[Any, Any], None]): The existing or newly created notification handler.
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
        Await an awaitable and raise a BLEError if it does not complete within the given timeout.

        Parameters:
            awaitable (Awaitable): The awaitable to await.
            timeout (Optional[float]): Maximum time in seconds to wait; if None, wait indefinitely.
            label (str): Short label used in the timeout error message.

        Returns:
            The result produced by the awaited object.

        Raises:
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

        Performs a timed BLE scan and returns devices whose advertisements include the Meshtastic service UUID.

        Returns:
            List[BLEDevice]: BLEDevice instances that advertise the Meshtastic service UUID; empty list if none are found.
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
                return parse_scan_response(response)
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
        Find a Meshtastic BLE device by address or device name.

        Parameters:
            address (Optional[str]): Address or device name to match. Comparison ignores case and common separators
                (':', '-', '_', and spaces). If None, any discovered Meshtastic device may be returned.

        Returns:
            BLEDevice: The matched BLE device. If `address` is None and multiple devices are discovered, the first discovered device is returned.

        Raises:
            BLEInterface.BLEError: If no Meshtastic devices are found, or if `address` was provided and multiple matching devices are found.
        """

        # Surface DBus failures to allow higher-level backoff
        addressed_devices = self._discovery_manager.discover_devices(address)

        if len(addressed_devices) == 0:
            if address:
                logger.warning(
                    "No peripherals found for %s via scan; attempting direct address connect",
                    address,
                )
                return BLEDevice(address=address, name=address, details={})
            raise self.BLEError(ERROR_NO_PERIPHERALS_FOUND)
        if len(addressed_devices) == 1:
            return addressed_devices[0]
        if address and len(addressed_devices) > 1:
            # Build a list of found devices for the error message
            device_list = "\n".join(
                [f"- {d.name} ({d.address})" for d in addressed_devices]
            )
            raise self.BLEError(ERROR_MULTIPLE_DEVICES.format(address, device_list))
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
        return sanitize_address(address)

    @property
    def connection_state(self) -> ConnectionState:
        """
        Retrieve the current connection state.

        Returns:
            ConnectionState: The current connection state from the internal state manager.
        """
        return self._state_manager.state

    @property
    def is_connection_connected(self) -> bool:
        """
        Report whether the BLE interface currently has an active connection.

        Returns:
            True if a connection is active, False otherwise.
        """
        return self._state_manager.is_connected

    @property
    def is_connection_closing(self) -> bool:
        """
        Indicates whether the interface is in the process of closing.

        Returns:
            bool: `True` if the interface is shutting down, `False` otherwise.
        """
        return self._state_manager.is_closing

    @property
    def can_initiate_connection(self) -> bool:
        """
        Return whether initiating a new BLE connection is permitted by the state manager.

        Returns:
            `True` if a new connection may be started, `False` otherwise.
        """
        return self._state_manager.can_connect

    def connect(self, address: Optional[str] = None) -> "BLEClient":
        """
        Establishes a BLE connection to a Meshtastic device identified by the given address or by automatic discovery.

        Parameters:
            address (Optional[str]): BLE address or device name to connect to; if None, the interface will attempt to discover a suitable device.

        Returns:
            BLEClient: The connected BLE client for the selected device. If already connected to a matching device, the existing client is returned.

        Notes:
            On connection failure any client opened during the attempt is closed before the exception is propagated.
        """

        requested_identifier = address if address is not None else self.address
        normalized_request = BLEInterface._sanitize_address(requested_identifier)

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

        device_address = getattr(getattr(client, "bleak_client", None), "address", None)
        previous_client = None
        with self._state_lock:
            previous_client = self.client
            self.address = device_address
            self.client = client
            self._disconnect_notified = False
            normalized_device_address = BLEInterface._sanitize_address(
                device_address or ""
            )
            if normalized_request is not None:
                self._last_connection_request = normalized_request
            else:
                self._last_connection_request = normalized_device_address

        if previous_client and previous_client is not client:
            self._client_manager.update_client_reference(client, previous_client)

        # Mark that at least one successful connection has been established
        self._ever_connected = True
        self._read_retry_count = 0
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

    def _receiveFromRadioImpl(self) -> None:
        """
        Run the receive loop that reads inbound packets from the BLE FROMRADIO characteristic and delivers them to the interface packet handler.

        Waits for the internal read trigger, performs GATT reads when a connected BLE client is available, forwards non-empty payloads to _handleFromRadio, and tolerates transient empty reads with retry/backoff. Coordinates with reconnection logic (e.g., reconnected events) when the client becomes unavailable and, on persistent or fatal errors, initiates a safe interface shutdown by scheduling close() on a background thread.
        """
        try:
            while self._want_receive:
                # Wait for data to read, but also check periodically for reconnection
                if not self.thread_coordinator.wait_for_event(
                    "read_trigger", timeout=BLEConfig.RECEIVE_WAIT_TIMEOUT
                ):
                    # Timeout occurred, check if we were reconnected during this time
                    if self.thread_coordinator.check_and_clear_event(
                        "reconnected_event"
                    ):
                        if self._ever_connected:
                            logger.debug(
                                "Detected reconnection, resuming normal operation"
                            )
                    continue
                self.thread_coordinator.clear_event("read_trigger")

                while self._want_receive:
                    # Use unified state lock
                    with self._state_lock:
                        client = self.client
                    if client is None:
                        if self.auto_reconnect:
                            logger.debug(
                                "BLE client is None; waiting for auto-reconnect"
                            )
                            # Wait briefly for reconnect or shutdown signal, then re-check
                            self.thread_coordinator.wait_for_event(
                                "reconnected_event",
                                timeout=BLEConfig.RECEIVE_WAIT_TIMEOUT,
                            )
                            break  # Return to outer loop to re-check state
                        logger.debug("BLE client is None, shutting down")
                        self._want_receive = False
                        break
                    try:
                        payload = self._read_from_radio_with_retries(client)
                        if not payload:
                            break  # Too many empty reads; exit to recheck state
                        logger.debug("FROMRADIO read: %s", payload.hex())
                        self._handleFromRadio(payload)
                        self._read_retry_count = 0
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
                        self._handle_transient_read_error(e)
                        continue
                    except BLEClient.BLEError as e:
                        if self._handle_read_loop_disconnect(str(e), client):
                            break
                        return
                    except asyncio.CancelledError as e:  # pragma: no cover - defensive
                        if self._handle_read_loop_disconnect(str(e), client):
                            break
                        return
        except Exception as e:
            # Defensive catch-all for the receive thread; keep BLE runtime alive.
            logger.exception(
                "Fatal error in BLE receive thread, closing interface: %s", e
            )
            if not self._state_manager.is_closing:
                # Use a thread to avoid deadlocks if close() waits for this thread
                error_close_thread = self.thread_coordinator.create_thread(
                    target=self.close, name="BLECloseOnError", daemon=True
                )
                self.thread_coordinator.start_thread(error_close_thread)
        except BaseException:
            # Propagate system-level exceptions
            raise

    def _read_from_radio_with_retries(self, client: "BLEClient") -> Optional[bytes]:
        """
        Attempt to read a non-empty payload from the FROMRADIO characteristic, retrying on transient empty responses.

        Returns:
            The payload bytes read from FROMRADIO, or `None` if no non-empty payload was obtained after the configured retry attempts.
        """
        empty_read_policy = RetryPolicy.empty_read()
        for attempt in range(BLEConfig.EMPTY_READ_MAX_RETRIES + 1):
            payload = client.read_gatt_char(
                FROMRADIO_UUID, timeout=BLEConfig.GATT_IO_TIMEOUT
            )
            if payload:
                self._suppressed_empty_read_warnings = 0
                return payload
            if attempt < BLEConfig.EMPTY_READ_MAX_RETRIES:
                _sleep(empty_read_policy.get_delay(attempt))
        self._log_empty_read_warning()
        return None

    def _handle_transient_read_error(self, error: BleakError) -> None:
        """
        Apply the configured transient-read retry policy for a Bleak read error.

        If the policy indicates a retry is allowed, increments the internal read-retry counter,
        waits the policy-specified delay, and returns so the caller may retry the read.
        If retries are exhausted, resets the counter and raises BLEError with ERROR_READING_BLE.

        Parameters:
            error (BleakError): The BLE read error to evaluate and handle.

        Raises:
            BLEInterface.BLEError: When the retry policy is exhausted and the read should be treated as persistent.
        """
        transient_policy = RetryPolicy.transient_error()
        if transient_policy.should_retry(self._read_retry_count):
            self._read_retry_count += 1
            logger.debug(
                "Transient BLE read error, retrying (%d/%d)",
                self._read_retry_count,
                BLEConfig.TRANSIENT_READ_MAX_RETRIES,
            )
            _sleep(transient_policy.get_delay(self._read_retry_count))
            return
        self._read_retry_count = 0
        logger.debug("Persistent BLE read error after retries", exc_info=True)
        raise self.BLEError(ERROR_READING_BLE) from error

    def _log_empty_read_warning(self) -> None:
        """
        Emit a throttled warning when repeated FROMRADIO reads return empty payloads.
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
        Serialize and write a protobuf message to the radio via the TORADIO BLE characteristic.

        Serializes `toRadio` with SerializeToString() and performs a write-with-response to the TORADIO characteristic when a BLE client is available. The call is a no-op if the serialized payload is empty or the interface is closing. After a successful write, waits briefly to allow propagation and signals the read trigger.

        Parameters:
            toRadio: Protobuf message object exposing SerializeToString() representing the outbound radio packet.

        Raises:
            BLEInterface.BLEError: If the BLE write operation fails.
        """
        b: bytes = toRadio.SerializeToString()
        if not b:
            return

        write_successful = False
        # Grab the current client under the shared lock, but perform the blocking write outside
        with self._state_lock:
            client = self.client

        if not client or self.is_connection_closing:
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
        Shut down the BLE interface, stop background activity, disconnect the BLE client, and perform cleanup.

        This method is idempotent and safe to call multiple times. It stops the receive loop and reconnection waits, unregisters the
            atexit handler, disconnects and closes any active BLE client, emits a disconnected notification if not already sent,
            and waits briefly for pending disconnect-related notifications and the receive thread to finish.
        """
        # Use unified state lock
        with self._state_lock:
            if self._closed:
                logger.debug(
                    "BLEInterface.close called on already closed interface; ignoring"
                )
                return
            # Mark closed immediately to prevent overlapping cleanup in concurrent calls
            self._closed = True
            if self.is_connection_closing:
                logger.debug(
                    "BLEInterface.close called while another shutdown is in progress; continuing with cleanup"
                )
            # Transition to DISCONNECTING only if we're not already fully disconnected or mid-disconnect
            if self._state_manager.state not in (
                ConnectionState.DISCONNECTED,
                ConnectionState.DISCONNECTING,
            ):
                self._state_manager.transition_to(ConnectionState.DISCONNECTING)
        if self._shutdown_event:
            self._shutdown_event.set()
        self._notification_manager.cleanup_all()

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
                if self._receiveThread is threading.current_thread():
                    logger.debug(
                        "close() called from receive thread; skipping self-join"
                    )
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

        if self._exit_handler:
            with contextlib.suppress(ValueError):
                atexit.unregister(self._exit_handler)
            self._exit_handler = None

        # Use unified state lock
        with self._state_lock:
            client = self.client
            self.client = None
        if client:
            self._disconnect_and_close_client(client)

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

        # Clean up thread coordinator
        self.thread_coordinator.cleanup()
        # Use unified state lock
        with self._state_lock:
            # Reset state to DISCONNECTED so future reconnects with this instance are permitted.
            self._state_manager.transition_to(ConnectionState.DISCONNECTED)

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
        Disconnect the given BLE client and ensure its underlying resources are released.

        Attempts a graceful disconnect using the module DISCONNECT_TIMEOUT_SECONDS; if disconnect times out or fails,
        the client is closed forcefully via the client manager to release resources.

        Parameters:
            client (BLEClient): BLE client instance to disconnect and close.
        """
        try:
            self.error_handler.safe_cleanup(
                lambda: client.disconnect(await_timeout=DISCONNECT_TIMEOUT_SECONDS)
            )
        finally:
            self._client_manager.safe_close_client(client)

    def _drain_publish_queue(self, flush_event: Event) -> None:
        """
        Drain and run pending publish callbacks on the current thread until the queue is empty or `flush_event` is set.

        Each callable is executed via the interface's error handler; exceptions from callbacks are caught and logged so draining continues.

        Parameters:
            flush_event (Event): When set, stops draining immediately.
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
