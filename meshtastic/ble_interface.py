"""Bluetooth interface
"""
import asyncio
import atexit
import contextlib
import io
import logging
import struct
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from queue import Empty
from threading import Event, Lock, Thread, current_thread
from typing import List, Optional

from bleak import BleakClient as BleakRootClient
from bleak import BleakScanner, BLEDevice
from bleak.exc import BleakDBusError, BleakError
from google.protobuf.message import DecodeError

from meshtastic import publishingThread
from meshtastic.mesh_interface import MeshInterface

from .protobuf import mesh_pb2

SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"
LEGACY_LOGRADIO_UUID = "6c6fd238-78fa-436b-aacf-15c5be1ef2e2"
LOGRADIO_UUID = "5a3d6e49-06e6-4423-9944-e9de8cdf9547"
MALFORMED_NOTIFICATION_THRESHOLD = 10
logger = logging.getLogger(__name__)

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
RECEIVE_THREAD_JOIN_TIMEOUT = 2.0
EVENT_THREAD_JOIN_TIMEOUT = 2.0

# BLE timeout and retry constants
BLE_SCAN_TIMEOUT = 10.0
RECEIVE_WAIT_TIMEOUT = 0.5
EMPTY_READ_RETRY_DELAY = 0.1
EMPTY_READ_MAX_RETRIES = 5
SEND_PROPAGATION_DELAY = 0.01
CONNECTION_TIMEOUT = 60.0
AUTO_RECONNECT_INITIAL_DELAY = 1.0
AUTO_RECONNECT_MAX_DELAY = 30.0
AUTO_RECONNECT_BACKOFF = 2.0

# Error message constants
ERROR_READING_BLE = "Error reading BLE"
ERROR_NO_PERIPHERAL_FOUND = "No Meshtastic BLE peripheral with identifier or address '{0}' found. Try --ble-scan to find it."
ERROR_MULTIPLE_PERIPHERALS_FOUND = (
    "More than one Meshtastic BLE peripheral with identifier or address '{0}' found."
)
ERROR_WRITING_BLE = (
    "Error writing BLE. This is often caused by missing Bluetooth "
    "permissions (e.g. not being in the 'bluetooth' group) or pairing issues."
)
ERROR_CONNECTION_FAILED = "Connection failed: {0}"
ERROR_NO_PERIPHERALS_FOUND = (
    "No Meshtastic BLE peripherals found. Try --ble-scan to find them."
)
ERROR_ASYNC_TIMEOUT = "Async operation timed out"


