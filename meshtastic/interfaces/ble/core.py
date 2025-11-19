"""Core BLE interface logic"""

import asyncio
import atexit
import contextlib
import io
import logging
import queue
import struct
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Event, Thread
from typing import Any, Callable, Optional, List

from bleak import BleakClient as BleakRootClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakError
from google.protobuf.message import DecodeError

from meshtastic import publishingThread
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2

from .client import BLEClient
from .config import BLEConfig
from .connection import (
    ClientManager,
    ConnectionOrchestrator,
    ConnectionValidator,
)
from .discovery import DiscoveryManager
from .gatt import (
    FROMNUM_UUID,
    FROMRADIO_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    MALFORMED_NOTIFICATION_THRESHOLD,
    NotificationManager,
    SERVICE_UUID,
    TORADIO_UUID,
)
from .exceptions import BLEError
from .reconnect import ReconnectScheduler, RetryPolicy
from .state import BLEStateManager, ConnectionState
from .util import (
    BLEErrorHandler,
    ThreadCoordinator,
    _bleak_supports_connected_fallback,
    _sanitize_address,
    _sleep,
    _with_timeout,
    enumerate_connected_devices,
)

logger = logging.getLogger(__name__)

DISCONNECT_TIMEOUT_SECONDS = 5.0
RECEIVE_THREAD_JOIN_TIMEOUT = (
    2.0  # Bounded join keeps shutdown responsive for CLI users and tests
)

# Error message constants
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


