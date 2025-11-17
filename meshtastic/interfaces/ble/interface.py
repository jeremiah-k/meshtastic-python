"""Bluetooth interface."""

import atexit
import contextlib
import io
import logging
import random
import re
import struct
import time
from enum import Enum
from queue import Empty
from threading import Event, RLock, Thread, current_thread
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING, cast

from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner, BLEDevice
from bleak.exc import BleakDBusError, BleakError

if TYPE_CHECKING:
    from .client import BLEClient

    class DecodeError(Exception):
        """Fallback DecodeError type used for static type checking."""

        pass
else:  # pragma: no cover - import real exception only at runtime
    from google.protobuf.message import DecodeError

from meshtastic import publishingThread
from meshtastic.mesh_interface import MeshInterface

from ...protobuf import mesh_pb2
from .config import (
    BLEAK_VERSION,
    BLEConfig,
    CONNECTION_TIMEOUT,
    FROMNUM_UUID,
    FROMRADIO_UUID,
    GATT_IO_TIMEOUT,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    NOTIFICATION_START_TIMEOUT,
    SERVICE_UUID,
    TORADIO_UUID,
)
from .state import BLEStateManager, ConnectionState
from .reconnect import (
    ReconnectPolicy,
    ReconnectScheduler,
    ReconnectWorker,
    RetryPolicy,
)
from .threads import ThreadCoordinator
from .client import BLEClient
from .error_handler import BLEErrorHandler
from .discovery import DiscoveryManager
from .util import build_ble_device, sanitize_address
from .connection import (
    ClientManager,
    ConnectionOrchestrator,
    ConnectionValidator,
)
from .notifications import NotificationManager
from .exceptions import BLEError

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

def _sleep(delay: float) -> None:
    """Allow callsites to throttle activity (wrapped for easier testing)."""
    time.sleep(delay)


