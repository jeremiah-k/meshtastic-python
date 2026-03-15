"""Lifecycle-oriented helpers for BLE interface orchestration."""

import atexit
import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from bleak import BleakClient as BleakRootClient

from meshtastic.interfaces.ble.constants import (
    CONNECTION_ERROR_LOST_OWNERSHIP,
    ERROR_INTERFACE_CLOSING,
    NOTIFICATION_START_TIMEOUT,
    RECEIVE_THREAD_JOIN_TIMEOUT,
    logger,
)
from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.gating import _addr_key, _is_currently_connected_elsewhere
from meshtastic.interfaces.ble.state import ConnectionState
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_callable,
    _is_unconfigured_mock_member,
    _thread_start_probe,
    sanitize_address,
)
from meshtastic.mesh_interface import MeshInterface

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient
    from meshtastic.interfaces.ble.interface import BLEInterface

READ_TRIGGER_EVENT = "read_trigger"
RECONNECTED_EVENT = "reconnected_event"


@dataclass(frozen=True)
class _DisconnectPlan:
    """Resolved disconnect handling plan from state-locked prechecks."""

    early_return: bool | None
    previous_client: "BLEClient | None" = None
    client_at_start: "BLEClient | None" = None
    address: str = "unknown"
    disconnect_keys: tuple[str, ...] = ()
    should_reconnect: bool = False
    should_schedule_reconnect: bool = False
    was_publish_pending: bool = False
    was_replacement_pending: bool = False


@dataclass(frozen=True)
class _OwnershipSnapshot:
    """Snapshot of connect-result ownership and shutdown/gate status."""

    still_owned: bool
    is_closing: bool
    lost_gate_ownership: bool
    prior_ever_connected: bool


