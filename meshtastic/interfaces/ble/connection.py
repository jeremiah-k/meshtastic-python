"""BLE connection management and validation."""

import logging
from threading import Event, RLock
from typing import Optional

from bleak.exc import BleakDBusError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import BLEConfig
from meshtastic.interfaces.ble.coordination import ThreadCoordinator
from meshtastic.interfaces.ble.errors import BLEErrorHandler
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface
    from meshtastic.interfaces.ble.discovery import DiscoveryManager

logger = logging.getLogger("meshtastic.ble")

class ConnectionValidator:
    """Encapsulate connection pre-checks and reuse logic."""

    def __init__(
        self, state_manager: BLEStateManager, state_lock: RLock, error_class: type
    ):
        """
        Initialize the ConnectionValidator with the BLE state manager and its lock.
        
        Parameters:
            state_manager (BLEStateManager): Manager responsible for BLE connection state and transitions.
            state_lock (RLock): Reentrant lock used to synchronize access to the shared state.
            error_class (type): Exception type to raise when validation fails.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.BLEError = error_class

    def validate_connection_request(self) -> None:
        """
        Validate that initiating a new BLE connection is permitted.
        
        Raises:
            BLEInterface.BLEError: If the interface is closing (message: "Cannot connect while interface is closing")
                or if a connection is already established or in progress (message: "Already connected or connection in progress").
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
        Check whether the provided client is connected and corresponds to the requested or known target address.
        
        Parameters:
            client (Optional[BLEClient]): The client to check.
            normalized_request (Optional[str]): A sanitized identifier for the desired target; if None the function treats any connected client as matching.
            last_connection_request (Optional[str]): The last-sent sanitized connection request to include among known targets.
            address (Optional[str]): The address originally requested for connection; included among known targets after normalization.
        
        Returns:
            `True` if the client is connected and its address matches `normalized_request` or one of the known targets, `False` otherwise.
        """
        if not client or not client.is_connected():
            return False
        bleak_client = getattr(client, "bleak_client", None)
        bleak_address = getattr(bleak_client, "address", None)
        normalized_known_targets = {
            last_connection_request,
            BLEClient._sanitize_address(address),
            BLEClient._sanitize_address(bleak_address),
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
    ):
        """
        Initialize the ClientManager with the objects required to manage BLE client lifecycle.
        
        Parameters:
            state_manager (BLEStateManager): Manager that tracks BLE connection state and current client.
            state_lock (RLock): Reentrant lock protecting access to shared BLE state.
            thread_coordinator (ThreadCoordinator): Coordinator used to create and manage background threads for cleanup.
            error_handler (BLEErrorHandler): Handler used to perform safe cleanup and suppress or surface errors during client close.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator
        self.error_handler = error_handler

    def create_client(self, device_address: str, disconnect_callback) -> "BLEClient":
        """
        Create a BLEClient configured for the specified device address and disconnect callback.
        
        Parameters:
            device_address (str): The BLE device address to connect to.
            disconnect_callback (callable): Function to be called when the client disconnects.
        
        Returns:
            ble_client (BLEClient): A new BLEClient instance bound to the given address with the provided disconnect callback.
        """
        return BLEClient(device_address, disconnected_callback=disconnect_callback)

    def connect_client(self, client: "BLEClient") -> None:
        """
        Connect the given BLE client and ensure its GATT services are available.
        
        If services are not immediately present on the underlying bleak client, this will request service discovery so the client has usable service/characteristic information.
        
        Parameters:
            client (BLEClient): BLE client to connect and prepare for use.
        """
        client.connect(
            await_timeout=BLEConfig.CONNECTION_TIMEOUT,
            timeout=BLEConfig.CONNECTION_TIMEOUT,
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
        Schedule safe closure of a previous BLE client when it is being replaced.
        
        If `old_client` is provided and is not the same object as `new_client`, this schedules a background daemon thread to invoke safe closure of `old_client`.
        
        Parameters:
            new_client (BLEClient): The client that will become the active client.
            old_client (Optional[BLEClient]): The previous client to close if it differs from `new_client`.
        """
        with self.state_lock:
            if old_client and old_client is not new_client:
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
        Close the given BLE client and suppress errors raised during shutdown.
        
        If provided, sets the given event after attempting to close the client to signal completion.
        
        Parameters:
            client (BLEClient): The client to close.
            event (Optional[Event]): Event to set after closing to indicate the operation finished.
        """
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
    ):
        """
        Initialize a ConnectionOrchestrator with its required BLE components and coordination helpers.
        
        Parameters:
            interface (BLEInterface): BLE interface used for device discovery and low-level operations.
            validator (ConnectionValidator): Performs pre-connection validation checks.
            client_manager (ClientManager): Manages BLEClient creation, connection, and cleanup.
            discovery_manager (DiscoveryManager): Handles device discovery and related operations.
            state_manager (BLEStateManager): Tracks and updates BLE connection state.
            state_lock (RLock): Lock protecting access to shared BLE state.
            thread_coordinator (ThreadCoordinator): Schedules background tasks and coordinates threading events.
        """
        self.interface = interface
        self.validator = validator
        self.client_manager = client_manager
        self.discovery_manager = discovery_manager
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator

    def establish_connection(
        self,
        address: Optional[str],
        current_address: Optional[str],
        register_notifications_func,
        on_connected_func,
        on_disconnect_func,
    ) -> "BLEClient":
        """
        Establishes a BLE connection to a target device, registers notifications, and invokes lifecycle callbacks.
        
        If `address` is provided it is used as the connection target; otherwise `current_address` is used. Transitions the connection state through CONNECTING to CONNECTED on success, sets the "reconnected_event" on the thread coordinator, and invokes `on_connected_func`. On failure the method closes any partially created client, transitions the state to ERROR and then DISCONNECTED, and re-raises the exception.
        
        Parameters:
            address (Optional[str]): Explicit device address to connect to; if None `current_address` will be used.
            current_address (Optional[str]): Fallback device address when `address` is None.
            register_notifications_func (callable): Function called with the connected `BLEClient` to register notification handlers.
            on_connected_func (callable): Callback invoked once the connection is established and state is updated to CONNECTED.
            on_disconnect_func (callable): Callback passed to the `BLEClient` to be invoked when the client disconnects.
        
        Returns:
            BLEClient: The connected BLE client instance.
        """
        self.validator.validate_connection_request()

        target_address = address if address is not None else current_address
        normalized_target = BLEClient._sanitize_address(target_address)
        logger.info("Attempting to connect to %s", target_address or "any")
        with self.state_lock:
            if not self.state_manager.transition_to(ConnectionState.CONNECTING):
                raise self.interface.BLEError(
                    "Already connected or connection in progress"
                )

        client: Optional["BLEClient"] = None
        try:
            if normalized_target:
                client = self.client_manager.create_client(
                    target_address, on_disconnect_func
                )
                try:
                    self.client_manager.connect_client(client)
                    register_notifications_func(client)
                    if self.state_manager.state == ConnectionState.DISCONNECTED:
                        self.state_manager.transition_to(
                            ConnectionState.CONNECTING, client
                        )
                    self.state_manager.transition_to(
                        ConnectionState.CONNECTED, client
                    )
                    on_connected_func()
                    if getattr(self.interface, "_ever_connected", False):
                        self.thread_coordinator.set_event("reconnected_event")
                    normalized_device_address = BLEClient._sanitize_address(
                        target_address
                    )
                    logger.info(
                        "Connection successful to %s",
                        normalized_device_address or "unknown",
                    )
                    return client
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
            register_notifications_func(client)
            # If a disconnect callback raced and moved us back to DISCONNECTED during connect,
            # reassert CONNECTING before marking CONNECTED to avoid invalid transition warnings.
            if self.state_manager.state == ConnectionState.DISCONNECTED:
                self.state_manager.transition_to(ConnectionState.CONNECTING, client)
            self.state_manager.transition_to(ConnectionState.CONNECTED, client)
            on_connected_func()
            if getattr(self.interface, "_ever_connected", False):
                self.thread_coordinator.set_event("reconnected_event")
            normalized_device_address = BLEClient._sanitize_address(device.address)
            logger.info(
                "Connection successful to %s", normalized_device_address or "unknown"
            )
        except BleakDBusError:
            if client:
                self.client_manager.safe_close_client(client)
            self.state_manager.transition_to(ConnectionState.ERROR)
            self.state_manager.transition_to(ConnectionState.DISCONNECTED)
            raise
        except Exception:
            logger.warning("Failed to connect, closing BLEClient thread.", exc_info=True)
            if client:
                self.client_manager.safe_close_client(client)
            self.state_manager.transition_to(ConnectionState.ERROR)
            # Reset to DISCONNECTED so future connection attempts are permitted
            self.state_manager.transition_to(ConnectionState.DISCONNECTED)
            raise
        else:
            return client
