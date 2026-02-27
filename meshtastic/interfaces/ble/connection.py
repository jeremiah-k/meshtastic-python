"""BLE connection management and validation."""

import logging
import sys
from threading import Event, RLock
from typing import TYPE_CHECKING, Callable

from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    AWAIT_TIMEOUT_BUFFER_SECONDS,
    DIRECT_CONNECT_TIMEOUT_SECONDS,
    DISCONNECT_TIMEOUT_SECONDS,
    BLEConfig,
)
from meshtastic.interfaces.ble.coordination import ThreadCoordinator
from meshtastic.interfaces.ble.errors import BLEErrorHandler
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from meshtastic.interfaces.ble.utils import sanitize_address

if TYPE_CHECKING:
    from bleak import BleakClient as BleakRootClient

    from meshtastic.interfaces.ble.discovery import DiscoveryManager
    from meshtastic.interfaces.ble.interface import BLEInterface

logger = logging.getLogger("meshtastic.ble")


class ConnectionValidator:
    """Encapsulate connection pre-checks and reuse logic."""

    def __init__(
        self, state_manager: BLEStateManager, state_lock: RLock, error_class: type
    ) -> None:
        """Create a ConnectionValidator that enforces pre-connection checks for BLE operations.

        Parameters
        ----------
        state_manager : BLEStateManager
            Manager for BLE connection state and transitions.
        state_lock : RLock
            Reentrant lock for synchronizing access to shared BLE state.
        error_class : type
            Exception class raised when validation fails.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.BLEError = error_class

    def _validate_connection_request(self) -> None:
        """Validate that a new BLE connection may be started.

        Raises
        ------
        BLEError
            If connections are not permitted. If the interface is closing the error message will be
            "Cannot connect while interface is closing". If a connection is already established or in progress
            the error message will be "Already connected or connection in progress".
        """
        if not self.state_manager._can_connect:
            if self.state_manager._is_closing:
                raise self.BLEError("Cannot connect while interface is closing")
            raise self.BLEError("Already connected or connection in progress")

    def _check_existing_client(
        self,
        client: BLEClient | None,
        normalized_request: str | None,
        last_connection_request: str | None,
        original_address: str | None = None,
    ) -> bool:
        """Return whether the given BLE client corresponds to the requested or a known device address.

        Considers the provided normalized_request, last_connection_request, original_address,
        and the client's bleak address. If `normalized_request` is `None`, any connected
        client is treated as acceptable.

        Parameters
        ----------
        client : BLEClient | None
            The BLE client to verify.
        normalized_request : str | None
            Sanitized identifier for the desired target; when `None` any connected client matches.
        last_connection_request : str | None
            The last sanitized connection request to include among known targets.
        original_address : str | None
            The original unsanitized address provided by the caller, included among known targets.

        Returns
        -------
        bool
            `true` if the client is connected and its address equals `normalized_request` or one of the known targets, `false` otherwise.
        """
        if not client or not client.isConnected():
            return False
        bleak_client = getattr(client, "bleak_client", None)
        bleak_address = getattr(bleak_client, "address", None)
        normalized_known_targets = {
            t
            for t in (
                last_connection_request,
                sanitize_address(bleak_address),
                sanitize_address(original_address),
            )
            if t is not None
        }
        return (
            normalized_request is None or normalized_request in normalized_known_targets
        )

class ClientManager:
    """Helper for creating, connecting, and closing BLEClient instances."""

    def __init__(
        self,
        state_manager: BLEStateManager,
        state_lock: RLock,
        thread_coordinator: ThreadCoordinator,
        error_handler: "BLEErrorHandler",
    ) -> None:
        """Initialize a ClientManager with the managers, synchronization primitive, and error handler required to manage BLEClient lifecycle.

        Parameters
        ----------
        state_manager : BLEStateManager
            Tracks BLE connection state and the current client.
        state_lock : RLock
            Reentrant lock protecting access to shared BLE state.
        thread_coordinator : ThreadCoordinator
            Creates and manages background threads for client cleanup.
        error_handler : 'BLEErrorHandler'
            Performs safe client shutdown and handles or suppresses errors during close.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator
        self.error_handler = error_handler

    def _create_client(
        self,
        device_address: str,
        disconnect_callback: Callable[["BleakRootClient"], None],
    ) -> BLEClient:
        """Create a BLEClient bound to the given device address and register a disconnect callback.

        Parameters
        ----------
        device_address : str
            Target BLE device address to bind the client to.
        disconnect_callback : 'Callable'
            Callable invoked when the client disconnects.

        Returns
        -------
        'BLEClient'
            A BLEClient instance bound to device_address with the disconnect callback configured.
        """
        return BLEClient(device_address, disconnected_callback=disconnect_callback)

    def _connect_client(self, client: BLEClient, timeout: float | None = None) -> None:
        """Connect the provided BLEClient and ensure its GATT services are populated.

        If the client's discovered services are not available immediately after connecting, service discovery will be requested so characteristics and services are usable.

        Parameters
        ----------
        client : BLEClient
            BLEClient to connect and prepare for use.
        timeout : float | None
            Maximum seconds to wait for the connection; if None, uses BLEConfig.CONNECTION_TIMEOUT. (Default value = None)
        """
        connect_timeout = (
            timeout if timeout is not None else BLEConfig.CONNECTION_TIMEOUT
        )
        # Give the underlying BLE timeout a chance to fail first with clearer context.
        await_timeout = connect_timeout + AWAIT_TIMEOUT_BUFFER_SECONDS
        client.connect(
            await_timeout=await_timeout,
            timeout=connect_timeout,
        )
        try:
            services = getattr(client.bleak_client, "services", None)
        except BleakError as exc:
            logger.debug(
                "BLE services property raised right after connect; forcing discovery: %s",
                exc,
            )
            services = None
        if not services or not getattr(services, "get_characteristic", None):
            logger.debug(
                "BLE services not available immediately after connect; getting services"
            )
            client._get_services()

    def _update_client_reference(
        self,
        new_client: BLEClient,
        old_client: BLEClient | None,
    ) -> None:
        """Schedule asynchronous close of a previous BLE client when replacing it.

        If `old_client` is provided and is a different object than `new_client`, schedules `_safe_close_client(old_client)` to run in a background daemon thread so the caller is not blocked.

        Parameters
        ----------
        new_client : BLEClient
            The client that will become active.
        old_client : BLEClient | None
            The previous client to close if different from `new_client`.
        """
        # Compute the decision under lock, but start the thread after releasing
        # to avoid holding the lock during thread creation/start
        should_close = False
        with self.state_lock:
            if old_client and old_client is not new_client:
                should_close = True

        if should_close:
            close_thread = self.thread_coordinator._create_thread(
                target=self._safe_close_client,
                args=(old_client,),
                name="BLEClientClose",
                daemon=True,
            )
            self.thread_coordinator._start_thread(close_thread)

    def _safe_close_client(self, client: BLEClient, event: Event | None = None) -> None:
        """Attempt to disconnect and close the given BLE client, suppressing any errors and optionally signal completion.

        Parameters
        ----------
        client : BLEClient
            BLE client to disconnect and close.
        event : Event | None
            Optional Event that will be set after cleanup completes. (Default value = None)
        """
        skip_disconnect = bool(getattr(sys, "is_finalizing", lambda: False)())
        if (
            not skip_disconnect
            and not getattr(client, "_closed", False)
            and getattr(client, "bleak_client", None)
        ):
            self.error_handler._safe_cleanup(
                lambda: client.disconnect(await_timeout=DISCONNECT_TIMEOUT_SECONDS),
                "client disconnect",
            )
        elif skip_disconnect:
            logger.debug(
                "Skipping BLE client disconnect during interpreter finalization."
            )
        self.error_handler._safe_cleanup(client.close, "client close")
        if event:
            event.set()