class BLEInterface(MeshInterface):
    """
    MeshInterface using BLE to connect to Meshtastic devices.
    """

    # Make BLEError available as class attribute for backward compatibility
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
        Initialize a BLEInterface, start background receive machinery, and attempt initial device connection and configuration.
        
        Parameters:
            address (Optional[str]): BLE device address or name to connect to initially; may be None to connect to any available device.
            noProto (bool): If True, skip protocol-level initialization and configuration.
            debugOut (Optional[io.TextIOWrapper]): Optional text stream for debug output.
            noNodes (bool): If True, disable node discovery/management.
            timeout (int): Timeout in seconds used for initial connection and configuration steps.
            auto_reconnect (bool): If True, enable automatic reconnection after unexpected disconnects.
        
        Raises:
            BLEError: If initial connection or configuration fails.
        """
        self._state_manager = BLEStateManager()
        self._state_lock = self._state_manager.lock
        self._closed: bool = False
        self._exit_handler = None
        self.address = address
        self._last_connection_request: Optional[str] = _sanitize_address(address)
        self.auto_reconnect = auto_reconnect
        self._disconnect_notified = False
        self._known_device_address: Optional[str] = None

        self.error_handler = BLEErrorHandler()
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

        self._read_trigger = self.thread_coordinator.create_event("read_trigger")
        self._reconnected_event = self.thread_coordinator.create_event(
            "reconnected_event"
        )
        self._shutdown_event = self.thread_coordinator.create_event("shutdown_event")
        self._malformed_notification_count = 0

        MeshInterface.__init__(
            self, debugOut=debugOut, noProto=noProto, noNodes=noNodes, timeout=timeout
        )

        self._read_retry_count = 0
        self._last_empty_read_warning = 0.0
        self._suppressed_empty_read_warnings = 0

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
                self._waitConnected(timeout=BLEConfig.CONNECTION_TIMEOUT)
                self.waitForConfig()

            self._exit_handler = atexit.register(self.close)
        except Exception as e:
            self.close()
            if isinstance(e, BLEError):
                raise
            raise BLEError(ERROR_CONNECTION_FAILED.format(e)) from e

    def __repr__(self):
        """
        Return a concise representation of this BLEInterface including its address and active flags.
        
        @returns A string containing the interface's address and any relevant options: `debugOut` when set, `noProto` and `noNodes` when true, and `auto_reconnect=False` when automatic reconnect is disabled.
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

    @staticmethod
    def scan() -> List[BLEDevice]:
        """
        Perform a BLE scan and return discovered BLE devices.
        
        Returns:
            devices (List[BLEDevice]): List of discovered BLEDevice objects; empty if no devices were found.
        """
        discovery_manager = DiscoveryManager()
        return discovery_manager.discover_devices(address=None)

    def _handle_disconnect(
        self,
        source: str,
        client: Optional["BLEClient"] = None,
        bleak_client: Optional[BleakRootClient] = None,
    ) -> bool:
        """
        Handle a BLE client disconnect event and trigger reconnection or shutdown flows.
        
        When the interface is closing this call is ignored. If auto-reconnect is enabled, the method marks the client as disconnected, clears the current client reference, schedules an automatic reconnect, and arranges for the previous client to be closed; in that case it returns `True` to indicate the disconnect was handled. If auto-reconnect is disabled, the method transitions the interface toward closing and initiates a full close; in that case it returns `False`.
        
        Parameters:
            source (str): Identifier describing the origin of the disconnect (e.g., callback source).
            client (Optional[BLEClient]): The high-level BLE client instance that disconnected, if known.
            bleak_client (Optional[BleakRootClient]): The underlying bleak client instance that triggered the disconnect, if known.
        
        Returns:
            bool: `True` if the disconnect was handled without initiating a full close (typically when auto-reconnect is enabled), `False` if the disconnect resulted in initiating the interface close (auto-reconnect disabled).
        """
        if self.is_connection_closing:
            logger.debug("Ignoring disconnect from %s during shutdown.", source)
            return False

        target_client = client
        if not target_client and bleak_client:
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
            with self._state_lock:
                if self._disconnect_notified:
                    logger.debug("Ignoring duplicate disconnect from %s.", source)
                    return True

                current_client = self.client
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

            if previous_client:
                self._state_manager.transition_to(ConnectionState.DISCONNECTED)
                self._disconnected()
                close_thread = self.thread_coordinator.create_thread(
                    target=self._client_manager.safe_close_client,
                    args=(previous_client,),
                    name="BLEClientClose",
                    daemon=True,
                )
                self.thread_coordinator.start_thread(close_thread)

            self.thread_coordinator.clear_events("read_trigger", "reconnected_event")
            self._schedule_auto_reconnect()
            return True
        else:
            self._state_manager.transition_to(ConnectionState.DISCONNECTING)
            logger.debug("Auto-reconnect disabled, closing interface.")
            self.close()
            return False

    def _on_ble_disconnect(self, client: BleakRootClient) -> None:
        """
        Handle a disconnect event originating from the Bleak client and initiate the interface's disconnect logic.
        
        Parameters:
            client (BleakRootClient): The Bleak client instance that triggered the disconnect callback.
        """
        self._handle_disconnect("bleak_callback", bleak_client=client)

    def _schedule_auto_reconnect(self) -> None:
        """
        Schedule an automatic reconnect attempt if auto-reconnect is enabled and the interface is not closing.
        
        If auto-reconnect is enabled, clears the internal shutdown event and delegates reconnect scheduling to the reconnect scheduler using the configured auto_reconnect policy and the shutdown event.
        """
        if not self.auto_reconnect:
            return
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
        Increment the malformed FROMNUM notification counter, log the incident, and warn when a threshold is reached.
        
        Parameters:
        	reason (str): Short message describing why the notification is considered malformed; included in the debug log.
        	exc_info (bool): If True, include exception info in the debug log.
        """
        self._malformed_notification_count += 1
        logger.debug("%s", reason, exc_info=exc_info)
        if self._malformed_notification_count >= MALFORMED_NOTIFICATION_THRESHOLD:
            logger.warning(
                "Received %d malformed FROMNUM notifications. Check BLE connection stability.",
                self._malformed_notification_count,
            )
            self._malformed_notification_count = 0

    def from_num_handler(self, _, b: bytearray) -> None:
        """
        Handle a FROMNUM BLE notification payload.
        
        Validates that the payload is exactly 4 bytes, decodes it as a little-endian unsigned 32-bit integer, logs the decoded value, and resets the malformed-notification counter. If the payload has the wrong length or cannot be decoded, reports the malformed occurrence via _handle_malformed_fromnum. Always signals the "read_trigger" event on the thread coordinator.
        
        Parameters:
            b (bytearray): Notification payload expected to be 4 bytes representing a little-endian unsigned 32-bit integer.
        """
        try:
            if len(b) != 4:
                self._handle_malformed_fromnum(
                    f"FROMNUM notify has unexpected length {len(b)}; ignoring"
                )
                return
            from_num = struct.unpack("<I", b)[0]
            logger.debug("FROMNUM notify: %d", from_num)
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
        Register and start notification callbacks for legacy log, log, and FROMNUM characteristics on the provided BLE client.
        
        The method ensures each characteristic has a single reusable callback registered with the internal NotificationManager, wraps handlers with the interface error handler to prevent handler exceptions from propagating, starts optional log notifications only if the client exposes those characteristics, and always starts the FROMNUM notification with the configured startup timeout.
        
        Parameters:
            client (BLEClient): The BLE client on which to register and start notifications.
        """
        def _safe_call(handler, sender, data, error_msg):
            self.error_handler.safe_execute(
                lambda: handler(sender, data),
                error_msg=error_msg,
            )

        def _safe_legacy_handler(sender, data):
            """
            Wraps and forwards legacy log notifications to the instance's legacy_log_radio_handler and ensures handler errors are captured and reported.
            
            Parameters:
                sender: The notification source (BLE characteristic or client) that triggered the callback.
                data (bytes | bytearray): The payload of the legacy log notification.
            """
            _safe_call(
                self.legacy_log_radio_handler,
                sender,
                data,
                "Error in legacy log notification handler",
            )

        def _safe_log_handler(sender, data):
            """
            Safe wrapper that forwards BLE log notification payloads to the instance log handler and reports handler errors.
            
            Parameters:
                sender: The origin of the notification (BLE client/characteristic identifier).
                data (bytes | bytearray): The notification payload delivered by the BLE characteristic.
            """
            _safe_call(
                self.log_radio_handler,
                sender,
                data,
                "Error in log notification handler",
            )

        def _safe_from_num_handler(sender, data):
            """
            Wrapper that invokes the FROMNUM notification handler through a safety wrapper to catch and log handler errors.
            
            Parameters:
            	sender: The notification source (typically a Bleak client or characteristic UUID).
            	data (bytes or bytearray): Raw payload from the FROMNUM BLE characteristic.
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
            Retrieve an existing notification callback for a UUID or create, register, and return a new one.
            
            Parameters:
            	uuid (str): The characteristic UUID used to look up or subscribe the notification callback.
            	factory (Callable[[], Callable[[Any, Any], None]]): A zero-argument factory that returns a notification callback to register if none exists.
            
            Returns:
            	handler (Callable[[Any, Any], None]): The existing or newly created notification callback registered for `uuid`.
            """
            handler = self._notification_manager.get_callback(uuid)
            if handler is None:
                handler = factory()
                self._notification_manager.subscribe(uuid, handler)
            return handler

        try:
            if client.has_characteristic(LEGACY_LOGRADIO_UUID):
                legacy_handler = _get_or_create_handler(
                    LEGACY_LOGRADIO_UUID, lambda: _safe_legacy_handler
                )
                client.start_notify(
                    LEGACY_LOGRADIO_UUID,
                    legacy_handler,
                    timeout=BLEConfig.NOTIFICATION_START_TIMEOUT,
                )
            if client.has_characteristic(LOGRADIO_UUID):
                log_handler = _get_or_create_handler(
                    LOGRADIO_UUID, lambda: _safe_log_handler
                )
                client.start_notify(
                    LOGRADIO_UUID,
                    log_handler,
                    timeout=BLEConfig.NOTIFICATION_START_TIMEOUT,
                )
        except (BleakError, BleakDBusError, RuntimeError) as e:
            logger.debug("Failed to start optional log notifications: %s", e)

        from_num_handler = _get_or_create_handler(
            FROMNUM_UUID, lambda: _safe_from_num_handler
        )
        client.start_notify(
            FROMNUM_UUID,
            from_num_handler,
            timeout=BLEConfig.NOTIFICATION_START_TIMEOUT,
        )

    def log_radio_handler(self, _, b: bytearray) -> None:
        """
        Handle a protobuf LogRecord payload received from the radio and forward its text to the internal log handler.
        
        Parses the provided bytearray as a mesh LogRecord; if the record contains a source, prefixes the message with "[source] " before passing it to the log handler. If the payload cannot be decoded as a LogRecord, a warning is emitted and the payload is ignored.
        
        Parameters:
            b (bytearray): Raw payload bytes containing a serialized mesh LogRecord.
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
        Handle a legacy UTF-8 log payload from the radio and forward it to the internal log handler.
        
        Decodes the provided payload as UTF-8, strips trailing newline characters, and passes the resulting string to the interface's log handler. If the payload cannot be decoded as UTF-8, a warning is emitted and the payload is ignored.
        
        Parameters:
        	_ (Any): Unused callback source parameter (provided by BLE notification API).
        	b (bytearray): Payload containing a legacy UTF-8 encoded log line.
        """
        try:
            log_radio = b.decode("utf-8").replace("\n", "")
            self._handleLogLine(log_radio)
        except UnicodeDecodeError:
            logger.warning(
                "Malformed legacy LogRecord received (not valid utf-8). Skipping."
            )

    def find_device(self, address: Optional[str]) -> BLEDevice:
        # Handle case where interface is not properly initialized (e.g., in tests)
        """
        Locate a BLE device matching the provided address or device name.
        
        Searches discovered BLE devices for one whose sanitized name or address matches the sanitized input. If no address is provided or the provided address is empty/whitespace, behaves as an unconstrained lookup among discovered devices. If discovery yields no matches, the method will attempt a legacy connected-device lookup for backward compatibility before raising an error.
        
        Parameters:
            address (Optional[str]): Device address or name to match; can be None to select any discovered device.
        
        Returns:
            BLEDevice: A single matching BLE device.
        
        Raises:
            BLEError: If no matching devices are found, or if an address was specified and multiple matching devices are found.
        """
        addressed_devices: List[BLEDevice] = []
        if not hasattr(self, "_discovery_manager"):
            from .discovery import DiscoveryManager

            addressed_devices = DiscoveryManager().discover_devices(address)
        else:
            addressed_devices = self._discovery_manager.discover_devices(address)

        if not hasattr(self, "address"):
            self.address = None
        if not hasattr(self, "debugOut"):
            self.debugOut = None

        if address:
            sanitized_address = _sanitize_address(address)
            if sanitized_address is None:
                logger.debug(
                    "Empty/whitespace address provided; treating as 'any device'"
                )
            else:
                filtered_devices = []
                for device in addressed_devices:
                    sanitized_name = _sanitize_address(device.name)
                    sanitized_device_address = _sanitize_address(device.address)
                    if sanitized_address in (sanitized_name, sanitized_device_address):
                        filtered_devices.append(device)
                addressed_devices = filtered_devices

        if len(addressed_devices) == 0:
            finder = getattr(self, "_find_connected_devices", None)
            if callable(finder):
                try:
                    connected_devices = finder(address)
                    if connected_devices:
                        addressed_devices = connected_devices
                except BLEError as exc:
                    logger.debug(
                        "Connected device lookup failed, continuing to error: %s", exc
                    )

            if not addressed_devices:
                if address:
                    raise BLEError(ERROR_NO_PERIPHERAL_FOUND.format(address))
                raise BLEError(ERROR_NO_PERIPHERALS_FOUND)
        if len(addressed_devices) == 1:
            return addressed_devices[0]
        if address and len(addressed_devices) > 1:
            device_list = "\n".join(
                [f"- {d.name} ({d.address})" for d in addressed_devices]
            )
            raise BLEError(ERROR_MULTIPLE_DEVICES.format(address, device_list))
        return addressed_devices[0]

    def _find_connected_devices(self, address: Optional[str]) -> List[BLEDevice]:
        """
        Fallback method for finding already-connected Meshtastic devices when active scanning fails.

        Uses Bleak's private backend API to enumerate devices currently connected to the adapter.
        """
        min_version = BLEConfig.BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION
        if not _bleak_supports_connected_fallback(min_version):
            logger.debug(
                "Skipping fallback connected-device scan; bleak is below %s",
                ".".join(str(part) for part in min_version),
            )
            return []

        try:
            return enumerate_connected_devices(SERVICE_UUID, address)
        except BLEError as exc:
            logger.debug("Fallback device discovery failed: %s", exc)
            return []

    @property
    def connection_state(self) -> ConnectionState:
        """
        Expose the current BLE connection state.
        
        Returns:
            ConnectionState: The interface's current connection state.
        """
        return self._state_manager.state

    @property
    def is_connection_connected(self) -> bool:
        """
        Whether the interface currently has an active BLE connection.
        
        @returns:
            `True` if connected to a BLE device, `False` otherwise.
        """
        return self._state_manager.is_connected

    @property
    def is_connection_closing(self) -> bool:
        """
        Indicates whether the current BLE connection is in the process of closing.
        
        Returns:
            True if the connection is in the process of closing, False otherwise.
        """
        return self._state_manager.is_closing

    @property
    def can_initiate_connection(self) -> bool:
        """
        Indicates whether initiating a new BLE connection is currently permitted.
        
        Returns:
            `true` if a new connection can be initiated, `false` otherwise.
        """
        return self._state_manager.can_connect

    def connect(self, address: Optional[str] = None) -> "BLEClient":
        """
        Establishes or returns a BLE client connected to the specified device.
        
        If an existing connected client already satisfies the requested identifier, that client is returned; otherwise a new connection is initiated, internal connection state is updated, and the connected BLEClient is returned.
        
        Parameters:
            address (Optional[str]): Device address or name to connect to. If omitted, the interface's current address or last requested address is used.
        
        Returns:
            BLEClient: The BLE client instance representing the active connection.
        """
        requested_identifier = address if address is not None else self.address
        normalized_request = _sanitize_address(requested_identifier)

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
            self._known_device_address = device_address
            self.client = client
            self._disconnect_notified = False
            normalized_device_address = _sanitize_address(device_address or "")
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
        Handle a device disconnect that occurred during the read loop and determine whether the receive loop should continue.
        
        Parameters:
            error_message (str): Human-readable context about the disconnect.
            previous_client (BLEClient): The client instance that was active when the disconnect occurred.
        
        Returns:
            bool: `True` if the disconnect was handled and the interface should continue (for example by scheduling a reconnect), `False` if the receive loop should stop.
        """
        logger.debug("Device disconnected: %s", error_message)
        should_continue = self._handle_disconnect(
            f"read_loop: {error_message}", client=previous_client
        )
        if not should_continue:
            self._want_receive = False
        return should_continue

    def _receiveFromRadioImpl(self) -> None:
        """
        Run the main BLE receive loop: wait for read triggers, read FROMRADIO payloads, and forward them to the radio handler.
        
        This method blocks inside a background thread while the interface desires to receive. It waits for a read trigger, reads radio payloads (with the configured retry policy), calls _handleFromRadio for each payload, and handles transient and fatal BLE errors including disconnect handling and automatic reconnect behavior. On an unhandled fatal exception it schedules a safe close of the interface.
        """
        try:
            while self._want_receive:
                if not self.thread_coordinator.wait_for_event(
                    "read_trigger", timeout=BLEConfig.RECEIVE_WAIT_TIMEOUT
                ):
                    if self.thread_coordinator.check_and_clear_event(
                        "reconnected_event"
                    ):
                        logger.debug("Detected reconnection, resuming normal operation")
                    continue
                self.thread_coordinator.clear_event("read_trigger")

                while self._want_receive:
                    with self._state_lock:
                        client = self.client
                    if client is None:
                        if self.auto_reconnect:
                            logger.debug(
                                "BLE client is None; waiting for auto-reconnect"
                            )
                            self.thread_coordinator.wait_for_event(
                                "reconnected_event",
                                timeout=BLEConfig.RECEIVE_WAIT_TIMEOUT,
                            )
                            break
                        logger.debug("BLE client is None, shutting down")
                        self._want_receive = False
                        break
                    try:
                        payload = self._read_from_radio_with_retries(client)
                        if not payload:
                            break
                        logger.debug("FROMRADIO read: %s", payload.hex())
                        self._handleFromRadio(payload)
                        self._read_retry_count = 0
                    except BleakDBusError as e:
                        if self._handle_read_loop_disconnect(str(e), client):
                            break
                        return
                    except BleakError as e:
                        if client and not client.is_connected():
                            if self._handle_read_loop_disconnect(str(e), client):
                                break
                            return
                        self._handle_transient_read_error(e)
                        continue
        except Exception:
            logger.exception("Fatal error in BLE receive thread, closing interface.")
            if not self._state_manager.is_closing:
                error_close_thread = self.thread_coordinator.create_thread(
                    target=self.close, name="BLECloseOnError", daemon=True
                )
                self.thread_coordinator.start_thread(error_close_thread)

    def _read_from_radio_with_retries(self, client: "BLEClient") -> Optional[bytes]:
        """
        Attempt to read a FROMRADIO payload, retrying on empty reads up to the configured limit.
        
        If a non-empty payload is read, the internal suppressed-empty-read counter is reset. If all attempts return no data, a warning about empty reads is logged and `None` is returned.
        
        Returns:
            bytes: The payload read from FROMRADIO.
            None: If no payload was obtained after all retries.
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
        Apply the transient-read retry policy for a BLE read error, delaying and retrying when allowed.
        
        Parameters:
            error (BleakError): The underlying BLE read error to evaluate and optionally retry.
        
        Notes:
            - Increments the instance's internal retry counter when a retry is performed.
            - Resets the retry counter when retries are exhausted.
        
        Raises:
            BLEError: If the retry policy is exhausted, raises a BLEError with an error-reading message.
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
        raise BLEError(ERROR_READING_BLE) from error

    def _log_empty_read_warning(self) -> None:
        """
        Log a warning about repeated empty BLE reads with suppression and cooldown.
        
        If the last warning was emitted at least BLEConfig.EMPTY_READ_WARNING_COOLDOWN seconds ago,
        emit a warning describing the empty read from the FROMRADIO characteristic and include the
        number of suppressed repeats (if any), then reset the suppression counter and timestamp.
        If the cooldown window has not elapsed, increment the suppressed-warning counter and emit
        a debug message noting the suppression.
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
        Write a serialized radio message to the device's TORADIO characteristic and trigger a subsequent read.
        
        Parameters:
            toRadio: A protobuf-like message with a `SerializeToString()` method whose serialized bytes will be written to the TORADIO characteristic.
        
        Raises:
            BLEError: If the BLE write operation fails.
        """
        b: bytes = toRadio.SerializeToString()
        if not b:
            return

        write_successful = False
        with self._state_lock:
            client = self.client

        if not client or self.is_connection_closing:
            logger.debug(
                "Skipping TORADIO write: no BLE client or interface is closing."
            )
            return

        logger.debug("TORADIO write: %s", b.hex())
        try:
            client.write_gatt_char(
                TORADIO_UUID, b, response=True, timeout=BLEConfig.GATT_IO_TIMEOUT
            )
            write_successful = True
        except (BleakError, RuntimeError, OSError) as e:
            logger.debug(
                "Error during write operation: %s",
                type(e).__name__,
                exc_info=True,
            )
            raise BLEError(ERROR_WRITING_BLE) from e

        if write_successful:
            _sleep(BLEConfig.SEND_PROPAGATION_DELAY)
            self.thread_coordinator.set_event("read_trigger")

    def close(self) -> None:
        """
        Perform an orderly shutdown of the BLE interface, releasing resources and disconnecting from the device.
        
        Transitions the connection state to disconnecting, signals shutdown to background threads, unregisters notifications, closes the mesh interface, stops and joins the receive thread, unregisters the process exit handler, disconnects and closes the active BLE client, flushes pending disconnect notifications, cleans up thread coordination, and finally marks the interface as disconnected and closed. Calling this method multiple times is safe and subsequent calls are no-ops.
        """
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
            self._state_manager.transition_to(ConnectionState.DISCONNECTING)
        if self._shutdown_event:
            self._shutdown_event.set()
        self._notification_manager.cleanup_all()

        self.error_handler.safe_execute(
            lambda: MeshInterface.close(self), error_msg="Error closing mesh interface"
        )

        if self._want_receive:
            self._want_receive = False
            self.thread_coordinator.wake_waiting_threads(
                "read_trigger", "reconnected_event"
            )
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

        with self._state_lock:
            client = self.client
            self.client = None
        if client:
            self._disconnect_and_close_client(client)

        notify = False
        with self._state_lock:
            if not self._disconnect_notified:
                self._disconnect_notified = True
                notify = True

        if notify:
            self._disconnected()
            self._wait_for_disconnect_notifications()

        self.thread_coordinator.cleanup()
        with self._state_lock:
            self._state_manager.transition_to(ConnectionState.DISCONNECTED)
            self._closed = True

    def _wait_for_disconnect_notifications(
        self, timeout: Optional[float] = None
    ) -> None:
        """
        Ensure pending disconnect notifications are delivered by flushing the publish queue and waiting up to `timeout` seconds.
        
        Parameters:
            timeout (float | None): Maximum time in seconds to wait for the publish-queue flush. If `None`, uses the module default `DISCONNECT_TIMEOUT_SECONDS`.
        
        Notes:
            - Schedules a flush task on the publishing thread and waits for a completion event.
            - If the wait times out and the publishing thread is not running, the publish queue is drained synchronously.
            - Errors scheduling the flush are handled by the instance's error handler.
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
        logger.debug("Disconnect notification flush completed")

    def _disconnect_and_close_client(self, client: "BLEClient"):
        """
        Disconnects and closes the specified BLE client.
        
        Parameters:
            client (BLEClient): The BLE client instance to disconnect and close.
        """
        self._client_manager.safe_close_client(client)

    def _drain_publish_queue(self, flush_event: Event) -> None:
        """
        Drain and execute any deferred publish callbacks from the publishing thread's queue until the provided flush event is set.
        
        If the publishing thread has no queue, the function returns immediately. While the flush event is not set, this will remove pending callables from the queue and execute each using the interface's error handler to prevent exceptions from propagating. Stops early when the queue is empty.
        
        Parameters:
            flush_event (asyncio.Event | threading.Event): Event that, when set, stops draining and allows the caller to proceed.
        """
        queue_obj = getattr(publishingThread, "queue", None)
        if queue_obj is None:
            return
        while not flush_event.is_set():
            try:
                runnable = queue_obj.get_nowait()
                logger.debug("Got runnable from queue: %r", runnable)
                self.error_handler.safe_execute(
                    runnable,
                    error_msg="Error in deferred publish callback",
                    reraise=False,
                )
                logger.debug("Deferred publish callback processed")
            except (asyncio.QueueEmpty, queue.Empty) as exc:
                logger.debug("Queue empty while draining publish queue: %s", exc)
                break
