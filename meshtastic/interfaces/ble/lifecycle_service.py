"""Lifecycle-oriented BLE facade exports and compatibility surface."""

from meshtastic.interfaces.ble.gating import _is_currently_connected_elsewhere
from meshtastic.interfaces.ble.lifecycle_compat_service import (
    BLELifecycleService,
    _ORIGINAL_GET_CONNECTED_CLIENT_STATUS,
    _ORIGINAL_GET_CONNECTED_CLIENT_STATUS_LOCKED,
)
from meshtastic.interfaces.ble.lifecycle_controller_runtime import BLELifecycleController
from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
    BLEDisconnectLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
    BLEConnectionOwnershipLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_primitives import (
    _DisconnectPlan,
    _LifecycleErrorAccess,
    _LifecycleStateAccess,
    _LifecycleThreadAccess,
    _OwnershipSnapshot,
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
    "_LifecycleStateAccess",
    "_LifecycleThreadAccess",
    "_LifecycleErrorAccess",
    "_DisconnectPlan",
    "_OwnershipSnapshot",
    "_ORIGINAL_GET_CONNECTED_CLIENT_STATUS",
    "_ORIGINAL_GET_CONNECTED_CLIENT_STATUS_LOCKED",
    "_is_currently_connected_elsewhere",
]