class ConnectionOrchestrator:
    """Coordinate discovery, validation, and notification setup for new connections."""

    def __init__(
        self,
        interface: "BLEInterface",
        validator: ConnectionValidator,
        client_manager: ClientManager,
        discovery_manager: "DiscoveryManager",
        state_manager: BLEStateManager,
        state_lock: RLock,
        thread_coordinator: ThreadCoordinator,
    ) -> None:
        """Coordinate BLE connection orchestration by wiring together the interface, validators, client lifecycle manager, discovery manager, and synchronization primitives.

        Parameters
        ----------
        interface : 'BLEInterface'
            BLE interface used for device discovery and low-level operations.
        validator : ConnectionValidator
            Performs pre-connection validation and reuse checks.
        client_manager : ClientManager
            Creates, connects, and safely closes BLE clients.
        discovery_manager : 'DiscoveryManager'
            Discovers target devices when direct connect fails or is not specified.
        state_manager : BLEStateManager
            Tracks and updates the BLE connection state machine.
        state_lock : RLock
            Reentrant lock protecting access to shared BLE state during transitions.
        thread_coordinator : ThreadCoordinator
            Schedules background tasks and signals threading events (e.g., reconnection notifications).
        """
        self.interface = interface
        self.validator = validator
        self.client_manager = client_manager
        self.discovery_manager = discovery_manager
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator

    def _finalize_connection(
        self,
        client: BLEClient,
        device_address: str,
        register_notifications_func: Callable[[BLEClient], None],
        on_connected_func: Callable[[], None],
    ) -> None:
        """Finalize a successful BLE connection by registering notification handlers, validating the client and orchestrator state, transitioning to CONNECTED, and invoking post-connection callbacks.

        Parameters
        ----------
        client : BLEClient
            The connected BLE client instance.
        device_address : str
            Device address used for logging.
        register_notifications_func : Callable
            Callable that registers notification handlers on `client`.
        on_connected_func : Callable
            Callback invoked after the connection state transitions to CONNECTED.

        Raises
        ------
        BLEInterface.BLEError
            If the orchestrator is not in CONNECTING state or if the client disconnects during finalization.
        BLEError
            If state transitions fail or the client connection is lost.
        """
        # Initial state check under lock before performing blocking I/O
        with self.state_lock:
            current_state = self.state_manager._current_state
            if current_state != ConnectionState.CONNECTING:
                logger.debug(
                    "Connection finalization aborted: state changed from CONNECTING to %s during connect",
                    current_state,
                )
                raise self.interface.BLEError(
                    "Connection invalidated by concurrent disconnect"
                )

        # Register notifications OUTSIDE the lock to avoid blocking state transitions
        # during BLE I/O (start_notify can take up to NOTIFICATION_START_TIMEOUT).
        # This allows disconnect/close/connect to proceed during notification setup.
        register_notifications_func(client)

        # Re-check state after registration under lock for atomic state transition
        with self.state_lock:
            if self.state_manager._current_state != ConnectionState.CONNECTING:
                logger.debug(
                    "Connection finalization aborted: state changed during notification registration"
                )
                raise self.interface.BLEError(
                    "Connection invalidated by concurrent disconnect"
                )

            # Post-registration check: verify client is still connected.
            # This catches disconnects that occurred during notification registration
            # which may have been ignored by _handle_disconnect due to CONNECTING state.
            if not client.isConnected():
                logger.debug(
                    "Connection finalization aborted: client disconnected during notification registration"
                )
                raise self.interface.BLEError(
                    "Connection invalidated: client disconnected during finalization"
                )

            self.state_manager._transition_to(ConnectionState.CONNECTED)

        on_connected_func()
        if getattr(self.interface, "_ever_connected", False):
            self.thread_coordinator._set_event("reconnected_event")
        normalized_device_address = sanitize_address(device_address)
        logger.info(
            "Connection successful to %s",
            normalized_device_address or "unknown",
        )

        """Perform a best-effort state correction after a connection failure.

        Attempts to transition the connection state to ERROR and then to DISCONNECTED; if a transition is rejected, logs a warning and forces DISCONNECTED as a final fallback.

        Parameters
        ----------
        error_context : str
            Context string used in log messages to identify the failure context.
        """
        if not self.state_manager._transition_to(ConnectionState.ERROR):
            logger.warning(
                "Failed state transition to %s during %s (current=%s)",
                ConnectionState.ERROR.value,
                error_context,
                self.state_manager._current_state.value,
            )
        if not self.state_manager._transition_to(ConnectionState.DISCONNECTED):
            logger.warning(
                "Failed state transition to %s during %s (current=%s); forcing reset",
                ConnectionState.DISCONNECTED.value,
                error_context,
                self.state_manager._current_state.value,
            )
            self.state_manager._reset_to_disconnected()

    def _establish_connection(
        self,
        address: str | None,
        current_address: str | None,
        register_notifications_func: Callable[[BLEClient], None],
        on_connected_func: Callable[[], None],
        on_disconnect_func: Callable[["BleakRootClient"], None],
    ) -> BLEClient:
        """Establish a BLE connection to a device, attempting a direct connect when an explicit address is provided and falling back to discovery when needed, then finalize notification registration and lifecycle callbacks.

        Parameters
        ----------
        address : str | None
            Explicit device address to connect to; when None discovery mode is used.
        current_address : str | None
            Fallback address used when `address` is None.
        register_notifications_func : Callable
            Function called with the connected `BLEClient` to register notification handlers.
        on_connected_func : Callable
            Callback invoked after the connection has been finalized and state updated to CONNECTED.
        on_disconnect_func : Callable
            Callback passed to the `BLEClient` to be invoked when the client disconnects.

        Returns
        -------
        BLEClient
            The connected BLE client instance.

        Raises
        ------
        BLEError
            If the request is invalid (e.g., empty address) or a concurrent connection state prevents establishing a connection.
        BleakDBusError
            If a DBus-level BLE error occurs during connection.
        Exception
            If any other error occurs during the connection process.
        """
        self.validator._validate_connection_request()

        target_address = address if address is not None else current_address
        # Allow None target_address for discovery mode - find_device() handles this
        # Only reject empty/whitespace-only strings that are explicitly provided
        if target_address is not None and not target_address.strip():
            raise self.interface.BLEError("Cannot connect: empty address provided")

        normalized_target = sanitize_address(target_address)
        # Note: normalized_target can be None for discovery mode - this is intentional
        # The discovery fallback in find_device() will scan for any Meshtastic device

        if target_address:
            logger.info("Attempting to connect to %s", target_address)
        else:
            logger.info("Attempting discovery-mode connection (no address specified)")

        with self.state_lock:
            if not self.state_manager._transition_to(ConnectionState.CONNECTING):
                raise self.interface.BLEError(
                    "Already connected or connection in progress"
                )
        client: BLEClient | None = None
        try:
            # Only attempt direct connect if we have a target address
            # Discovery mode (target_address=None) skips directly to find_device
            if target_address:
                client = self.client_manager._create_client(
                    target_address, on_disconnect_func
                )
                try:
                    direct_timeout = min(
                        DIRECT_CONNECT_TIMEOUT_SECONDS, BLEConfig.CONNECTION_TIMEOUT
                    )
                    self.client_manager._connect_client(client, timeout=direct_timeout)
                except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
                    raise
                except (BleakError, OSError, TimeoutError) as direct_err:
                    logger.debug(
                        "Direct connect to %s failed; falling back to discovery: %s",
                        normalized_target,
                        direct_err,
                        exc_info=True,
                    )
                    if client:
                        self.client_manager._safe_close_client(client)
                    client = None
                else:
                    self._finalize_connection(
                        client,
                        target_address,
                        register_notifications_func,
                        on_connected_func,
                    )
                    return client

            device = self.interface.findDevice(address or current_address)
            client = self.client_manager._create_client(
                device.address, on_disconnect_func
            )
            self.client_manager._connect_client(client)
            self._finalize_connection(
                client, device.address, register_notifications_func, on_connected_func
            )
            return client
        except BleakDBusError:
            if client:
                self.client_manager._safe_close_client(client)
            self._transition_failure_to_disconnected("BleakDBusError during connect")
            raise
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            # Clean up client before re-raising to avoid resource leak
            if client:
                self.client_manager._safe_close_client(client)
            raise
        except Exception:
            logger.warning(
                "Failed to connect, closing BLEClient thread.", exc_info=True
            )
            if client:
                self.client_manager._safe_close_client(client)
            self._transition_failure_to_disconnected("unexpected connect failure")
            raise
