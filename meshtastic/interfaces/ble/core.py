"""Core BLE interface logic"""

import asyncio
import atexit
import contextlib
import io
import logging
import struct
import time
from threading import Event, RLock, Thread, current_thread
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
    TORADIO_UUID,
)
from .exceptions import BLEError
from .reconnect import ReconnectScheduler, RetryPolicy
from .state import BLEStateManager, ConnectionState
from .util import (
    BLEErrorHandler,
    ThreadCoordinator,
    _sanitize_address,
    _sleep,
    _with_timeout,
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
        self._state_manager = BLEStateManager()
        self._state_lock = self._state_manager.lock
        self._closed: bool = False
        self._exit_handler = None
        self.address = address
        self._last_connection_request: Optional[str] = _sanitize_address(address)
        self.auto_reconnect = auto_reconnect
        self._disconnect_notified = False

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
        """Scan for BLE devices."""
        discovery_manager = DiscoveryManager()
        return discovery_manager.discover_devices(address=None)

    def _handle_disconnect(
        self,
        source: str,
        client: Optional["BLEClient"] = None,
        bleak_client: Optional[BleakRootClient] = None,
    ) -> bool:
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
        self._handle_disconnect("bleak_callback", bleak_client=client)

    def _schedule_auto_reconnect(self) -> None:
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
        self._malformed_notification_count += 1
        logger.debug("%s", reason, exc_info=exc_info)
        if self._malformed_notification_count >= MALFORMED_NOTIFICATION_THRESHOLD:
            logger.warning(
                "Received %d malformed FROMNUM notifications. Check BLE connection stability.",
                self._malformed_notification_count,
            )
            self._malformed_notification_count = 0

    def from_num_handler(self, _, b: bytearray) -> None:
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
        try:
            log_radio = b.decode("utf-8").replace("\n", "")
            self._handleLogLine(log_radio)
        except UnicodeDecodeError:
            logger.warning(
                "Malformed legacy LogRecord received (not valid utf-8). Skipping."
            )

    def find_device(self, address: Optional[str]) -> BLEDevice:
        # Handle case where interface is not properly initialized (e.g., in tests)
        if not hasattr(self, "_discovery_manager"):
            # Fallback for tests that create interface with object.__new__
            from .discovery import DiscoveryManager

            discovery_manager = DiscoveryManager()
            addressed_devices = discovery_manager.discover_devices(address)
        else:
            addressed_devices = self._discovery_manager.discover_devices(address)

        # Ensure address attribute exists for tests
        if not hasattr(self, "address"):
            self.address = None
        if not hasattr(self, "debugOut"):
            self.debugOut = None
        # Ensure address attribute exists for tests
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
        Find currently connected devices matching the given address.

        This is a legacy method for backward compatibility with tests.
        In the current architecture, this functionality is handled by the DiscoveryManager.
        """
        # For backward compatibility, return empty list
        # The actual connected device logic is handled by DiscoveryManager
        return []

    @property
    def connection_state(self) -> ConnectionState:
        return self._state_manager.state

    @property
    def is_connection_connected(self) -> bool:
        return self._state_manager.is_connected

    @property
    def is_connection_closing(self) -> bool:
        return self._state_manager.is_closing

    @property
    def can_initiate_connection(self) -> bool:
        return self._state_manager.can_connect

    def connect(self, address: Optional[str] = None) -> "BLEClient":
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
            self._last_connection_request,
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
        logger.debug("Device disconnected: %s", error_message)
        should_continue = self._handle_disconnect(
            f"read_loop: {error_message}", client=previous_client
        )
        if not should_continue:
            self._want_receive = False
        return should_continue

    def _receiveFromRadioImpl(self) -> None:
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
            self._closed = True

    def _wait_for_disconnect_notifications(
        self, timeout: Optional[float] = None
    ) -> None:
        if timeout is None:
            timeout = DISCONNECT_TIMEOUT_SECONDS
        flush_event = Event()
        self.error_handler.safe_execute(
            lambda: publishingThread.queueWork(flush_event.set),
            error_msg="Runtime error during disconnect notification flush (possible threading issue)",
            reraise=False,
        )
        logger.debug("Disconnect notification flush completed")
        if not flush_event.wait(timeout=timeout):
            thread = getattr(publishingThread, "thread", None)
            if thread is not None and thread.is_alive():
                logger.debug("Timed out waiting for publish queue flush")
            else:
                self._drain_publish_queue(flush_event)

    def _disconnect_and_close_client(self, client: "BLEClient"):
        try:
            self.error_handler.safe_cleanup(
                lambda: client.disconnect(await_timeout=DISCONNECT_TIMEOUT_SECONDS)
            )
        finally:
            self._client_manager.safe_close_client(client)

    def _drain_publish_queue(self, flush_event: Event) -> None:
        queue = getattr(publishingThread, "queue", None)
        if queue is None:
            return
        while not flush_event.is_set():
            try:
                runnable = queue.get_nowait()
                logger.debug(f"Got runnable from queue: {runnable}")
                self.error_handler.safe_execute(
                    runnable,
                    error_msg="Error in deferred publish callback",
                    reraise=False,
                )
                logger.debug("Error in deferred publish callback processed")
            except Exception as e:
                logger.debug(f"Exception in drain queue: {e}")
                # Handle both asyncio.QueueEmpty and queue.Empty
                break
            logger.debug("Error in deferred publish callback processed")
