"""Pure lifecycle decisions for BLE receive and disconnect orchestration."""

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.state import ConnectionState

if TYPE_CHECKING:
    from bleak import BleakClient as BleakRootClient

    from meshtastic.interfaces.ble.client import BLEClient


class _ReceiveStartDisposition(Enum):
    """Action selected after probing one receive-thread snapshot."""

    PROCEED = auto()
    DEFER_CURRENT = auto()
    DEFER_CURRENT_TIMEOUT = auto()
    SKIP_RUNNING = auto()
    REPLACE_STALE_PENDING = auto()
    REPLACE_STALE_PENDING_TIMEOUT = auto()
    WAIT_PENDING = auto()
    REPLACE_DEAD = auto()
    WAIT_INCONCLUSIVE = auto()
    CLEAR_FAILED_START = auto()


@dataclass(frozen=True, slots=True)
class _ReceiveStartSnapshot:
    """Receive-thread state captured under the session lock."""

    existing: ThreadLike | None
    start_pending: bool
    pending_since: float | None


@dataclass(frozen=True, slots=True)
class _ReceiveThreadProbe:
    """Lock-free observations about one thread-like collaborator."""

    ident: int | None
    is_alive: bool
    is_current: bool
    start_failure_confirmed: bool
    display_name: str


@dataclass(frozen=True, slots=True)
class _ReceiveStartDecision:
    """Pure decision derived from one stable receive snapshot and probe."""

    disposition: _ReceiveStartDisposition
    pending_age: float = 0.0
    initialize_pending_since: bool = False
    schedule_deferred_restart: bool = False


def _decide_receive_start(
    snapshot: _ReceiveStartSnapshot,
    probe: _ReceiveThreadProbe,
    *,
    now: float,
    pending_timeout: float,
) -> _ReceiveStartDecision:
    """Classify the next receive-start action without mutating shared state."""
    existing = snapshot.existing
    pending_since = snapshot.pending_since
    has_pending_since = isinstance(pending_since, (float, int))
    pending_age = now - float(pending_since) if has_pending_since else 0.0

    if existing is None:
        decision = _ReceiveStartDecision(_ReceiveStartDisposition.PROCEED)
    elif probe.is_current:
        if snapshot.start_pending and has_pending_since and pending_age >= pending_timeout:
            decision = _ReceiveStartDecision(
                _ReceiveStartDisposition.DEFER_CURRENT_TIMEOUT,
                pending_age=pending_age,
            )
        else:
            decision = _ReceiveStartDecision(
                _ReceiveStartDisposition.DEFER_CURRENT,
                pending_age=pending_age if snapshot.start_pending else 0.0,
                initialize_pending_since=not has_pending_since,
                schedule_deferred_restart=(
                    not has_pending_since or not snapshot.start_pending
                ),
            )
    elif probe.is_alive:
        decision = _ReceiveStartDecision(_ReceiveStartDisposition.SKIP_RUNNING)
    elif snapshot.start_pending:
        if probe.ident is not None or probe.start_failure_confirmed:
            decision = _ReceiveStartDecision(
                _ReceiveStartDisposition.REPLACE_STALE_PENDING
            )
        elif pending_age < pending_timeout:
            decision = _ReceiveStartDecision(
                _ReceiveStartDisposition.WAIT_PENDING,
                pending_age=pending_age,
                initialize_pending_since=not has_pending_since,
            )
        else:
            decision = _ReceiveStartDecision(
                _ReceiveStartDisposition.REPLACE_STALE_PENDING_TIMEOUT,
                pending_age=pending_age,
            )
    elif probe.ident is not None:
        decision = _ReceiveStartDecision(_ReceiveStartDisposition.REPLACE_DEAD)
    elif not probe.start_failure_confirmed:
        decision = _ReceiveStartDecision(
            _ReceiveStartDisposition.WAIT_INCONCLUSIVE,
            initialize_pending_since=not has_pending_since,
        )
    else:
        decision = _ReceiveStartDecision(_ReceiveStartDisposition.CLEAR_FAILED_START)
    return decision


class _DisconnectDisposition(Enum):
    """Ownership result for a disconnect signal."""

    ACCEPT = auto()
    IGNORE_CONNECTING = auto()
    IGNORE_SHUTDOWN = auto()
    IGNORE_STALE = auto()
    IGNORE_UNOWNED = auto()
    IGNORE_DUPLICATE = auto()


@dataclass(frozen=True, slots=True)
class _DisconnectOwnershipSnapshot:
    """Inputs needed to decide whether one disconnect owns the current session."""

    current_state: ConnectionState
    current_client: "BLEClient | None"
    target_client: "BLEClient | None"
    bleak_client: "BleakRootClient | None"
    is_closing: bool
    publish_pending: bool
    replacement_pending: bool
    disconnect_notified: bool


@dataclass(frozen=True, slots=True)
class _DisconnectOwnershipDecision:
    """Pure disconnect ownership decision with any resolved target client."""

    disposition: _DisconnectDisposition
    target_client: "BLEClient | None"


def _decide_disconnect_ownership(
    snapshot: _DisconnectOwnershipSnapshot,
) -> _DisconnectOwnershipDecision:
    """Resolve disconnect ownership without mutating interface/session state."""
    current_client = snapshot.current_client
    target_client = snapshot.target_client
    bleak_client = snapshot.bleak_client
    disposition = _DisconnectDisposition.ACCEPT

    if snapshot.current_state == ConnectionState.CONNECTING:
        disconnect_from_owned_client = current_client is not None and (
            target_client is current_client
            or (
                target_client is None
                and bleak_client is not None
                and getattr(current_client, "bleak_client", None) is bleak_client
            )
        )
        if not disconnect_from_owned_client:
            disposition = _DisconnectDisposition.IGNORE_CONNECTING

    if disposition is _DisconnectDisposition.ACCEPT and snapshot.is_closing:
        disposition = _DisconnectDisposition.IGNORE_SHUTDOWN

    if (
        disposition is _DisconnectDisposition.ACCEPT
        and target_client is None
        and bleak_client is not None
    ):
        if (
            current_client is not None
            and getattr(current_client, "bleak_client", None) is bleak_client
        ):
            target_client = current_client
        elif current_client is not None:
            disposition = _DisconnectDisposition.IGNORE_STALE

    if (
        disposition is _DisconnectDisposition.ACCEPT
        and current_client is None
        and not snapshot.publish_pending
        and not snapshot.replacement_pending
    ):
        disposition = _DisconnectDisposition.IGNORE_UNOWNED

    if (
        disposition is _DisconnectDisposition.ACCEPT
        and target_client is not None
        and current_client is not None
        and target_client is not current_client
    ):
        disposition = _DisconnectDisposition.IGNORE_STALE

    if (
        disposition is _DisconnectDisposition.ACCEPT
        and snapshot.disconnect_notified
    ):
        disposition = _DisconnectDisposition.IGNORE_DUPLICATE

    return _DisconnectOwnershipDecision(disposition, target_client)
