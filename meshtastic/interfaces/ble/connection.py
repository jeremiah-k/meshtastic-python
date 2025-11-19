"""BLE connection management"""

import logging
from threading import Event, RLock
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from meshtastic.ble_interface import BLEInterface

from .state import BLEStateManager, ConnectionState
from .util import BLEErrorHandler, ThreadCoordinator, _sanitize_address
from .config import BLEConfig
from .discovery import DiscoveryManager
from .exceptions import BLEError

if TYPE_CHECKING:
    from .client import BLEClient


logger = logging.getLogger(__name__)


class ConnectionValidator:
    """Encapsulate connection pre-checks and reuse logic."""

    def __init__(self, state_manager: BLEStateManager, state_lock: RLock):
        """
        Initialize the validator with the BLE state manager and a reentrant lock for state synchronization.
        
        Parameters:
            state_manager (BLEStateManager): Manager that tracks and updates BLE connection state.
            state_lock (RLock): Reentrant lock used to synchronize access to the shared state.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock

    def validate_connection_request(self) -> None:
        """
        Ensure a new BLE connection may be initiated given the current state.
        
        Raises:
            BLEError: If the interface is closing ("Cannot connect while interface is closing")
                or if a connection already exists or is in progress ("Already connected or connection in progress").
        """
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
        Determine whether the provided connected client corresponds to the given connection request or known targets.
        
        Parameters:
            client: The BLE client instance to inspect; must be connected to be considered.
            normalized_request: The normalized identifier for the current connection request (or None to accept any connected client).
            last_connection_request: The normalized identifier of the last connection request associated with the client.
            address: A device address to compare against the client's address (may be raw/un-normalized).
        
        Returns:
            `true` if the client is connected and its known identifiers (last request or address) match `normalized_request`, or if `normalized_request` is `None`; `false` otherwise.
        """
        if not client or not client.is_connected():
            return False
        bleak_client = getattr(client, "bleak_client", None)
        bleak_address = getattr(bleak_client, "address", None)
        normalized_known_targets = {
            last_connection_request,
            _sanitize_address(address),
            _sanitize_address(bleak_address),
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
        Initialize the client manager with BLE state access, synchronization primitives, thread coordination, and error handling.
        
        Parameters:
            state_manager (BLEStateManager): Manages BLE connection state and transitions.
            state_lock (RLock): Recursive lock used to synchronize access to the shared BLE state.
            thread_coordinator (ThreadCoordinator): Coordinator for creating and starting background threads for client cleanup.
            error_handler (BLEErrorHandler): Utility used for safe cleanup and error-tolerant disconnect/close operations.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator
        self.error_handler = error_handler

    def create_client(self, device_address: str, disconnect_callback) -> "BLEClient":
        # We need to import BLEClient here to avoid circular dependencies
        """
        Create a BLEClient for the specified device address and attach a disconnect callback.
        
        Parameters:
            device_address (str): Address or identifier of the target BLE device.
            disconnect_callback (Callable): Callback invoked when the client disconnects.
        
        Returns:
            BLEClient: A new BLEClient instance configured for the device.
        """
        from .client import BLEClient

        return BLEClient(device_address, disconnected_callback=disconnect_callback)

    def connect_client(self, client: "BLEClient") -> None:
        """
        Establish a connection for the given BLE client and verify its services are available.
        
        Parameters:
            client (BLEClient): The BLE client to connect and validate.
        """
        client.connect(await_timeout=BLEConfig.CONNECTION_TIMEOUT)
        client.ensure_services_available()

    def update_client_reference(
        self,
        new_client: "BLEClient",
        old_client: Optional["BLEClient"],
    ) -> None:
        """
        If the provided old client differs from the new client, schedules a background (daemon) thread to close the old client while holding the state lock.
        
        Parameters:
            new_client (BLEClient): The client intended to become the active client.
            old_client (Optional[BLEClient]): The previous client; if present and not the same object as `new_client`, it will be closed asynchronously.
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
        Perform a best-effort disconnect and close of the given BLEClient, suppressing any errors raised during cleanup.
        
        Parameters:
            client (BLEClient): The client to disconnect and close.
            event (Optional[Event]): If provided, will be set after cleanup completes.
        """
        self.error_handler.safe_cleanup(
            lambda: client.disconnect(await_timeout=BLEConfig.CONNECTION_TIMEOUT),
            "client disconnect",
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
        discovery_manager: DiscoveryManager,
        state_manager: BLEStateManager,
        state_lock: RLock,
        thread_coordinator: ThreadCoordinator,
    ):
        """
        Initialize the ConnectionOrchestrator with its required collaborators and synchronization primitives.
        
        Parameters:
            interface (BLEInterface): BLE hardware/interface abstraction used to discover and interact with devices.
            validator (ConnectionValidator): Validator that enforces pre-connection state rules.
            client_manager (ClientManager): Manager responsible for creating, connecting, and closing BLE clients.
            discovery_manager (DiscoveryManager): Component used to perform BLE device discovery.
            state_manager (BLEStateManager): Shared state container tracking current connection state and active client.
            state_lock (RLock): Reentrant lock used to synchronize access to the shared state_manager.
            thread_coordinator (ThreadCoordinator): Coordinator used to spawn and signal background threads (e.g., for cleanup or reconnection events).
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
        Establishes a BLE connection to the specified or current address and returns the connected BLEClient.
        
        Parameters:
            address (Optional[str]): Preferred device address to connect to; if None, current_address is used.
            current_address (Optional[str]): Fallback device address to use when address is not provided.
            register_notifications_func: Callable that takes a BLEClient and registers required notification handlers.
            on_connected_func: Callable invoked immediately after a successful connection and state transition.
            on_disconnect_func: Callable invoked by the BLEClient when the remote device disconnects; passed to client creation.
        
        Returns:
            BLEClient: The connected BLEClient instance.
        """
        with self.state_lock:
            self.validator.validate_connection_request()
            target_address = address if address is not None else current_address
            logger.info("Attempting to connect to %s", target_address or "any")
            self.state_manager.transition_to(ConnectionState.CONNECTING)

        known_address = getattr(self.interface, "_known_device_address", None)
        normalized_target = _sanitize_address(
            address if address is not None else current_address
        )
        normalized_known = _sanitize_address(known_address)
        reuse_address = (
            known_address
            if normalized_target
            and normalized_known
            and normalized_target == normalized_known
            else None
        )

        client: Optional["BLEClient"] = None
        resolved_address: Optional[str] = None
        try:
            if reuse_address:
                logger.debug(
                    "Attempting direct connection to cached BLE address %s",
                    reuse_address,
                )
                try:
                    client = self.client_manager.create_client(
                        reuse_address, on_disconnect_func
                    )
                    self.client_manager.connect_client(client)
                    resolved_address = reuse_address
                except Exception:
                    logger.debug(
                        "Direct connection to %s failed; falling back to discovery.",
                        reuse_address,
                        exc_info=True,
                    )
                    if client:
                        self.client_manager.safe_close_client(client)
                        client = None
                    resolved_address = None

            if client is None:
                device = self.interface.find_device(address or current_address)
                resolved_address = device.address
                client = self.client_manager.create_client(
                    resolved_address, on_disconnect_func
                )
                self.client_manager.connect_client(client)

            register_notifications_func(client)
            self.state_manager.transition_to(ConnectionState.CONNECTED, client)
            on_connected_func()
            self.thread_coordinator.set_event("reconnected_event")
            normalized_device_address = _sanitize_address(resolved_address)
            logger.info(
                "Connection successful to %s", normalized_device_address or "unknown"
            )
            return client
        except Exception:
            logger.debug("Failed to connect, closing BLEClient thread.", exc_info=True)
            if client:
                self.client_manager.safe_close_client(client)
            self.state_manager.transition_to(ConnectionState.ERROR)
            # Reset to DISCONNECTED so future connection attempts are permitted
            self.state_manager.transition_to(ConnectionState.DISCONNECTED)
            raise
