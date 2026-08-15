"""Typed callback bundles for BLE lifecycle compatibility wiring."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.lifecycle_primitives import _OwnershipSnapshot
from meshtastic.interfaces.ble.state import ConnectionState

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient


@dataclass(frozen=True, slots=True)
class _LifecycleStateCallbacks:
    """State-manager callbacks consumed by lifecycle coordinators."""

    current_state_getter: Callable[[], ConnectionState]
    is_connected_getter: Callable[[], bool]
    is_closing_getter: Callable[[], bool]
    transition_to_state: Callable[[ConnectionState], bool]
    reset_to_disconnected: Callable[[], bool]

    def transition_to_disconnected(self) -> bool:
        """Transition the bound lifecycle state to ``DISCONNECTED``."""
        return self.transition_to_state(ConnectionState.DISCONNECTED)


@dataclass(frozen=True, slots=True)
class _LifecycleThreadCallbacks:
    """Thread-coordinator callbacks consumed by lifecycle coordinators."""

    create_thread: Callable[..., ThreadLike]
    start_thread: Callable[[object], None]
    join_thread: Callable[[object, float | None], None]
    clear_events: Callable[..., None]
    wake_waiting_threads: Callable[..., None]


@dataclass(frozen=True, slots=True)
class _LifecycleErrorCallbacks:
    """Error-policy callbacks consumed by lifecycle coordinators."""

    safe_cleanup: Callable[[Callable[[], object], str], None]
    safe_execute: Callable[[Callable[[], object], str], object | None]


@dataclass(frozen=True, slots=True)
class _LifecycleOwnershipCallbacks:
    """Connected-client ownership callbacks used by compatibility shims."""

    get_connected_client_status: Callable[["BLEClient"], tuple[bool, bool]]
    get_connected_client_status_locked: Callable[["BLEClient"], tuple[bool, bool]]
    verify_ownership_snapshot: Callable[
        ["BLEClient", str | None, str | None], _OwnershipSnapshot
    ]


@dataclass(frozen=True, slots=True)
class _DisconnectCallbackBundle:
    """Canonical callback wiring for disconnect compatibility entrypoints."""

    is_closing_getter: Callable[[], bool]
    current_state_getter: Callable[[], ConnectionState]
    transition_to_disconnected: Callable[[], bool]
    reset_to_disconnected: Callable[[], bool]
    close_previous_client_async: Callable[["BLEClient | None"], None]
    clear_events: Callable[..., None]