# Error message constants
ERROR_TIMEOUT = "{0} timed out after {1:.1f} seconds"
ERROR_MULTIPLE_DEVICES = (
    "Multiple Meshtastic BLE peripherals found matching '{0}'. Please specify one:\n{1}"
)

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

    BLEError = BLEError

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
        # Unified state-based lock system replacing multiple locks and boolean flags
        self._state_manager = BLEStateManager()  # Centralized state tracking
        self._state_lock = self._state_manager.lock  # Direct access to unified lock
        self._closed: bool = (
            False  # Tracks completion of shutdown for idempotent close()
        )
        self._exit_handler = None
        self.address = address
        self._last_connection_request: Optional[str] = sanitize_address(
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
            self._state_manager, self._state_lock
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

    def __repr__(self):
        """
        Produce a compact textual representation of the BLEInterface including its address and relevant feature flags.

        Returns
        -------
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
        # Use state manager for disconnect validation
        if self.is_connection_closing:
            logger.debug("Ignoring disconnect from %s during shutdown.", source)
            return False

        # Determine which client we're dealing with
        target_client = client
        resolved_client = target_client

        duplicate_disconnect = False
        stale_bleak_callback = False
        stale_client_disconnect = False

        if self.auto_reconnect:
            previous_client = None
            # Use unified state lock
            with self._state_lock:
                # Prevent duplicate disconnect notifications
                if self._disconnect_notified:
                    duplicate_disconnect = True
                else:
                    current_client = self.client

                    if (
                        not resolved_client
                        and bleak_client
                        and current_client
                    ):
                        current_bleak = getattr(
                            current_client, "bleak_client", None
                        )
                        if current_bleak is bleak_client:
                            resolved_client = current_client
                        else:
                            stale_bleak_callback = True

                    # Ignore stale client disconnects (from previous connections)
                    if (
                        not stale_bleak_callback
                        and resolved_client
                        and current_client
                        and resolved_client is not current_client
                    ):
                        stale_client_disconnect = True
                    elif not stale_bleak_callback:
                        previous_client = current_client
                        self.client = None
                        self._disconnect_notified = True

        address = "unknown"
        if resolved_client:
            address = getattr(resolved_client, "address", repr(resolved_client))
        elif bleak_client:
            address = getattr(bleak_client, "address", repr(bleak_client))

        logger.debug("BLE client %s disconnected (source: %s).", address, source)

        if self.auto_reconnect:
            if duplicate_disconnect:
                logger.debug("Ignoring duplicate disconnect from %s.", source)
                return True

            if stale_bleak_callback or stale_client_disconnect:
                if stale_bleak_callback:
                    logger.debug(
                        "Ignoring stale disconnect from %s (mismatched Bleak client).",
                        source,
                    )
                else:
                    logger.debug("Ignoring stale disconnect from %s.", source)
                return True

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

        def _safe_call(handler, sender, data, error_msg):
            self.error_handler.safe_execute(
                lambda: handler(sender, data),
                error_msg=error_msg,
            )

        def _safe_legacy_handler(sender, data):
            _safe_call(
                self.legacy_log_radio_handler,
                sender,
                data,
                "Error in legacy log notification handler",
            )

        def _safe_log_handler(sender, data):
            _safe_call(
                self.log_radio_handler,
                sender,
                data,
                "Error in log notification handler",
            )

        def _safe_from_num_handler(sender, data):
            _safe_call(
                self.from_num_handler,
                sender,
                data,
                "Error in FROMNUM notification handler",
            )

        def _get_or_create_handler(
            uuid: str, factory: Callable[[], Callable[[Any, Any], None]]
        ):
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
        except (BleakError, BleakDBusError, RuntimeError) as e:
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
    def scan() -> List[BLEDevice]:
        """
        Scan for BLE devices advertising the Meshtastic service UUID.

        Performs a timed BLE scan, handles variations in BleakScanner.discover() return formats, and returns devices whose
        advertisements include the Meshtastic service UUID.

        Returns
        -------
            List[BLEDevice]: Devices whose advertisements include the Meshtastic service UUID; empty list if none are found.

        """
        with BLEClient(log_if_no_address=False) as client:
            logger.debug(
                "Scanning for BLE devices (takes %.0f seconds)...",
                BLEConfig.BLE_SCAN_TIMEOUT,
            )
            response = client.discover(
                timeout=BLEConfig.BLE_SCAN_TIMEOUT,
                return_adv=True,
                service_uuids=[SERVICE_UUID],
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

        addressed_devices = self._discovery_manager.discover_devices(address)

        if address:
            sanitized_address = sanitize_address(address)
            if sanitized_address is None:
                logger.debug(
                    "Empty/whitespace address provided; treating as 'any device'"
                )
            else:
                filtered_devices = []
                for device in addressed_devices:
                    sanitized_name = sanitize_address(device.name)
                    sanitized_device_address = sanitize_address(
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
            raise self.BLEError(ERROR_MULTIPLE_DEVICES.format(address, device_list))
        # No specific address provided and multiple devices found, return the first one
        return addressed_devices[0]


    # State management convenience properties (Phase 1 addition)
    @property
    def connection_state(self) -> ConnectionState:
        """Get current connection state from state manager."""
        return self._state_manager.state

    @property
    def is_connection_connected(self) -> bool:
        """Check if currently connected via state manager."""
        return self._state_manager.is_connected

    @property
    def is_connection_closing(self) -> bool:
        """Check if interface is closing via state manager."""
        return self._state_manager.is_closing

    @property
    def can_initiate_connection(self) -> bool:
        """Check if a new connection can be initiated via state manager."""
        return self._state_manager.can_connect

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

        requested_identifier = address if address is not None else self.address
        normalized_request = sanitize_address(requested_identifier)

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

        device_address = (
            client.bleak_client.address if hasattr(client, "bleak_client") else None
        )
        previous_client = None
        with self._state_lock:
            previous_client = self.client
            self.address = device_address
            self.client = client
            self._disconnect_notified = False
            normalized_device_address = sanitize_address(
                device_address or ""
            )
            if normalized_request is not None:
                self._last_connection_request = normalized_request
            else:
                self._last_connection_request = normalized_device_address

        if previous_client and previous_client is not client:
            self._client_manager.update_client_reference(client, previous_client)

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
        Run loop that reads inbound packets from the BLE FROMRADIO characteristic and delivers them to the interface packet handler.

        Waits on an internal read trigger, performs GATT reads when a BLE client is available, and forwards non-empty payloads
            to _handleFromRadio. Coordinates with the reconnection logic when the client becomes unavailable and tolerates
            transient empty reads. On a fatal error, initiates a safe shutdown of the interface.
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
                        logger.debug("Detected reconnection, resuming normal operation")
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
        except Exception:
            logger.exception("Fatal error in BLE receive thread, closing interface.")
            # Use state manager instead of boolean flag
            if not self._state_manager.is_closing:
                # Use a thread to avoid deadlocks if close() waits for this thread
                error_close_thread = self.thread_coordinator.create_thread(
                    target=self.close, name="BLECloseOnError", daemon=True
                )
                self.thread_coordinator.start_thread(error_close_thread)

    def _read_from_radio_with_retries(self, client: "BLEClient") -> Optional[bytes]:
        """
        Read the FROMRADIO characteristic, tolerating transient empty payloads.
        """
        for attempt in range(BLEConfig.EMPTY_READ_MAX_RETRIES + 1):
            payload = client.read_gatt_char(
                FROMRADIO_UUID, timeout=BLEConfig.GATT_IO_TIMEOUT
            )
            if payload:
                self._suppressed_empty_read_warnings = 0
                return payload
            if attempt < BLEConfig.EMPTY_READ_MAX_RETRIES:
                _sleep(RetryPolicy.EMPTY_READ.get_delay(attempt))
        self._log_empty_read_warning()
        return None

    def _handle_transient_read_error(self, error: BleakError) -> None:
        """
        Handle recoverable BleakErrors with the configured retry policy.
        """
        if RetryPolicy.TRANSIENT_ERROR.should_retry(self._read_retry_count):
            self._read_retry_count += 1
            logger.debug(
                "Transient BLE read error, retrying (%d/%d)",
                self._read_retry_count,
                BLEConfig.TRANSIENT_READ_MAX_RETRIES,
            )
            _sleep(RetryPolicy.TRANSIENT_ERROR.get_delay(self._read_retry_count))
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
        except (BleakError, RuntimeError, OSError) as e:
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
            if self.is_connection_closing:
                logger.debug(
                    "BLEInterface.close called while another shutdown is in progress; continuing with cleanup"
                )
            # Transition to DISCONNECTING state on close (replaces _closing flag)
            self._state_manager.transition_to(ConnectionState.DISCONNECTING)
        if self._shutdown_event:
            self._shutdown_event.set()
        self._notification_manager.cleanup_all()

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

        # Close parent interface (stops publishing thread, etc.)
        self.error_handler.safe_execute(
            lambda: MeshInterface.close(self), error_msg="Error closing mesh interface"
        )

        # Clean up thread coordinator
        self.thread_coordinator.cleanup()
        # Use unified state lock
        with self._state_lock:
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
        try:
            publishingThread.queueWork(flush_event.set)
        except Exception as exc:
            logger.debug(
                "Runtime error during disconnect notification flush (possible threading issue): %s",
                exc,
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
            self._client_manager.safe_close_client(client)

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
            try:
                runnable()
            except Exception as exc:
                logger.debug("Error in deferred publish callback: %s", exc)
