"""Lifecycle-oriented BLE facade exports and compatibility surface."""

from meshtastic.interfaces.ble.gating import (  # noqa: F401  # COMPAT_STABLE_SHIM: module-level monkeypatch target
    _is_currently_connected_elsewhere,
)
from meshtastic.interfaces.ble.lifecycle_compat_service import (  # noqa: F401  # COMPAT_STABLE_SHIM: runtime monkeypatch detection baseline
    _ORIGINAL_GET_CONNECTED_CLIENT_STATUS,
    _ORIGINAL_GET_CONNECTED_CLIENT_STATUS_LOCKED,
    BLELifecycleService,
)
from meshtastic.interfaces.ble.lifecycle_controller_runtime import (
    BLELifecycleController,
)
from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
    BLEDisconnectLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
    BLEConnectionOwnershipLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
    BLEReceiveLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_shutdown_runtime import (
    BLEShutdownLifecycleCoordinator,
)

__all__ = [
    "BLELifecycleController",
    "BLELifecycleService",
    "BLEReceiveLifecycleCoordinator",
    "BLEDisconnectLifecycleCoordinator",
    "BLEConnectionOwnershipLifecycleCoordinator",
    "BLEShutdownLifecycleCoordinator",
]
