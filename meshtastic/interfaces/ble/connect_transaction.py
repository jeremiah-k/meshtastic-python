"""Value objects for one BLE client-establishment transaction."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient


@dataclass(frozen=True, slots=True)
class _BLEConnectStateSnapshot:
    """Interface-owned state captured before a provisional BLE connection."""

    client: "BLEClient | None"
    address: str | None
    last_connection_request: str | None
    client_publish_pending: bool
    client_replacement_pending: bool
    disconnect_notified: bool
    connection_session_epoch: int


@dataclass(frozen=True, slots=True)
class _BLEClientAdoption:
    """Result of attempting to adopt one newly established BLE client."""

    previous_client: "BLEClient | None"
    device_address: str | None
    aborted_for_shutdown: bool