class BLELifecycleService:
    """Service helpers for BLEInterface lifecycle responsibilities."""

    @staticmethod
    def _set_receive_wanted(iface: "BLEInterface", *, want_receive: bool) -> None:
        """Request or clear the background receive loop."""
        with iface._state_lock:
            iface._want_receive = want_receive

    @staticmethod
    def _should_run_receive_loop(iface: "BLEInterface") -> bool:
        """Return whether the receive loop should run."""
        with iface._state_lock:
            return iface._want_receive and not iface._closed

    @staticmethod
    def _thread_create_thread(
        iface: "BLEInterface",
        *,
        target: Callable[..., object],
        name: str,
        daemon: bool,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
    ) -> ThreadLike:
        """Create thread using public-first coordinator dispatch."""
        create_thread = getattr(iface.thread_coordinator, "create_thread", None)
        if callable(create_thread) and not _is_unconfigured_mock_callable(create_thread):
            return cast(
                ThreadLike,
                create_thread(
                    target=target,
                    name=name,
                    daemon=daemon,
                    args=args,
                    kwargs=kwargs,
                ),
            )
        legacy_create_thread = getattr(iface.thread_coordinator, "_create_thread", None)
        if callable(legacy_create_thread) and not _is_unconfigured_mock_callable(
            legacy_create_thread
        ):
            return cast(
                ThreadLike,
                legacy_create_thread(
                    target=target,
                    name=name,
                    daemon=daemon,
                    args=args,
                    kwargs=kwargs,
                ),
            )
        raise AttributeError("Thread coordinator is missing create_thread/_create_thread")

    @staticmethod
    def _thread_start_thread(iface: "BLEInterface", thread: object) -> None:
        """Start thread using public-first coordinator dispatch."""
        start_thread = getattr(iface.thread_coordinator, "start_thread", None)
        if callable(start_thread) and not _is_unconfigured_mock_callable(start_thread):
            start_thread(thread)
            return
        legacy_start_thread = getattr(iface.thread_coordinator, "_start_thread", None)
        if callable(legacy_start_thread) and not _is_unconfigured_mock_callable(
            legacy_start_thread
        ):
            legacy_start_thread(thread)
            return
        raise AttributeError("Thread coordinator is missing start_thread/_start_thread")

    @staticmethod
    def _thread_join_thread(
        iface: "BLEInterface", thread: object, *, timeout: float | None
    ) -> None:
        """Join thread using public-first coordinator dispatch."""
        join_thread = getattr(iface.thread_coordinator, "join_thread", None)
        if callable(join_thread) and not _is_unconfigured_mock_callable(join_thread):
            join_thread(thread, timeout=timeout)
            return
        legacy_join_thread = getattr(iface.thread_coordinator, "_join_thread", None)
        if callable(legacy_join_thread) and not _is_unconfigured_mock_callable(
            legacy_join_thread
        ):
            legacy_join_thread(thread, timeout=timeout)
            return
        logger.debug("Thread coordinator is missing join_thread/_join_thread")

    @staticmethod
    def _thread_set_event(iface: "BLEInterface", event_name: str) -> None:
        """Set event using public-first coordinator dispatch."""
        set_event = getattr(iface.thread_coordinator, "set_event", None)
        if callable(set_event) and not _is_unconfigured_mock_callable(set_event):
            set_event(event_name)
            return
        legacy_set_event = getattr(iface.thread_coordinator, "_set_event", None)
        if callable(legacy_set_event) and not _is_unconfigured_mock_callable(
            legacy_set_event
        ):
            legacy_set_event(event_name)
            return
        logger.debug("Thread coordinator is missing set_event/_set_event")

    @staticmethod
    def _thread_clear_events(iface: "BLEInterface", *event_names: str) -> None:
        """Clear events using public-first coordinator dispatch."""
        clear_events = getattr(iface.thread_coordinator, "clear_events", None)
        if callable(clear_events) and not _is_unconfigured_mock_callable(clear_events):
            clear_events(*event_names)
            return
        legacy_clear_events = getattr(iface.thread_coordinator, "_clear_events", None)
        if callable(legacy_clear_events) and not _is_unconfigured_mock_callable(
            legacy_clear_events
        ):
            legacy_clear_events(*event_names)
            return
        logger.debug("Thread coordinator is missing clear_events/_clear_events")

    @staticmethod
    def _thread_wake_waiting_threads(
        iface: "BLEInterface", *event_names: str
    ) -> None:
        """Wake waiting threads using public-first coordinator dispatch."""
        wake_waiting_threads = getattr(iface.thread_coordinator, "wake_waiting_threads", None)
        if callable(wake_waiting_threads) and not _is_unconfigured_mock_callable(
            wake_waiting_threads
        ):
            wake_waiting_threads(*event_names)
            return
        legacy_wake_waiting_threads = getattr(
            iface.thread_coordinator, "_wake_waiting_threads", None
        )
        if callable(legacy_wake_waiting_threads) and not _is_unconfigured_mock_callable(
            legacy_wake_waiting_threads
        ):
            legacy_wake_waiting_threads(*event_names)
            return
        logger.debug(
            "Thread coordinator is missing wake_waiting_threads/_wake_waiting_threads"
        )

    @staticmethod
    def _start_receive_thread(
        iface: "BLEInterface", *, name: str, reset_recovery: bool = True
    ) -> None:
        """Create and start the background receive thread."""
        with iface._state_lock:
            # Avoid reviving the receive loop after shutdown has begun.
            if iface._closed or not iface._want_receive:
                logger.debug(
                    "Skipping receive thread start (%s): interface is closing/stopped.",
                    name,
                )
                return
            # Prevent duplicate live receive threads except when the current thread
            # is replacing itself after an unexpected failure.
            existing = iface._receiveThread
            existing_ident, existing_is_alive = (
                _thread_start_probe(existing) if existing is not None else (None, False)
            )
            if (
                existing is not None
                and existing is not threading.current_thread()
                and (existing_is_alive or existing_ident is None)
            ):
                logger.debug(
                    "Skipping receive thread start (%s): %s is already running or pending start.",
                    name,
                    existing.name,
                )
                return
            thread = BLELifecycleService._thread_create_thread(
                iface,
                target=iface._receive_from_radio_impl,
                name=name,
                daemon=True,
            )
            iface._receiveThread = thread
        try:
            BLELifecycleService._thread_start_thread(iface, thread)
        except Exception:  # noqa: BLE001 - start failure must clear stale thread reference
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receiveThread = None
            raise
        thread_ident, thread_is_alive = _thread_start_probe(thread)
        if thread_ident is None and not thread_is_alive:
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receiveThread = None
            logger.debug(
                "Receive thread %s did not start; cleared stale thread reference.",
                name,
            )
            return
        if reset_recovery:
            # Reset recovery throttling on successful thread start (not during recovery).
            with iface._state_lock:
                iface._receive_recovery_attempts = 0

    @staticmethod
    def _on_ble_disconnect(iface: "BLEInterface", client: BleakRootClient) -> None:
        """Handle Bleak client disconnect callback."""
        iface._handle_disconnect("bleak_callback", bleak_client=client)

    @staticmethod
    def _schedule_auto_reconnect(iface: "BLEInterface") -> None:
        """Schedule background auto-reconnect attempts when enabled."""
        if not iface.auto_reconnect:
            return
        with iface._state_lock:
            if iface._closed:
                logger.debug(
                    "Skipping auto-reconnect scheduling because interface is closed."
                )
                return
            if iface._state_manager._is_closing:
                logger.debug(
                    "Skipping auto-reconnect scheduling because interface is closing."
                )
                return
            # Keep clear() under the state lock so reconnect scheduling starts a
            # fresh cycle against a non-signaled shutdown event.
            iface._shutdown_event.clear()
        iface._reconnect_scheduler._schedule_reconnect(
            iface.auto_reconnect, iface._shutdown_event
        )

    @staticmethod
    def _disconnect_and_close_client(
        iface: "BLEInterface", client: "BLEClient"
    ) -> None:
        """Ensure BLE client resources are released."""
        iface._client_manager_safe_close_client(client)

    @staticmethod
    def _compute_disconnect_keys(
        iface: "BLEInterface",
        *,
        previous_client: "BLEClient | None",
        alias_key: str | None,
        should_reconnect: bool,
        address: str,
    ) -> tuple[list[str], bool]:
        """Compute disconnect registry keys and reconnect scheduling intent."""
        should_schedule_reconnect = should_reconnect and not iface._closed
        if should_reconnect:
            if previous_client is not None:
                previous_address = getattr(previous_client, "address", iface.address)
                device_key = _addr_key(previous_address) if previous_address else None
                return iface._sorted_address_keys(device_key, alias_key), should_schedule_reconnect
            fallback_key = _addr_key(iface.address)
            return iface._sorted_address_keys(fallback_key, alias_key), should_schedule_reconnect

        # Guard against sentinel "unknown" address; prefer previous active
        # address because callback metadata can be stale.
        address_for_registry = (
            getattr(previous_client, "address", None)
            if previous_client is not None
            else (address if address != "unknown" else iface.address)
        )
        addr_disconnect_key = _addr_key(address_for_registry)
        return iface._sorted_address_keys(addr_disconnect_key, alias_key), should_schedule_reconnect

    @staticmethod
    def _resolve_disconnect_target(
        iface: "BLEInterface",
        source: str,
        client: "BLEClient | None",
        bleak_client: BleakRootClient | None,
    ) -> _DisconnectPlan:
        """Resolve disconnect ownership, mutate state, and build side-effect plan."""
        target_client = client
        with iface._state_lock:
            current_state = iface._state_manager._current_state
            current_client = iface.client
            is_closing = iface._state_manager._is_closing or iface._closed
            was_publish_pending = iface._client_publish_pending
            was_replacement_pending = iface._client_replacement_pending

            if current_state == ConnectionState.CONNECTING:
                logger.debug(
                    "Ignoring disconnect from %s while a connection is in progress.",
                    source,
                )
                return _DisconnectPlan(early_return=True)
            if is_closing:
                logger.debug("Ignoring disconnect from %s during shutdown.", source)
                return _DisconnectPlan(early_return=False)

            # Resolve callback source against the currently active client.
            if target_client is None and bleak_client is not None:
                if (
                    current_client is not None
                    and getattr(current_client, "bleak_client", None) is bleak_client
                ):
                    target_client = current_client
                elif current_client is not None:
                    logger.debug("Ignoring stale disconnect from %s.", source)
                    return _DisconnectPlan(early_return=True)

            # Ignore stale disconnect callbacks from non-active clients.
            if (
                target_client is not None
                and current_client is not None
                and target_client is not current_client
            ):
                logger.debug("Ignoring stale disconnect from %s.", source)
                return _DisconnectPlan(early_return=True)

            # Prevent duplicate disconnect notifications.
            if iface._disconnect_notified:
                logger.debug("Ignoring duplicate disconnect from %s.", source)
                return _DisconnectPlan(early_return=True)

            previous_client = current_client
            client_at_start = current_client
            alias_key = iface._connection_alias_key
            iface.client = None
            iface._client_publish_pending = False
            iface._client_replacement_pending = False
            iface._disconnect_notified = True
            iface._connection_alias_key = None
            iface._state_manager._transition_to(ConnectionState.DISCONNECTED)
            should_reconnect = iface.auto_reconnect

            address = "unknown"
            if target_client is not None:
                address = getattr(target_client, "address", repr(target_client))
            elif bleak_client is not None:
                address = getattr(bleak_client, "address", repr(bleak_client))
            elif previous_client is not None:
                address = getattr(previous_client, "address", repr(previous_client))

            disconnect_keys, should_schedule_reconnect = (
                BLELifecycleService._compute_disconnect_keys(
                    iface,
                    previous_client=previous_client,
                    alias_key=alias_key,
                    should_reconnect=should_reconnect,
                    address=address,
                )
            )
            return _DisconnectPlan(
                early_return=None,
                previous_client=previous_client,
                client_at_start=client_at_start,
                address=address,
                disconnect_keys=tuple(disconnect_keys),
                should_reconnect=should_reconnect,
                should_schedule_reconnect=should_schedule_reconnect,
                was_publish_pending=was_publish_pending,
                was_replacement_pending=was_replacement_pending,
            )

    @staticmethod
    def _close_previous_client_async(
        iface: "BLEInterface", previous_client: "BLEClient | None"
    ) -> None:
        """Close previous client asynchronously, with inline fallback on start failure."""
        if previous_client is None:
            return
        try:
            close_thread = BLELifecycleService._thread_create_thread(
                iface,
                target=iface._client_manager_safe_close_client,
                args=(previous_client,),
                name="BLEClientClose",
                daemon=True,
            )
            BLELifecycleService._thread_start_thread(iface, close_thread)
            thread_ident, thread_is_alive = _thread_start_probe(close_thread)
            if thread_ident is None and not thread_is_alive:
                logger.warning(
                    "BLE client close thread did not start; closing inline.",
                    exc_info=False,
                )
                iface._client_manager_safe_close_client(previous_client)
        except Exception:  # noqa: BLE001 - cleanup must not abort disconnect flow
            logger.warning(
                "Failed to start async BLE client close; closing inline.",
                exc_info=True,
            )
            iface._client_manager_safe_close_client(previous_client)

    @staticmethod
    def _execute_disconnect_side_effects(
        iface: "BLEInterface", *, plan: _DisconnectPlan, source: str
    ) -> bool:
        """Execute disconnect side effects outside ``_disconnect_lock``."""
        disconnect_keys = list(plan.disconnect_keys)
        skip_side_effects = False
        stale_disconnect_keys: list[str] = []
        with iface._state_lock:
            active_client = iface.client
            if active_client is not None and active_client is not plan.client_at_start:
                active_keys = set(
                    iface._sorted_address_keys(
                        _addr_key(getattr(active_client, "address", None)),
                        iface._connection_alias_key,
                    )
                )
                stale_disconnect_keys = [
                    key for key in disconnect_keys if key not in active_keys
                ]
                skip_side_effects = True

        if skip_side_effects:
            if stale_disconnect_keys:
                iface._mark_address_keys_disconnected(*stale_disconnect_keys)
            BLELifecycleService._close_previous_client_async(iface, plan.previous_client)
            logger.debug(
                "Skipping stale disconnect side-effects from %s: newer client already active.",
                source,
            )
            return True

        logger.debug("BLE client %s disconnected (source: %s).", plan.address, source)
        # Expose the most recent disconnect source for external listeners that
        # only receive the generic meshtastic.connection.lost event.
        iface._last_disconnect_source = f"ble.{source}"

        if disconnect_keys:
            iface._mark_address_keys_disconnected(*disconnect_keys)

        BLELifecycleService._close_previous_client_async(iface, plan.previous_client)
        if not plan.was_publish_pending or plan.was_replacement_pending:
            iface._disconnected()
        else:
            logger.debug(
                "Skipping public disconnect event for provisional session from %s.",
                source,
            )

        if plan.should_reconnect:
            if plan.should_schedule_reconnect:
                BLELifecycleService._thread_clear_events(
                    iface, READ_TRIGGER_EVENT, RECONNECTED_EVENT
                )
                iface._schedule_auto_reconnect()
            return True

        logger.debug("Auto-reconnect disabled, staying disconnected.")
        return False

    @staticmethod
    def _handle_disconnect(
        iface: "BLEInterface",
        source: str,
        client: "BLEClient | None" = None,
        bleak_client: BleakRootClient | None = None,
    ) -> bool:
        """Handle a BLE client disconnection and drive reconnect/shutdown orchestration."""
        if not iface._disconnect_lock.acquire(blocking=False):
            # Another disconnect handler is active; mirror current reconnect
            # intent so callers can stop when reconnect is not expected.
            logger.debug(
                "Disconnect from %s skipped: another disconnect handler is active.",
                source,
            )
            with iface._state_lock:
                return (
                    iface.auto_reconnect
                    and not iface._closed
                    and not iface._state_manager._is_closing
                )

        disconnect_lock_released = False
        plan = _DisconnectPlan(early_return=False)
        try:
            # Lock-order invariant: release _disconnect_lock before any
            # operations that compute/acquire address locks.
            plan = BLELifecycleService._resolve_disconnect_target(
                iface,
                source,
                client,
                bleak_client,
            )
            if plan.early_return is not None:
                return plan.early_return

            # Release the disconnect lock before any address-lock operations.
            iface._disconnect_lock.release()
            disconnect_lock_released = True
        finally:
            if not disconnect_lock_released:
                iface._disconnect_lock.release()

        return BLELifecycleService._execute_disconnect_side_effects(
            iface,
            plan=plan,
            source=source,
        )

    @staticmethod
    def _emit_verified_connection_side_effects(
        iface: "BLEInterface", connected_client: "BLEClient"
    ) -> None:
        """Emit reconnect signaling/logging only after verified connect publish."""
        coordinator = getattr(iface, "thread_coordinator", None)
        if iface._prior_publish_was_reconnect and coordinator is not None:
            BLELifecycleService._thread_set_event(iface, RECONNECTED_EVENT)
        iface._prior_publish_was_reconnect = False
        normalized_device_address = sanitize_address(
            iface._extract_client_address(connected_client)
        )
        logger.info(
            "Connection successful to %s",
            normalized_device_address or "unknown",
        )

    @staticmethod
    def _discard_invalidated_connected_client(
        iface: "BLEInterface",
        client: "BLEClient",
        *,
        restore_address: str | None = None,
        restore_last_connection_request: str | None = None,
    ) -> None:
        """Best-effort cleanup for a connected client invalidated before return."""
        restored_address = (
            restore_address.strip()
            if restore_address is not None and restore_address.strip()
            else None
        )
        should_reset_state = False
        is_closing = False
        with iface._state_lock:
            if iface.client is client:
                is_closing = iface._state_manager._is_closing or iface._closed
                iface.client = None
                iface._client_publish_pending = False
                iface._client_replacement_pending = False
                # The disconnect callback remains registered on `client` until
                # best-effort close completes, so mark this interface as already
                # notified before close() can trigger a stale callback.
                iface._disconnect_notified = True
                if not is_closing:
                    iface.address = restored_address
                    iface._last_connection_request = restore_last_connection_request
                    iface._connection_alias_key = None
                    should_reset_state = True
                else:
                    iface._last_connection_request = None
            elif iface.client is None and iface._client_publish_pending:
                # A provisional client can be detached by a concurrent
                # disconnect path before this cleanup runs; ensure callers do
                # not remain stuck in ERROR_MANAGEMENT_CONNECTING.
                iface._client_publish_pending = False
                iface._client_replacement_pending = False
                is_closing = iface._state_manager._is_closing or iface._closed
                if not is_closing:
                    iface.address = restored_address
                    iface._last_connection_request = restore_last_connection_request
                    iface._connection_alias_key = None
                    should_reset_state = True
                else:
                    iface._last_connection_request = None

        iface._client_manager_safe_close_client(client)

        if should_reset_state:
            with iface._state_lock:
                iface._state_manager._reset_to_disconnected()

    @staticmethod
    def _state_manager_is_connected(iface: "BLEInterface") -> bool:
        """Return connected-state flag from public-first state-manager members."""
        public_is_connected = getattr(iface._state_manager, "is_connected", None)
        if not _is_unconfigured_mock_member(public_is_connected) and isinstance(
            public_is_connected, bool
        ):
            return public_is_connected
        legacy_is_connected = getattr(iface._state_manager, "_is_connected", None)
        if not _is_unconfigured_mock_member(legacy_is_connected) and isinstance(
            legacy_is_connected, bool
        ):
            return legacy_is_connected
        return False

    @staticmethod
    def _client_is_connected(client: "BLEClient") -> bool:
        """Return connected-state flag from public/legacy BLEClient members."""
        for candidate_name in ("isConnected", "is_connected", "_is_connected"):
            candidate = getattr(client, candidate_name, None)
            if callable(candidate):
                if _is_unconfigured_mock_callable(candidate):
                    continue
                connected = candidate()
                if isinstance(connected, bool):
                    return connected
                continue
            if isinstance(candidate, bool) and not _is_unconfigured_mock_member(candidate):
                return candidate
        return False

    @staticmethod
    def _get_connected_client_status_locked(
        iface: "BLEInterface", client: "BLEClient"
    ) -> tuple[bool, bool]:
        """Return owned/closing status for a connected client while holding state lock."""
        is_closing = iface._state_manager._is_closing or iface._closed
        state_connected = BLELifecycleService._state_manager_is_connected(iface)
        client_connected = BLELifecycleService._client_is_connected(client)
        is_owned = (
            not iface._closed
            and iface.client is client
            and state_connected
            and client_connected
        )
        return is_owned, is_closing

    @staticmethod
    def _get_connected_client_status(
        iface: "BLEInterface", client: "BLEClient"
    ) -> tuple[bool, bool]:
        """Return whether interface owns `client` and whether shutdown has started."""
        with iface._state_lock:
            return BLELifecycleService._get_connected_client_status_locked(iface, client)

    @staticmethod
    def _has_lost_gate_ownership(iface: "BLEInterface", *keys: str | None) -> bool:
        """Return whether any connected address key is now owned elsewhere."""
        return any(
            key is not None and _is_currently_connected_elsewhere(key, owner=iface)
            for key in keys
        )

    @staticmethod
    def _raise_for_invalidated_connect_result(
        iface: "BLEInterface",
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        is_closing: bool,
        lost_gate_ownership: bool,
        restore_address: str | None,
        restore_last_connection_request: str | None,
    ) -> None:
        """Clean up a stale connect result and raise the appropriate BLEError."""
        stale_keys = iface._sorted_address_keys(
            connected_device_key,
            connection_alias_key,
        )
        if not lost_gate_ownership:
            with iface._state_lock:
                active_client = iface.client
                active_keys = set(
                    iface._sorted_address_keys(
                        _addr_key(iface._extract_client_address(active_client)),
                        iface._connection_alias_key,
                    )
                )
            stale_keys = [key for key in stale_keys if key not in active_keys]
        if stale_keys:
            iface._mark_address_keys_disconnected(*stale_keys)
        BLELifecycleService._discard_invalidated_connected_client(
            iface,
            connected_client,
            restore_address=restore_address,
            restore_last_connection_request=restore_last_connection_request,
        )
        if is_closing:
            raise iface.BLEError(ERROR_INTERFACE_CLOSING)
        raise iface.BLEError(CONNECTION_ERROR_LOST_OWNERSHIP)

    @staticmethod
    def _verify_ownership_snapshot(
        iface: "BLEInterface",
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
    ) -> _OwnershipSnapshot:
        """Return a single ownership snapshot for connect-result verification."""
        lost_gate_ownership = iface._has_lost_gate_ownership(
            connected_device_key,
            connection_alias_key,
        )
        with iface._state_lock:
            still_owned, is_closing = iface._get_connected_client_status_locked(
                connected_client
            )
            prior_ever_connected = iface._ever_connected
        return _OwnershipSnapshot(
            still_owned=still_owned,
            is_closing=is_closing,
            lost_gate_ownership=lost_gate_ownership,
            prior_ever_connected=prior_ever_connected,
        )

    @staticmethod
    def _verify_and_publish_connected(
        iface: "BLEInterface",
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        restore_address: str | None,
        restore_last_connection_request: str | None,
    ) -> None:
        """Publish connected state only when ownership is still valid."""
        def _raise_invalidated(snapshot: _OwnershipSnapshot) -> None:
            iface._raise_for_invalidated_connect_result(
                connected_client,
                connected_device_key,
                connection_alias_key,
                is_closing=snapshot.is_closing,
                lost_gate_ownership=snapshot.lost_gate_ownership,
                restore_address=restore_address,
                restore_last_connection_request=restore_last_connection_request,
            )

        snapshot = BLELifecycleService._verify_ownership_snapshot(
            iface,
            connected_client,
            connected_device_key,
            connection_alias_key,
        )
        if (
            not snapshot.still_owned
            or snapshot.is_closing
            or snapshot.lost_gate_ownership
        ):
            _raise_invalidated(snapshot)
        prior_ever_connected = snapshot.prior_ever_connected

        should_publish_connected = False
        with iface._state_lock:
            still_owned, is_closing = iface._get_connected_client_status_locked(
                connected_client
            )
            if still_owned and not is_closing:
                if not iface._client_publish_pending:
                    # Some direct test harnesses stub connection setup
                    # without setting provisional publish state.
                    iface._client_publish_pending = True
                    iface._client_replacement_pending = False
                should_publish_connected = True
        snapshot = BLELifecycleService._verify_ownership_snapshot(
            iface,
            connected_client,
            connected_device_key,
            connection_alias_key,
        )
        publish_committed = False
        if should_publish_connected:
            with iface._state_lock:
                still_owned, is_closing = iface._get_connected_client_status_locked(
                    connected_client
                )
                if (
                    snapshot.still_owned
                    and not snapshot.is_closing
                    and not snapshot.lost_gate_ownership
                    and still_owned
                    and not is_closing
                ):
                    iface._ever_connected = True
                    iface._prior_publish_was_reconnect = prior_ever_connected
                    publish_committed = True
                if iface.client is connected_client:
                    iface._client_publish_pending = False
                    iface._client_replacement_pending = False
            if publish_committed:
                iface._connected()
                iface._emit_verified_connection_side_effects(connected_client)
                return

        _raise_invalidated(snapshot)

    @staticmethod
    def _cleanup_thread_coordinator(iface: "BLEInterface") -> None:
        """Run thread coordinator cleanup using public-first compatibility dispatch."""
        cleanup = getattr(iface.thread_coordinator, "cleanup", None)
        if callable(cleanup) and not _is_unconfigured_mock_callable(cleanup):
            try:
                cleanup()
            except Exception:  # noqa: BLE001 - shutdown cleanup is best effort
                logger.debug("Error running thread coordinator cleanup()", exc_info=True)
            return

        legacy_cleanup = getattr(iface.thread_coordinator, "_cleanup", None)
        if callable(legacy_cleanup) and not _is_unconfigured_mock_callable(legacy_cleanup):
            try:
                legacy_cleanup()
            except Exception:  # noqa: BLE001 - shutdown cleanup is best effort
                logger.debug("Error running thread coordinator _cleanup()", exc_info=True)
            return

        logger.debug("Thread coordinator is missing cleanup/_cleanup")

    @staticmethod
    def _close(
        iface: "BLEInterface",
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
    ) -> None:
        """Shut down the BLE interface and release associated resources."""
        # Deliberately avoid _connect_lock here so close() can mark shutdown
        # immediately and in-flight connect/pair timeouts can observe it.
        management_wait_timed_out = BLELifecycleService._await_management_shutdown(
            iface,
            management_shutdown_wait_timeout=management_shutdown_wait_timeout,
            management_wait_poll_seconds=management_wait_poll_seconds,
        )
        if management_wait_timed_out is None:
            return

        try:
            if iface._shutdown_event is not None:
                iface._shutdown_event.set()
            BLELifecycleService._shutdown_discovery(iface)
            BLELifecycleService._shutdown_receive_thread(iface)
            BLELifecycleService._close_mesh_interface(iface)
            BLELifecycleService._unregister_exit_handler(iface)
            BLELifecycleService._shutdown_client(
                iface, management_wait_timed_out=management_wait_timed_out
            )
        finally:
            try:
                BLELifecycleService._cleanup_thread_coordinator(iface)
            finally:
                BLELifecycleService._finalize_close_state(iface)

    @staticmethod
    def _finalize_connection_gates(
        iface: "BLEInterface",
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
    ) -> None:
        """Finalize post-connection gating and cleanup stale claims."""
        still_active, is_closing = iface._get_connected_client_status(connected_client)

        if still_active:
            iface._mark_address_keys_connected(
                connected_device_key, connection_alias_key
            )
            needs_cleanup = False
            with iface._state_lock:
                still_active, is_closing = iface._get_connected_client_status_locked(
                    connected_client
                )
                if still_active:
                    iface._connection_alias_key = connection_alias_key
                else:
                    if is_closing:
                        logger.debug(
                            "Interface closed during connect(), cleaning up gate claim for %s",
                            getattr(connected_client, "address", "unknown"),
                        )
                    else:
                        logger.debug(
                            "Interface lost ownership during connect(), cleaning up gate claim for %s",
                            getattr(connected_client, "address", "unknown"),
                        )
                    iface._connection_alias_key = None
                    needs_cleanup = True
            if needs_cleanup:
                iface._mark_address_keys_disconnected(
                    connected_device_key, connection_alias_key
                )
        elif is_closing:
            logger.debug(
                "Skipping connect gate marking during shutdown for stale client result (%s).",
                getattr(connected_client, "address", "unknown"),
            )
        else:
            logger.debug(
                "Skipping connect gate marking for client result that lost ownership (%s).",
                getattr(connected_client, "address", "unknown"),
            )

    @staticmethod
    def _is_owned_connected_client(
        iface: "BLEInterface", client: "BLEClient"
    ) -> bool:
        """Return whether the interface still owns the provided connected client."""
        is_owned, _ = iface._get_connected_client_status(client)
        return is_owned

    @staticmethod
    def _await_management_shutdown(
        iface: "BLEInterface",
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
    ) -> bool | None:
        """Mark interface as closed and wait for inflight management operations."""
        management_wait_timed_out = False
        management_wait_started = time.monotonic()
        with iface._management_lock:
            with iface._state_lock:
                if iface._closed:
                    logger.debug(
                        "BLEInterface.close called on already closed interface; ignoring"
                    )
                    return None
                was_closing = iface._state_manager._is_closing
                iface._closed = True
                if was_closing:
                    logger.debug(
                        "BLEInterface.close called while another shutdown is in progress; continuing with cleanup"
                    )
                if iface._state_manager._current_state not in (
                    ConnectionState.DISCONNECTED,
                    ConnectionState.DISCONNECTING,
                ):
                    iface._state_manager._transition_to(ConnectionState.DISCONNECTING)
            while iface._management_inflight > 0:
                elapsed = time.monotonic() - management_wait_started
                if elapsed >= management_shutdown_wait_timeout:
                    management_wait_timed_out = True
                    logger.warning(
                        "Timed out waiting %.1fs for %d inflight management operation(s) during shutdown",
                        elapsed,
                        iface._management_inflight,
                    )
                    break
                remaining = management_shutdown_wait_timeout - elapsed
                iface._management_idle_condition.wait(
                    timeout=min(management_wait_poll_seconds, remaining)
                )
        return management_wait_timed_out

    @staticmethod
    def _shutdown_discovery(iface: "BLEInterface") -> None:
        """Close discovery resources and stop receive-loop intent."""
        discovery_manager = iface._discovery_manager
        iface._discovery_manager = None
        if discovery_manager is not None:
            iface.error_handler.safe_cleanup(discovery_manager.close, "discovery manager close")
        iface._set_receive_wanted(want_receive=False)

    @staticmethod
    def _shutdown_receive_thread(iface: "BLEInterface") -> None:
        """Wake and join receive thread, then clear cached thread reference."""
        BLELifecycleService._thread_wake_waiting_threads(
            iface, READ_TRIGGER_EVENT, RECONNECTED_EVENT
        )
        receive_thread = iface._receiveThread
        if receive_thread is None:
            return
        if receive_thread is threading.current_thread():
            logger.debug("close() called from receive thread; skipping self-join")
        else:
            BLELifecycleService._thread_join_thread(
                iface, receive_thread, timeout=RECEIVE_THREAD_JOIN_TIMEOUT
            )
            _, thread_is_alive = _thread_start_probe(receive_thread)
            if thread_is_alive:
                logger.warning(
                    "BLE receive thread did not exit within %.1fs",
                    RECEIVE_THREAD_JOIN_TIMEOUT,
                )
        iface._receiveThread = None

    @staticmethod
    def _close_mesh_interface(iface: "BLEInterface") -> None:
        """Run mesh-interface close under safe execution wrapper."""
        iface.error_handler.safe_execute(
            lambda: MeshInterface.close(iface),
            error_msg="Error closing mesh interface",
        )

    @staticmethod
    def _unregister_exit_handler(iface: "BLEInterface") -> None:
        """Unregister process exit handler when present."""
        if iface._exit_handler:
            atexit.unregister(iface._exit_handler)
            iface._exit_handler = None

    @staticmethod
    def _detach_client_for_shutdown(iface: "BLEInterface") -> tuple["BLEClient | None", bool]:
        """Detach active client reference and return detached client plus publish state."""
        with iface._state_lock:
            client = iface.client
            publish_pending = iface._client_publish_pending
            if client is not None:
                iface.client = None
        return client, publish_pending

    @staticmethod
    def _consume_disconnect_notification_state(iface: "BLEInterface") -> bool:
        """Consume pending publish flags and return whether public disconnect should emit."""
        notify = False
        with iface._state_lock:
            if iface._client_publish_pending:
                replacement_pending = iface._client_replacement_pending
                iface._client_publish_pending = False
                iface._client_replacement_pending = False
                if replacement_pending and not iface._disconnect_notified:
                    iface._disconnect_notified = True
                    notify = True
                else:
                    iface._disconnect_notified = True
            elif iface._client_replacement_pending:
                iface._client_replacement_pending = False
                if not iface._disconnect_notified:
                    iface._disconnect_notified = True
                    notify = True
            elif not iface._disconnect_notified:
                iface._disconnect_notified = True
                notify = True
        return notify

    @staticmethod
    def _shutdown_client(
        iface: "BLEInterface", *, management_wait_timed_out: bool
    ) -> None:
        """Shutdown active client, notifications, and disconnect publication state."""
        client, publish_pending = BLELifecycleService._detach_client_for_shutdown(iface)
        client_address = iface._extract_client_address(client)
        notification_manager = iface._notification_manager

        def _resolve_notification_cleanup(
            public_name: str, legacy_name: str
        ) -> Callable[..., object] | None:
            method = getattr(notification_manager, public_name, None)
            if callable(method) and not _is_unconfigured_mock_callable(method):
                return cast(Callable[..., object], method)
            legacy_method = getattr(notification_manager, legacy_name, None)
            if callable(legacy_method) and not _is_unconfigured_mock_callable(
                legacy_method
            ):
                return cast(Callable[..., object], legacy_method)
            return None

        if client is not None:
            gate_context = (
                iface._management_target_gate(client_address)
                if client_address is not None
                and not management_wait_timed_out
                and not publish_pending
                else contextlib.nullcontext()
            )
            with gate_context:
                unsubscribe_all = _resolve_notification_cleanup(
                    "unsubscribe_all", "_unsubscribe_all"
                )
                if unsubscribe_all is not None:
                    iface.error_handler.safe_cleanup(
                        lambda: unsubscribe_all(
                            client, timeout=NOTIFICATION_START_TIMEOUT
                        ),
                        "notification unsubscribe_all",
                    )
                else:
                    logger.debug(
                        "Notification manager is missing unsubscribe_all/_unsubscribe_all"
                    )
                iface.error_handler.safe_cleanup(
                    lambda: iface._disconnect_and_close_client(client),
                    "BLE client disconnect/close",
                )
        cleanup_all = _resolve_notification_cleanup("cleanup_all", "_cleanup_all")
        if cleanup_all is not None:
            iface.error_handler.safe_cleanup(
                cleanup_all,
                "notification manager cleanup",
            )
        else:
            logger.debug("Notification manager is missing cleanup_all/_cleanup_all")

        if BLELifecycleService._consume_disconnect_notification_state(iface):
            iface._disconnected()
            iface._wait_for_disconnect_notifications()

    @staticmethod
    def _finalize_close_state(iface: "BLEInterface") -> None:
        """Persist terminal disconnected state and clear address registry claims."""
        with iface._state_lock:
            # Record final state as DISCONNECTED for observers; instance remains closed.
            iface._state_manager._transition_to(ConnectionState.DISCONNECTED)
            alias_key = iface._connection_alias_key
            iface._connection_alias_key = None
        close_key = _addr_key(iface.address)
        iface._mark_address_keys_disconnected(close_key, alias_key)
