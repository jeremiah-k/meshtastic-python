"""BLE connection management and validation."""

import logging
import sys
from threading import Event, RLock
from typing import TYPE_CHECKING, Callable, Optional

from bleak.exc import BleakDBusError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import DISCONNECT_TIMEOUT_SECONDS, BLEConfig
from meshtastic.interfaces.ble.coordination import ThreadCoordinator
from meshtastic.interfaces.ble.errors import BLEErrorHandler
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from meshtastic.interfaces.ble.utils import sanitize_address

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.discovery import DiscoveryManager
    from meshtastic.interfaces.ble.interface import BLEInterface

logger = logging.getLogger("meshtastic.ble")

# Direct connect timeout for known device addresses.
# Kept shorter than full CONNECTION_TIMEOUT to allow quick fallback to discovery
# if the device has moved to a different adapter or requires fresh discovery.
# This prevents long waits on stale connection attempts.
DIRECT_CONNECT_TIMEOUT_SECONDS: float = 12.0

# Extra margin added to await_timeout so the BLE-level timeout fires first and
# surfaces clearer context before the outer future wait times out.
AWAIT_TIMEOUT_BUFFER_SECONDS: float = 5.0


class ConnectionValidator:
    """Encapsulate connection pre-checks and reuse logic."""

    def __init__(
        self, state_manager: BLEStateManager, state_lock: RLock, error_class: type
    ) -> None:
        """
        Create a ConnectionValidator that enforces BLE connection pre-checks using the provided state manager and lock.

        Parameters
        ----------
            state_manager (BLEStateManager): Manager responsible for BLE connection state and transitions.
            state_lock (RLock): Reentrant lock used to synchronize access to the shared BLE state.
            error_class (type): Exception type to raise when a connection validation fails.

        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.BLEError = error_class

    def validate_connection_request(self) -> None:
        """
        Validate that a new BLE connection may be started.

        Raises:
            BLEError: If connections are not permitted. If the interface is closing the error message will be
                "Cannot connect while interface is closing". If a connection is already established or in progress
                the error message will be "Already connected or connection in progress".

        """
        if not self.state_manager.can_connect:
            if self.state_manager.is_closing:
                raise self.BLEError("Cannot connect while interface is closing")
            raise self.BLEError("Already connected or connection in progress")

    def check_existing_client(
        self,
        client: Optional["BLEClient"],
        normalized_request: Optional[str],
        last_connection_request: Optional[str],
        address: Optional[str],
    ) -> bool:
        """
        Determine whether a connected BLE client matches the requested or known device address.

        Considers the provided normalized_request, the last_connection_request, and the sanitized forms of the original address and the client's bleak address when deciding a match.

        Parameters
        ----------
            client (Optional[BLEClient]): The BLE client to verify.
            normalized_request (Optional[str]): Sanitized identifier for the desired target; if `None`, any connected client is treated as acceptable for matching.
            last_connection_request (Optional[str]): The last sanitized connection request to include among known targets.
            address (Optional[str]): The originally requested address; its sanitized form is compared against the client's address.

        Returns
        -------
            True if the client is connected and its address equals `normalized_request` or one of the known targets, False otherwise.

        """
        if not client or not client.is_connected():
            return False
        bleak_client = getattr(client, "bleak_client", None)
        bleak_address = getattr(bleak_client, "address", None)
        normalized_known_targets = {
            last_connection_request,
            sanitize_address(address),
            sanitize_address(bleak_address),
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
        """
        Initialize a ClientManager with the managers, synchronization primitive, and error handler required to manage BLEClient lifecycle.

        Parameters
        ----------
            state_manager (BLEStateManager): Tracks BLE connection state and the current client.
            state_lock (RLock): Reentrant lock protecting access to shared BLE state.
            thread_coordinator (ThreadCoordinator): Creates and manages background threads for client cleanup.
            error_handler (BLEErrorHandler): Performs safe client shutdown and handles or suppresses errors during close.

        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator
        self.error_handler = error_handler

    def create_client(
        self, device_address: str, disconnect_callback: "Callable"
    ) -> "BLEClient":
        """
        Create a BLEClient for the specified device address and register a disconnect callback.

        Parameters
        ----------
            device_address (str): The BLE device address to bind the client to.
            disconnect_callback (Callable): Callback invoked when the client disconnects.

        Returns
        -------
            BLEClient: A BLEClient instance bound to device_address with the disconnect callback set.

        """
        return BLEClient(device_address, disconnected_callback=disconnect_callback)

    def connect_client(
        self, client: "BLEClient", timeout: Optional[float] = None
    ) -> None:
        """
        Connect the provided BLEClient and ensure its GATT services are populated.

        If the client's discovered services are not available immediately after connecting, service discovery will be requested so characteristics and services are usable.

        Parameters
        ----------
            client: BLEClient to connect and prepare for use.
            timeout: Maximum seconds to wait for the connection; if None, uses BLEConfig.CONNECTION_TIMEOUT.

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
        services = getattr(client.bleak_client, "services", None)
        if not services or not getattr(services, "get_characteristic", None):
            logger.debug(
                "BLE services not available immediately after connect; getting services"
            )
            client.get_services()

    def update_client_reference(
        self,
        new_client: "BLEClient",
        old_client: Optional["BLEClient"],
    ) -> None:
        """
        Schedule closure of a previous BLE client in a background daemon thread when replacing it with a new client.

        If `old_client` is provided and is a different object than `new_client`, schedules `safe_close_client(old_client)` to run in a daemon thread after releasing the state lock.

        Parameters
        ----------
            new_client (BLEClient): The client becoming active.
            old_client (Optional[BLEClient]): The previous client to close if different from `new_client`.

        """
        # Compute the decision under lock, but start the thread after releasing
        # to avoid holding the lock during thread creation/start
        should_close = False
        with self.state_lock:
            if old_client and old_client is not new_client:
                should_close = True

        if should_close:
            close_thread = self.thread_coordinator.create_thread(
                target=self.safe_close_client,
                args=(old_client,),
                name="BLEClientClose",
                daemon=True,
            )
            self.thread_coordinator.start_thread(close_thread)

    def safe_close_client(
        self, client: "BLEClient", event: Optional[Event] = None
    ) -> None:
        """
        Close a BLE client while suppressing shutdown errors and optionally signal completion.

        Performs a best-effort disconnect followed by closing the client; any exceptions raised during disconnect or close are suppressed.

        Parameters
        ----------
            client (BLEClient): The BLE client to disconnect and close.
            event (Optional[Event]): If provided, will be set after the close attempt to signal completion.

        """
        skip_disconnect = bool(getattr(sys, "is_finalizing", lambda: False)())
        if (
            not skip_disconnect
            and not getattr(client, "_closed", False)
            and getattr(client, "bleak_client", None)
        ):
            self.error_handler.safe_cleanup(
                lambda: client.disconnect(await_timeout=DISCONNECT_TIMEOUT_SECONDS),
                "client disconnect",
            )
        elif skip_disconnect:
            logger.debug(
                "Skipping BLE client disconnect during interpreter finalization."
            )
        self.error_handler.safe_cleanup(client.close, "client close")
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
        """
        Coordinate BLE connection orchestration by wiring together the interface, validators, client lifecycle manager, discovery manager, and synchronization primitives.

        Parameters
        ----------
            interface (BLEInterface): BLE interface used for device discovery and low-level operations.
            validator (ConnectionValidator): Performs pre-connection validation and reuse checks.
            client_manager (ClientManager): Creates, connects, and safely closes BLE clients.
            discovery_manager (DiscoveryManager): Discovers target devices when direct connect fails or is not specified.
            state_manager (BLEStateManager): Tracks and updates the BLE connection state machine.
            state_lock (RLock): Reentrant lock protecting access to shared BLE state during transitions.
            thread_coordinator (ThreadCoordinator): Schedules background tasks and signals threading events (e.g., reconnection notifications).

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
        client: "BLEClient",
        device_address: str,
        register_notifications_func: "Callable",
        on_connected_func: "Callable",
    ) -> None:
        """
        Finalize a successful BLE connection by registering notifications, verifying the client remains connected, transitioning state to CONNECTED, invoking the connected callback, signaling a reconnection event if applicable, and logging success.

        Parameters
        ----------
            client (BLEClient): The connected BLE client instance.
            device_address (str): Device address used for logging.
            register_notifications_func (Callable): Function that registers notification handlers on the client.
            on_connected_func (Callable): Callback invoked after the state transitions to CONNECTED.

        Raises
        ------
            BLEInterface.BLEError: If the connection is invalidated by a concurrent disconnect or if the client disconnects during finalization.

        """
        # Initial state check under lock before performing blocking I/O
        with self.state_lock:
            current_state = self.state_manager.state
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
            if self.state_manager.state != ConnectionState.CONNECTING:
                logger.debug(
                    "Connection finalization aborted: state changed during notification registration"
                )
                raise self.interface.BLEError(
                    "Connection invalidated by concurrent disconnect"
                )

            # Post-registration check: verify client is still connected.
            # This catches disconnects that occurred during notification registration
            # which may have been ignored by _handle_disconnect due to CONNECTING state.
            if not client.is_connected():
                logger.debug(
                    "Connection finalization aborted: client disconnected during notification registration"
                )
                raise self.interface.BLEError(
                    "Connection invalidated: client disconnected during finalization"
                )

            self.state_manager.transition_to(ConnectionState.CONNECTED)

        on_connected_func()
        if getattr(self.interface, "_ever_connected", False):
            self.thread_coordinator.set_event("reconnected_event")
        normalized_device_address = sanitize_address(device_address)
        logger.info(
            "Connection successful to %s",
            normalized_device_address or "unknown",
        )

    def establish_connection(
        self,
        address: Optional[str],
        current_address: Optional[str],
        register_notifications_func: "Callable",
        on_connected_func: "Callable",
        on_disconnect_func: "Callable",
    ) -> "BLEClient":
        """
        Finalize and establish a BLE connection to a device, register notifications, and invoke lifecycle callbacks.

        Attempts a direct connect when an explicit address is provided, falls back to discovery when direct connect fails or when no address is given, and transitions connection state from CONNECTING to CONNECTED on success. On failure the client (if created) is closed and the state is moved to ERROR then DISCONNECTED.

        Parameters
        ----------
            address (Optional[str]): Explicit device address to connect to; if None, discovery mode is used or `current_address` is used as a hint.
            current_address (Optional[str]): Fallback device address when `address` is None.
            register_notifications_func (Callable): Function called with the connected `BLEClient` to register notification handlers.
            on_connected_func (Callable): Callback invoked after the connection has been finalized and state updated to CONNECTED.
            on_disconnect_func (Callable): Callback passed to the `BLEClient` to be invoked when the client disconnects.

        Returns
        -------
            BLEClient: The connected BLE client instance.

        Raises
        ------
            BLEError: If the request is invalid (e.g., empty address) or a concurrent connection state prevents establishing a connection.
            BleakDBusError: When a DBus-level BLE error occurs during connection.

        """
        self.validator.validate_connection_request()

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
            if not self.state_manager.transition_to(ConnectionState.CONNECTING):
                raise self.interface.BLEError(
                    "Already connected or connection in progress"
                )
        client: Optional["BLEClient"] = None
        try:
            # Only attempt direct connect if we have a target address
            # Discovery mode (target_address=None) skips directly to find_device
            if target_address:
                client = self.client_manager.create_client(
                    target_address, on_disconnect_func
                )
                try:
                    direct_timeout = min(
                        DIRECT_CONNECT_TIMEOUT_SECONDS, BLEConfig.CONNECTION_TIMEOUT
                    )
                    self.client_manager.connect_client(client, timeout=direct_timeout)
                    self._finalize_connection(
                        client,
                        target_address,
                        register_notifications_func,
                        on_connected_func,
                    )
                    return client
                except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
                    raise
                except Exception as direct_err:
                    logger.debug(
                        "Direct connect to %s failed; falling back to discovery: %s",
                        normalized_target,
                        direct_err,
                        exc_info=True,
                    )
                    if client:
                        self.client_manager.safe_close_client(client)
                    client = None

            device = self.interface.find_device(address or current_address)
            client = self.client_manager.create_client(
                device.address, on_disconnect_func
            )
            self.client_manager.connect_client(client)
            self._finalize_connection(
                client, device.address, register_notifications_func, on_connected_func
            )
        except BleakDBusError:
            if client:
                self.client_manager.safe_close_client(client)
            self.state_manager.transition_to(ConnectionState.ERROR)
            self.state_manager.transition_to(ConnectionState.DISCONNECTED)
            raise
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            raise
        except Exception:
            logger.warning(
                "Failed to connect, closing BLEClient thread.", exc_info=True
            )
            if client:
                self.client_manager.safe_close_client(client)
            self.state_manager.transition_to(ConnectionState.ERROR)
            # Reset to DISCONNECTED so future connection attempts are permitted
            self.state_manager.transition_to(ConnectionState.DISCONNECTED)
            raise
        else:
            return client
