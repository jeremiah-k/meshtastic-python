"""Narrow internal protocols for BLE runtime collaborators."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Protocol

from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.state import ConnectionState

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient


class _LockPort(Protocol):
    """Context-manager contract required from the BLE lifecycle lock."""

    def __enter__(self) -> object:
        """Acquire the lock and return its context value."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Release the lock context."""


class _BLEStateManagerPort(Protocol):
    """Connection-state operations required by lifecycle collaborators."""

    @property
    def current_state(self) -> ConnectionState:
        """Return the current connection state."""

    @property
    def is_connected(self) -> bool:
        """Return whether the state is connected."""

    @property
    def is_closing(self) -> bool:
        """Return whether the state is disconnecting."""

    def transition_to(self, new_state: ConnectionState) -> bool:
        """Attempt a validated state transition."""

    def reset_to_disconnected(self) -> bool:
        """Force the state machine back to disconnected."""


class _BLESessionStatePort(Protocol):  # pylint: disable=too-many-instance-attributes
    """Mutable lifecycle state required by BLE coordinators."""

    lock: _LockPort
    closed: bool
    disconnect_notified: bool
    client_publish_pending: bool
    connected_publish_inflight_client: BLEClient | None
    client_replacement_pending: bool
    last_disconnect_source: str | None
    connection_alias_key: str | None
    prior_publish_was_reconnect: bool
    last_connect_pair_override: bool | None
    last_connect_timeout_override: float | None
    publishing_thread_override: object | None
    ever_connected: bool
    connection_session_epoch: int
    receive_recovery_attempts: int
    last_recovery_time: float
    read_retry_count: int
    last_empty_read_warning: float
    suppressed_empty_read_warnings: int
    want_receive: bool
    receive_start_pending: bool
    receive_start_pending_since: float | None
    receive_thread: ThreadLike | None

    def reset_read_retry_count(self) -> None:
        """Reset only the transient read retry counter."""

    def reset_receive_retry_state(self) -> None:
        """Reset transient read retry and warning counters."""

    def reset_recovery_state(self) -> None:
        """Reset receive-recovery attempt bookkeeping."""
