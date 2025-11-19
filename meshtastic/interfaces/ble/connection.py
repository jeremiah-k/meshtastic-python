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
        self.state_manager = state_manager
        self.state_lock = state_lock

    def validate_connection_request(self) -> None:
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
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator
        self.error_handler = error_handler

    def create_client(self, device_address: str, disconnect_callback) -> "BLEClient":
        # We need to import BLEClient here to avoid circular dependencies
        from .client import BLEClient

        return BLEClient(device_address, disconnected_callback=disconnect_callback)

    def connect_client(self, client: "BLEClient") -> None:
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
        Close the provided BLEClient and suppress common close-time errors.
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
        last_connection_request: Optional[str],
        register_notifications_func,
        on_connected_func,
        on_disconnect_func,
    ) -> "BLEClient":
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
            normalized_device_address = _sanitize_address(device.address)
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