class BLEInterface(MeshInterface):
    """MeshInterface using BLE to connect to devices."""

    class BLEError(Exception):
        """An exception class for BLE errors."""

    def __init__(
        self,
        address: Optional[str],
        noProto: bool = False,
        debugOut: Optional[io.TextIOWrapper] = None,
        noNodes: bool = False,
        *,
        auto_reconnect: bool = True,
    ) -> None:
        """
        Initialize a BLEInterface, start its background receive thread, and attempt an initial connection to a Meshtastic BLE device.

        Parameters:
            address (Optional[str]): BLE address to connect to; if None, any available Meshtastic device may be used.
            noProto (bool): If True, do not initialize protobuf-based protocol handling.
            debugOut (Optional[io.TextIOWrapper]): Stream to emit debug output to, if provided.
            noNodes (bool): If True, do not attempt to read the device's node list on startup.
            auto_reconnect (bool): If True, keep the interface alive across unexpected disconnects and attempt
                automatic reconnection; if False, close the interface on disconnect.
        """
        # Thread safety and state management
        self._closing_lock: Lock = Lock()  # Prevents concurrent close operations
        self._client_lock: Lock = Lock()   # Protects client access during reconnection
        self._closing: bool = False        # Indicates shutdown in progress
        self._exit_handler = None
        self.address = address
        self.auto_reconnect = auto_reconnect
        self._disconnect_notified = False  # Prevents duplicate disconnect events

        # Event coordination for reconnection and read operations
        self._read_trigger: Event = Event()      # Signals when data is available to read
        self._reconnected_event: Event = Event() # Signals when reconnection occurred
        self._malformed_notification_count = 0   # Tracks corrupted packets for threshold
        self._reconnect_thread: Optional[Thread] = None

        # Initialize parent interface
        MeshInterface.__init__(
            self, debugOut=debugOut, noProto=noProto, noNodes=noNodes
        )

        # Start background receive thread for inbound packet processing
        logger.debug("Threads starting")
        self._want_receive = True
        self._receiveThread: Optional[Thread] = Thread(
            target=self._receiveFromRadioImpl, name="BLEReceive", daemon=True
        )
        self._receiveThread.start()
        logger.debug("Threads running")

        self.client: Optional[BLEClient] = None
        try:
            logger.debug(f"BLE connecting to: {address if address else 'any'}")
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

    def _on_ble_disconnect(self, client: BleakRootClient) -> None:
        """
        Handle a Bleak client disconnection by notifying the interface and initiating either a reconnection workflow or a full shutdown.

        If `auto_reconnect` is True, notify the parent interface of the disconnect and schedule a safe close of the current
        client so reconnection can proceed; duplicate or stale disconnect callbacks are ignored.
        If `auto_reconnect` is False, schedule a full interface close.

        Parameters:
            client (BleakRootClient): The Bleak client instance that triggered the disconnect callback.
        """
        # Ignore disconnects during shutdown to avoid race conditions
        if self._closing:
            logger.debug(
                "Ignoring disconnect callback because a shutdown is already in progress."
            )
            return

        address = getattr(client, "address", repr(client))
        logger.debug(f"BLE client {address} disconnected.")

        if self.auto_reconnect:
            previous_client = None
            with self._client_lock:
                # Prevent duplicate disconnect notifications
                if self._disconnect_notified:
                    logger.debug("Ignoring duplicate disconnect callback.")
                    return

                current_client = self.client
                # Ignore stale client disconnects (from previous connections)
                if (
                    current_client
                    and getattr(current_client, "bleak_client", None) is not client
                ):
                    logger.debug(
                        "Ignoring disconnect from a stale BLE client instance."
                    )
                    return
                previous_client = current_client
                self.client = None
                self._disconnect_notified = True

            # Notify parent interface of disconnection and close previous client asynchronously
            if previous_client:
                self._disconnected()
                Thread(
                    target=self._safe_close_client,
                    args=(previous_client,),
                    name="BLEClientClose",
                    daemon=True,
                ).start()

            # Event coordination: wake receive loop and clear reconnection flag
            self._read_trigger.set()  # Wake receive loop to handle reconnection
            self._reconnected_event.clear()  # Clear reconnected event on disconnect
            self._schedule_auto_reconnect()
        else:
            # When auto_reconnect is disabled, close the entire interface
            Thread(target=self.close, name="BLEClose", daemon=True).start()

    def _schedule_auto_reconnect(self) -> None:
        """Start (if needed) a background task that retries BLE connection until success or shutdown."""

        if not self.auto_reconnect:
            return
        if self._closing:
            logger.debug("Skipping auto-reconnect scheduling because interface is closing.")
            return

        with self._client_lock:
            existing_thread = self._reconnect_thread
            if existing_thread and existing_thread.is_alive():
                logger.debug("Auto-reconnect already in progress; skipping new attempt.")
                return

            def _attempt_reconnect() -> None:
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
                            new_client = self.connect(self.address)
                            with self._client_lock:
                                if self.client is None and new_client is not None:
                                    self.client = new_client
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
                            logger.exception("Unexpected error during auto-reconnect attempt")

                        if self._closing or not self.auto_reconnect:
                            return
                        time.sleep(delay)
                        delay = min(delay * AUTO_RECONNECT_BACKOFF, AUTO_RECONNECT_MAX_DELAY)
                finally:
                    with self._client_lock:
                        if self._reconnect_thread is current_thread():
                            self._reconnect_thread = None

            thread = Thread(
                target=_attempt_reconnect,
                name="BLEAutoReconnect",
                daemon=True,
            )
            self._reconnect_thread = thread

        thread.start()

    def from_num_handler(self, _, b: bytearray) -> None:  # pylint: disable=C0116
        """
        Handle notifications from the FROMNUM characteristic and wake the read loop.

        Parses a 4-byte little-endian unsigned integer from the notification payload `b`. On a successful parse, resets the 
        malformed-notification counter and logs the parsed value. On parse failure, increments the malformed-notification 
        counter and logs; if the counter reaches MALFORMED_NOTIFICATION_THRESHOLD, emits a warning and resets the 
        counter. Always sets self._read_trigger to signal the read loop.

        Parameters:
            _ (Any): Unused sender/handle parameter supplied by the BLE library.
            b (bytearray): Notification payload expected to be exactly 4 bytes containing a little-endian unsigned 32-bit integer.
        """
        try:
            if len(b) != 4:
                logger.debug(f"FROMNUM notify has unexpected length {len(b)}; ignoring")
                return
            from_num = struct.unpack("<I", b)[0]
            logger.debug(f"FROMNUM notify: {from_num}")
            # Successful parse: reset malformed counter
            self._malformed_notification_count = 0
        except (struct.error, ValueError):
            self._malformed_notification_count += 1
            logger.debug("Malformed FROMNUM notify; ignoring", exc_info=True)
            if self._malformed_notification_count >= MALFORMED_NOTIFICATION_THRESHOLD:
                logger.warning(
                    f"Received {self._malformed_notification_count} malformed FROMNUM notifications. "
                    "Check BLE connection stability."
                )
                self._malformed_notification_count = 0  # Reset counter after warning
            return
        finally:
            self._read_trigger.set()

    def _register_notifications(self, client: "BLEClient") -> None:
        """
        Register BLE characteristic notification handlers on the given client.

        Registers optional log notification handlers for legacy and current log characteristics; failures to start these optional 
        handlers are caught and logged at debug level. Also registers the critical FROMNUM notification handler for 
        incoming packets — failures to register this notification are not suppressed and will propagate.

        Parameters:
            client (BLEClient): Connected BLE client to register notifications on.
        """
        # Optional log notifications - failures are not critical
        try:
            if client.has_characteristic(LEGACY_LOGRADIO_UUID):
                client.start_notify(LEGACY_LOGRADIO_UUID, self.legacy_log_radio_handler)
            if client.has_characteristic(LOGRADIO_UUID):
                client.start_notify(LOGRADIO_UUID, self.log_radio_handler)
        except BleakError:
            logger.debug("Failed to start optional log notifications", exc_info=True)

        # Critical notification for receiving packets - let failures bubble up
        client.start_notify(FROMNUM_UUID, self.from_num_handler)

    async def log_radio_handler(self, _, b: bytearray) -> None:  # pylint: disable=C0116
        """
        Handle a protobuf LogRecord notification and forward a formatted log line to the instance log handler.

        Parses the notification payload as a `mesh_pb2.LogRecord`. If the record contains a `source`, the forwarded line is prefixed 
        with `[source] `; otherwise the record `message` is forwarded as-is. Malformed records are logged as a warning 
        and ignored.

        Parameters:
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

    async def legacy_log_radio_handler(self, _, b: bytearray) -> None:
        """
        Handle a legacy log-radio notification by decoding and forwarding the UTF-8 log line.

        Decodes `b` as UTF-8, strips newline characters, and passes the resulting string to `self._handleLogLine`. 
        If `b` is not valid UTF-8, a warning is logged and the notification is ignored.

        Parameters:
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
        advertisements include SERVICE_UUID.

        Returns:
            List[BLEDevice]: Devices whose advertisements include SERVICE_UUID; empty list if none are found.
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
                    f"BleakScanner.discover returned unexpected type: {type(response)}"
                )
                return devices
            for _, value in response.items():
                if isinstance(value, tuple):
                    device, adv = value
                else:
                    logger.warning(
                        f"Unexpected return type from BleakScanner.discover: {type(value)}"
                    )
                    continue
                suuids = getattr(adv, "service_uuids", None)
                if suuids and SERVICE_UUID in suuids:
                    devices.append(device)
            return devices

    def find_device(self, address: Optional[str]) -> BLEDevice:
        """
        Finds a Meshtastic BLE device matching an optional address or device name.

        Parameters:
            address (Optional[str]): Address or device name to match; comparison ignores case and common separators.
            If None, any discovered Meshtastic device may be returned.

        Returns:
            BLEDevice: The matched BLE device (the first match when multiple devices were discovered and no address was specified).

        Raises:
            BLEInterface.BLEError: If no Meshtastic devices are found, or if an address was provided and multiple matching devices are found.
        """

        addressed_devices = BLEInterface.scan()

        if address:
            sanitized_address = BLEInterface._sanitize_address(address)
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
                raise BLEInterface.BLEError(ERROR_NO_PERIPHERAL_FOUND.format(address))
            else:
                raise BLEInterface.BLEError(ERROR_NO_PERIPHERALS_FOUND)
        if len(addressed_devices) > 1:
            if address:
                raise BLEInterface.BLEError(
                    ERROR_MULTIPLE_PERIPHERALS_FOUND.format(address)
                )
            logger.warning(
                "Multiple Meshtastic BLE peripherals detected; selecting the first match: %s",
                addressed_devices[0].address,
            )
        return addressed_devices[0]

    @staticmethod
    def _sanitize_address(address: Optional[str]) -> Optional[str]:
        """
        Normalize a BLE address by removing separators and lowercasing.

        Parameters:
            address (Optional[str]): BLE address or identifier; may be None.

        Returns:
            Optional[str]: The normalized address with all "-", "_", ":" removed, trimmed of surrounding whitespace,
            and lowercased, or None if `address` is None.
        """
        if address is None:
            return None
        return (
            address.strip().replace("-", "").replace("_", "").replace(":", "").lower()
        )

    def connect(self, address: Optional[str] = None) -> "BLEClient":
        """
        Establish a BLE connection to the Meshtastic device identified by address.

        If address is provided it will be used to select the peripheral; otherwise a scan is performed to find a suitable device.
            On success returns a connected BLEClient with notifications registered and internal reconnect state updated.
            On failure, the created client is closed before the exception is propagated.

        Parameters:
            address (Optional[str]): BLE address or device name to connect to; may be None to allow automatic discovery.

        Returns:
            BLEClient: A connected BLEClient instance for the selected device.
        """

        # Bleak docs recommend always doing a scan before connecting (even if we know addr)
        device = self.find_device(address or self.address)
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
                client.get_services()
            # Ensure notifications are always active for this client (reconnect-safe)
            self._register_notifications(client)
            # Publish the new client before waking any waiters
            with self._client_lock:
                previous_client = self.client
                self.client = client
                self._disconnect_notified = False
            if previous_client and previous_client is not client:
                Thread(
                    target=self._safe_close_client,
                    args=(previous_client,),
                    name="BLEClientClose",
                    daemon=True,
                ).start()
            # Signal successful reconnection to waiting threads
            self._reconnected_event.set()
        except Exception as e:
            logger.debug("Failed to connect, closing BLEClient thread.", exc_info=True)
            try:
                client.close()
            except Exception as close_exc:
                logger.warning(
                    f"Ignoring exception during client cleanup on connection failure: {close_exc!r}"
                )
            raise e

        return client

    def _handle_read_loop_disconnect(
        self, error_message: str, previous_client: "BLEClient"
    ) -> bool:
        """
        Decide whether the receive loop should continue after a BLE disconnection and perform related cleanup for reconnection.

        Parameters:
            error_message (str): Human-readable description of the disconnection cause.
            previous_client (BLEClient): The BLEClient instance that observed the disconnect and may be closed.

        Returns:
            bool: True if the read loop should continue to allow auto-reconnect, False otherwise.
        """
        logger.debug(f"Device disconnected: {error_message}")
        if self.auto_reconnect:
            notify_disconnect = False
            should_close = False
            with self._client_lock:
                current = self.client
                if current is previous_client:
                    self.client = None
                    should_close = True
                    if not self._disconnect_notified:
                        self._disconnect_notified = True
                        notify_disconnect = True
                else:
                    logger.debug(
                        "Read-loop disconnect from a stale BLE client; ignoring."
                    )
            if notify_disconnect:
                self._disconnected()
            if should_close:
                Thread(
                    target=self._safe_close_client,
                    args=(previous_client,),
                    name="BLEClientClose",
                    daemon=True,
                ).start()
            self._read_trigger.clear()
            self._reconnected_event.clear()
            self._schedule_auto_reconnect()
            return True
        # End our read loop immediately
        self._want_receive = False
        return False

    def _safe_close_client(self, c: "BLEClient") -> None:
        """
        Close the provided BLEClient and suppress common close-time errors.

        Attempts to close the given BLEClient and ignores BleakError, RuntimeError, and OSError raised during close
            to avoid propagating shutdown-related exceptions.

        Parameters:
            c (BLEClient): The BLEClient instance to close; may be None or already-closed.
        """
        try:
            c.close()
        except BleakError:
            logger.debug("BLE-specific error during client close", exc_info=True)
        except (RuntimeError, OSError):
            logger.debug(
                "OS/Runtime error during client close (possible resource or threading issue)",
                exc_info=True,
            )

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
                if not self._read_trigger.wait(timeout=RECEIVE_WAIT_TIMEOUT):
                    # Timeout occurred, check if we were reconnected during this time
                    if self._reconnected_event.is_set():
                        self._reconnected_event.clear()
                        logger.debug("Detected reconnection, resuming normal operation")
                    continue
                self._read_trigger.clear()

                # Retry loop for handling empty reads and transient BLE issues
                retries: int = 0
                while self._want_receive:
                    with self._client_lock:
                        client = self.client
                    if client is None:
                        if self.auto_reconnect:
                            logger.debug(
                                "BLE client is None; waiting for application-managed reconnect"
                            )
                            # Wait briefly for reconnect or shutdown signal, then re-check
                            self._reconnected_event.wait(timeout=RECEIVE_WAIT_TIMEOUT)
                            break  # Return to outer loop to re-check state
                        logger.debug("BLE client is None, shutting down")
                        self._want_receive = False
                        break
                    try:
                        # Read from the FROMRADIO characteristic for incoming packets
                        b = client.read_gatt_char(FROMRADIO_UUID)
                        if not b:
                            # Handle empty reads with limited retries to avoid busy-looping
                            if retries < EMPTY_READ_MAX_RETRIES:
                                time.sleep(EMPTY_READ_RETRY_DELAY)
                                retries += 1
                                continue
                            logger.warning("Exceeded max retries for empty BLE read from FROMRADIO_UUID")
                            break  # Too many empty reads, exit to recheck state
                        logger.debug(f"FROMRADIO read: {b.hex()}")
                        self._handleFromRadio(b)
                        retries = 0  # Reset retry counter on successful read
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
                        logger.debug("Error reading BLE", exc_info=True)
                        raise BLEInterface.BLEError(ERROR_READING_BLE) from e
        except Exception:
            logger.exception("Fatal error in BLE receive thread, closing interface.")
            if not self._closing:
                # Use a thread to avoid deadlocks if close() waits for this thread
                Thread(target=self.close, name="BLECloseOnError", daemon=True).start()

    def _sendToRadioImpl(self, toRadio) -> None:
        """
        Send a protobuf message to the radio over the TORADIO BLE characteristic.

        Serializes `toRadio` to bytes and writes them with write-with-response to the TORADIO characteristic when a BLE client
            is available. If the serialized payload is empty or no client is present (e.g., during shutdown), the call is a
            no-op. After a successful write, the method waits briefly to allow propagation and then signals the read trigger.

        Parameters:
            toRadio: A protobuf message with a SerializeToString() method representing the outbound radio packet.

        Raises:
            BLEInterface.BLEError: If the write operation fails.
        """
        b: bytes = toRadio.SerializeToString()
        if not b:
            return

        write_successful = False
        with self._client_lock:
            client = self.client
            if client:  # Silently ignore writes while shutting down to avoid errors
                logger.debug(f"TORADIO write: {b.hex()}")
                try:
                    # Use write-with-response to ensure delivery is acknowledged by the peripheral
                    client.write_gatt_char(TORADIO_UUID, b, response=True)
                    write_successful = True
                except (BleakError, RuntimeError, OSError) as e:
                    # Log detailed error information and wrap in our interface exception
                    logger.debug(
                        "Error during write operation: %s", type(e).__name__, exc_info=True
                    )
                    raise BLEInterface.BLEError(ERROR_WRITING_BLE) from e

        if write_successful:
            # Brief delay to allow write to propagate before triggering read
            time.sleep(SEND_PROPAGATION_DELAY)
            self._read_trigger.set()  # Wake receive loop to process any response

    def close(self) -> None:
        """
        Shut down the BLE interface, stop background activity, disconnect the BLE client, and perform cleanup.

        This method is idempotent and safe to call multiple times. It stops the receive loop and reconnection waits, unregisters the
            atexit handler, disconnects and closes any active BLE client, emits a disconnected notification if not already sent,
            and waits briefly for pending disconnect-related notifications and the receive thread to finish.
        """
        with self._closing_lock:
            if self._closing:
                logger.debug(
                    "BLEInterface.close called while another shutdown is in progress; ignoring"
                )
                return
            self._closing = True

        try:
            # Close parent interface (stops publishing thread, etc.)
            MeshInterface.close(self)
        except Exception:
            # Log but don't let parent interface errors prevent BLE cleanup
            logger.exception("Error closing mesh interface")

        if self._want_receive:
            self._want_receive = False  # Tell the thread we want it to stop
            self._read_trigger.set()  # Wake up the receive thread if it's waiting
            self._reconnected_event.set()  # Ensure any reconnection waits are released
            if self._receiveThread:
                self._receiveThread.join(timeout=RECEIVE_THREAD_JOIN_TIMEOUT)
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

    def _wait_for_disconnect_notifications(
        self, timeout: float = DISCONNECT_TIMEOUT_SECONDS
    ) -> None:
        """
        Wait briefly for queued publish notifications to flush before continuing.

        If the publish queue does not flush within `timeout` seconds, the method
        will synchronously drain the publish queue when the publishing thread is not
        alive; otherwise it logs the timeout and returns. Any RuntimeError or ValueError
        raised while attempting the flush is caught and logged.
        Parameters:
            timeout (float): Maximum seconds to wait for the publish queue to flush.
        """
        flush_event = Event()
        try:
            publishingThread.queueWork(flush_event.set)
            if not flush_event.wait(timeout=timeout):
                thread = getattr(publishingThread, "thread", None)
                if thread is not None and thread.is_alive():
                    logger.debug("Timed out waiting for publish queue flush")
                else:
                    self._drain_publish_queue(flush_event)
        except RuntimeError:  # pragma: no cover - defensive logging
            logger.debug(
                "Runtime error during disconnect notification flush (possible threading issue)",
                exc_info=True,
            )
        except ValueError:  # pragma: no cover - defensive logging
            logger.debug(
                "Value error during disconnect notification flush (possible invalid event state)",
                exc_info=True,
            )

    def _disconnect_and_close_client(self, client: "BLEClient"):
        """
        Disconnects the given BLEClient and ensures its resources are released.

        Attempts to disconnect the client using DISCONNECT_TIMEOUT_SECONDS; if the disconnect times out or fails, forces closure.
            Always closes the client to release underlying resources and logs warnings on timeout and debug details for other
            disconnect/close errors.

        Parameters:
            client (BLEClient): The BLE client instance to disconnect and close.
        """
        try:
            client.disconnect(await_timeout=DISCONNECT_TIMEOUT_SECONDS)
        except BLEInterface.BLEError:
            logger.warning("Timed out waiting for BLE disconnect; forcing shutdown")
        except BleakError:
            logger.debug(
                "BLE-specific error during disconnect operation", exc_info=True
            )
        except (RuntimeError, OSError):  # pragma: no cover - defensive logging
            logger.debug(
                "OS/Runtime error during disconnect (possible resource or threading issue)",
                exc_info=True,
            )
        finally:
            self._safe_close_client(client)

    def _drain_publish_queue(self, flush_event: Event) -> None:
        """
        Drain and run any pending publish callbacks inline until the queue is empty or `flush_event` is set.

        This executes queued callables from the publishing thread's queue on the current thread, catching and logging exceptions from
            each callable so draining continues. If the publishing thread has no accessible queue, the method returns immediately.

        Parameters:
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
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.debug(
                    "Error in deferred publish callback: %s",
                    exc,
                    exc_info=True,
                )


class BLEClient:
    """
    Client wrapper for managing BLE device connections with thread-safe async operations.

    This class provides a synchronous interface to Bleak's async operations by running
    an internal event loop in a dedicated thread. It handles the complexity of
    asyncio-to-thread synchronization while providing a simple API for BLE operations.
    """

    def __init__(self, address=None, **kwargs) -> None:
        """
        Create a BLEClient with its own asyncio event loop and, optionally, a Bleak client for a specific address.

        Parameters:
            address (Optional[str]): BLE device address to attach a Bleak client to. If None, no Bleak client is created and the instance
            can only be used for discovery.
            **kwargs: Keyword arguments forwarded to the underlying Bleak client constructor when `address` is provided.
        """
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

        Parameters:
            kwargs: Backend-specific pairing options forwarded to the underlying BLE client.

        Returns:
            `True` if pairing succeeded, `False` otherwise.
        """
        return self.async_await(self.bleak_client.pair(**kwargs))

    def connect(
        self, *, await_timeout: Optional[float] = None, **kwargs
    ):  # pylint: disable=C0116
        """
        Initiates a connection using the underlying Bleak client and its internal event loop.

        Parameters:
            await_timeout (float | None): Maximum seconds to wait for the connect operation to complete; `None` to wait indefinitely.
            **kwargs: Forwarded to the underlying Bleak client's `connect` call.

        Returns:
            The value returned by the underlying Bleak client's `connect` call.
        """
        return self.async_await(self.bleak_client.connect(**kwargs), timeout=await_timeout)

    def is_connected(self) -> bool:
        """
        Check whether the underlying Bleak client reports a connected state.

        Returns:
            `true` if the underlying Bleak client reports it is connected, `false` otherwise. This will return `false` if no bleak client
            is present or if the client's connected state cannot be read.
        """
        bleak_client = getattr(self, "bleak_client", None)
        if bleak_client is None:
            return False
        try:
            connected = getattr(bleak_client, "is_connected", False)
            if callable(connected):
                connected = connected()
            return bool(connected)
        except (
            AttributeError,
            TypeError,
            RuntimeError,
        ):  # pragma: no cover - defensive logging
            logger.debug("Unable to read bleak connection state", exc_info=True)
            return False

    def disconnect(
        self, *, await_timeout: Optional[float] = None, **kwargs
    ):  # pylint: disable=C0116
        """
        Disconnects the underlying Bleak client, waiting for the operation to complete.

        Parameters:
            await_timeout (float | None): Maximum number of seconds to wait for the disconnect operation; if None, wait indefinitely.
            **kwargs: Additional keyword arguments forwarded to the Bleak client's disconnect method.
        """
        self.async_await(self.bleak_client.disconnect(**kwargs), timeout=await_timeout)

    def read_gatt_char(self, *args, **kwargs):  # pylint: disable=C0116
        """Read a GATT characteristic from the connected BLE device.

        Forwards all arguments to the underlying Bleak client's `read_gatt_char`.

        Parameters:
            *args: Positional arguments forwarded to `read_gatt_char` (typically the characteristic UUID or handle).
            **kwargs: Keyword arguments forwarded to `read_gatt_char`.

        Returns:
            bytes: The raw bytes read from the characteristic.
        """
        return self.async_await(self.bleak_client.read_gatt_char(*args, **kwargs))

    def write_gatt_char(self, *args, **kwargs):  # pylint: disable=C0116
        """
        Write a GATT characteristic on the connected BLE device and wait for the operation to complete.

        Raises:
            BLEInterface.BLEError: If the write fails or the operation times out.
        """
        self.async_await(self.bleak_client.write_gatt_char(*args, **kwargs))

    def get_services(self):
        """
        Return the connected device's discovered GATT services and their characteristics.

        Returns:
            A collection object describing the device's available GATT services and their characteristics.
        """
        return self.async_await(self.bleak_client.get_services())

    def has_characteristic(self, specifier):
        """
        Determine whether the connected BLE device exposes the characteristic identified by `specifier`.

        If the client's services are not yet populated, attempts to fetch services before checking.

        Parameters:
            specifier (str | UUID): UUID string or UUID object identifying the characteristic to check.

        Returns:
            bool: `true` if the characteristic is present, `false` otherwise.
        """
        services = getattr(self.bleak_client, "services", None)
        if not services or not getattr(services, "get_characteristic", None):
            try:
                self.get_services()
                services = getattr(self.bleak_client, "services", None)
            except (BLEInterface.BLEError, BleakError):  # pragma: no cover - defensive
                logger.debug(
                    "Unable to populate services before has_characteristic",
                    exc_info=True,
                )
        return bool(services and services.get_characteristic(specifier))

    def start_notify(self, *args, **kwargs):  # pylint: disable=C0116
        """
        Subscribe to notifications for a BLE characteristic on the connected device.

        Parameters:
            char_specifier: Identifier for the characteristic to subscribe to (UUID string, UUID object, or integer handle).
            callback: Callable invoked when a notification is received; called as (sender, data) where `data` is a bytearray.
            *args, **kwargs: Additional arguments forwarded to the BLE backend's notification start call.
        """
        self.async_await(self.bleak_client.start_notify(*args, **kwargs))

    def close(self):  # pylint: disable=C0116
        """
        Stop and tear down the BLE client's internal event loop and its thread.

        Attempts to stop the internal asyncio event loop, waits up to EVENT_THREAD_JOIN_TIMEOUT for the event thread to exit,
            and logs a warning if the thread does not terminate in time.
        """
        self.async_run(self._stop_event_loop())
        self._eventThread.join(timeout=EVENT_THREAD_JOIN_TIMEOUT)
        if self._eventThread.is_alive():
            logger.warning(
                "BLE event thread did not exit within %.1fs",
                EVENT_THREAD_JOIN_TIMEOUT,
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
        Waits for the given coroutine to complete on the client's event loop and returns its result.

        If the coroutine does not finish within `timeout` seconds the pending task is cancelled and a BLEInterface.BLEError is raised.

        Parameters:
            coro: The coroutine to run on the client's internal event loop.
            timeout (float | None): Maximum seconds to wait for completion; `None` means wait indefinitely.

        Returns:
            The value produced by the completed coroutine.

        Raises:
            BLEInterface.BLEError: If the wait times out.
        """
        future = self.async_run(coro)
        try:
            return future.result(timeout)
        except FutureTimeoutError as e:
            future.cancel()  # Clean up pending task to avoid resource leaks
            raise BLEInterface.BLEError(ERROR_ASYNC_TIMEOUT) from e

    def async_run(self, coro):  # pylint: disable=C0116
        """
        Schedule a coroutine on the client's internal asyncio event loop.

        Parameters:
            coro (coroutine): The coroutine to schedule.

        Returns:
            concurrent.futures.Future: Future representing the scheduled coroutine's eventual result.
        """
        return asyncio.run_coroutine_threadsafe(coro, self._eventLoop)

    def _run_event_loop(self):
        """Run the event loop in the dedicated thread until stopped."""
        try:
            self._eventLoop.run_forever()
        finally:
            self._eventLoop.close()  # Clean up resources when loop stops

    async def _stop_event_loop(self):
        """Signal the event loop to stop running."""
        self._eventLoop.stop()
