"""BLE connection management."""
import logging
from threading import Event, RLock
from typing import Optional, TYPE_CHECKING

from .client import BLEClient
from .config import BLEConfig
from .discovery import DiscoveryManager
from .error_handler import BLEErrorHandler
from .state import BLEStateManager, ConnectionState
from .threads import ThreadCoordinator
from .util import sanitize_address
from .exceptions import BLEError

if TYPE_CHECKING:
    from meshtastic.ble_interface import BLEInterface

logger = logging.getLogger(__name__)


class ConnectionValidator:
    """Encapsulate connection pre-checks and reuse logic."""

    def __init__(self, state_manager: BLEStateManager, state_lock: RLock):
        """
        Initialize the connection helper with a BLE state manager and its synchronization lock.
        
        Parameters:
            state_manager (BLEStateManager): Manager that holds and updates BLE connection state.
            state_lock (RLock): Reentrant lock used to synchronize access to the state_manager.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock

    def validate_connection_request(self) -> None:
        """
        Validate whether a new BLE connection may be initiated.
        
        Raises:
            BLEError: If the interface is closing ("Cannot connect while interface is closing")
                     or if a connection is already active or in progress ("Already connected or connection in progress").
        """
        with self.state_lock:
            if not self.state_manager.can_connect:
                if self.state_manager.is_closing:
                    raise BLEError("Cannot connect while interface is closing")
                raise BLEError("Already connected or connection in progress")

    def check_existing_client(
        self,
        client: Optional["BLEClient"],
        normalized_request: Optional[str],
        last_connection_request: Optional[str],
        address: Optional[str],
    ) -> bool:
        """
        Determine whether the given BLE client is currently connected and corresponds to the requested target address.
        
        Parameters:
            client (Optional[BLEClient]): The BLE client to check.
            normalized_request (Optional[str]): The (possibly pre-normalized) target address to match against; if None, any connected client is considered a match.
            last_connection_request (Optional[str]): The last requested address used by the manager; used as a potential match target.
            address (Optional[str]): The explicit address provided for this connection attempt; used as a potential match target.
        
        Returns:
            bool: `True` if `client` is connected and its address (or the provided known addresses) matches `normalized_request` (or if `normalized_request` is `None`), `False` otherwise.
        """
        if not client or not client.is_connected():
            return False
        bleak_client = getattr(client, "bleak_client", None)
        bleak_address = getattr(bleak_client, "address", None)
        normalized_known_targets = {
            sanitize_address(last_connection_request),
            sanitize_address(address),
            sanitize_address(bleak_address),
        }
        return (
            normalized_request is None
            or sanitize_address(normalized_request) in normalized_known_targets
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
        Initialize the ClientManager and store the runtime helpers required to manage BLE clients.
        
        Stores references to the BLE state manager, the reentrant lock protecting state, the thread coordinator for background tasks, and the BLE error handler.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator
        self.error_handler = error_handler

    def create_client(self, device_address: str, disconnect_callback) -> "BLEClient":
        """
        Create a BLEClient configured for the given device address and disconnect handler.
        
        Parameters:
            device_address (str): The device address or identifier used to instantiate the client.
            disconnect_callback (callable): Callback invoked when the client disconnects.
        
        Returns:
            BLEClient: A new BLEClient instance bound to the specified device address with the provided disconnect callback.
        """
        return BLEClient(device_address, disconnected_callback=disconnect_callback)

    def connect_client(self, client: "BLEClient") -> None:
        """
        Ensure the provided BLEClient is connected and its GATT services are available.
        
        Attempts to connect the client using the configured connection timeout (BLEConfig.CONNECTION_TIMEOUT). If the underlying bleak client does not expose service information immediately after connect, explicitly requests service discovery from the client.
        
        Parameters:
        	client (BLEClient): The client to connect and ensure has discovered services.
        """
        client.connect(await_timeout=BLEConfig.CONNECTION_TIMEOUT)
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
        Schedule a background close of an old BLE client when it is different from the new client.
        
        Parameters:
            new_client (BLEClient): The client that will replace the existing reference.
            old_client (Optional[BLEClient]): The previous client to close if present and different from new_client.
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
        Close the given BLE client and suppress common errors that can occur during shutdown.
        
        Parameters:
            client ("BLEClient"): The BLE client instance to close; errors raised by its close method are handled and suppressed.
            event (Optional[Event]): If provided, the event will be set after the client has been closed to signal completion.
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
        discovery_manager: DiscoveryManager,
        state_manager: BLEStateManager,
        state_lock: RLock,
        thread_coordinator: ThreadCoordinator,
    ):
        """
        Initialize the connection orchestrator with required managers and synchronization primitives.
        
        Parameters:
            interface: BLE interface used for device discovery and low-level disconnect/close helpers.
            validator: ConnectionValidator that enforces pre-connection checks.
            client_manager: ClientManager responsible for creating, connecting, and closing BLE clients.
            discovery_manager: DiscoveryManager used to locate BLE devices.
            state_manager: BLEStateManager that holds connection state.
            state_lock: RLock protecting access to the state_manager.
            thread_coordinator: ThreadCoordinator used to schedule background tasks and signal events.
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
        Establishes a BLE connection to the provided address (or the current address) and registers notifications and lifecycle callbacks.
        
        Parameters:
            address (Optional[str]): Target device address to connect to; if None, the current_address will be used.
            current_address (Optional[str]): Fallback device address to use when `address` is None.
            register_notifications_func (callable): Function that accepts a `BLEClient` and registers notification handlers on it.
            on_connected_func (callable): Callback invoked after the client transitions to CONNECTED.
            on_disconnect_func (callable): Callback passed to the created `BLEClient` to be invoked on unexpected disconnection.
        
        Returns:
            BLEClient: The connected BLE client instance.
        
        Raises:
            Exception: Re-raises any exception encountered during device discovery, client creation, connection, or notification registration. On failure the orchestrator attempts to close the client and resets connection state before propagating the error.
        """
        self.validator.validate_connection_request()

        target_address = address if address is not None else current_address
        logger.info("Attempting to connect to %s", target_address or "any")
        self.state_manager.transition_to(ConnectionState.CONNECTING)

        client: Optional["BLEClient"] = None
        try:
            device = self.interface.find_device(address or current_address)
            client = self.client_manager.create_client(
                device.address, on_disconnect_func
            )
            self.client_manager.connect_client(client)
            register_notifications_func(client)
            self.state_manager.transition_to(ConnectionState.CONNECTED, client)
            on_connected_func()
            self.thread_coordinator.set_event("reconnected_event")
            logger.info("Connection successful to %s", client.bleak_client.address)
            return client
        except Exception:
            logger.debug("Failed to connect, closing BLEClient thread.", exc_info=True)
            if client:
                try:
                    self.interface._disconnect_and_close_client(client)
                except Exception:
                    # Fall back to safe close if the interface helper raises unexpectedly.
                    self.client_manager.safe_close_client(client)
            self.state_manager.transition_to(ConnectionState.ERROR)
            # Reset to DISCONNECTED so future connection attempts are permitted
            self.state_manager.transition_to(ConnectionState.DISCONNECTED)
            raise