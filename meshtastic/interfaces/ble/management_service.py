"""Management BLE facade exports and compatibility surface."""

from meshtastic.interfaces.ble.management_compat_service import (
    BLEManagementCommandsService,
)
from meshtastic.interfaces.ble.management_runtime import (
    BLEManagementCommandHandler,
    BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS,
    TRUST_COMMAND_OUTPUT_MAX_CHARS,
    TRUST_HEX_BLOB_RE,
    TRUST_TOKEN_RE,
    _ManagementStartContext,
    _create_management_client,
    _is_blank_or_malformed_address_like,
)

__all__ = [
    "BLEManagementCommandHandler",
    "BLEManagementCommandsService",
    "BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS",
    "TRUST_COMMAND_OUTPUT_MAX_CHARS",
    "TRUST_HEX_BLOB_RE",
    "TRUST_TOKEN_RE",
    "_ManagementStartContext",
    "_create_management_client",
    "_is_blank_or_malformed_address_like",
]
