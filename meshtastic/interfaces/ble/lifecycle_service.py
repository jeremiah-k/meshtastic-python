"""Lifecycle-oriented helpers for BLE interface orchestration."""

import atexit
import contextlib
import threading
import time
from typing import TYPE_CHECKING

from bleak import BleakClient as BleakRootClient

from meshtastic.interfaces.ble.constants import (
    NOTIFICATION_START_TIMEOUT,
    RECEIVE_THREAD_JOIN_TIMEOUT,
    logger,
)
from meshtastic.interfaces.ble.gating import _addr_key
from meshtastic.interfaces.ble.state import ConnectionState
from meshtastic.interfaces.ble.utils import sanitize_address
from meshtastic.mesh_interface import MeshInterface

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient
    from meshtastic.interfaces.ble.interface import BLEInterface


class BLELifecycleService:
    """Service helpers for BLEInterface lifecycle responsibilities."""

    @staticmethod
    def _set_receive_wanted(iface: "BLEInterface", want_receive: bool) -> None:
        """Request or clear the background receive loop."""
        with iface._state_lock:
            iface._want_receive = want_receive

    @staticmethod
    def _should_run_receive_loop(iface: "BLEInterface") -> bool:
        """Return whether the receive loop should run."""
        with iface._state_lock:
            return iface._want_receive and not iface._closed

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
            if (
                existing is not None
                and existing is not threading.current_thread()
                and (existing.is_alive() or existing.ident is None)
            ):
                logger.debug(
                    "Skipping receive thread start (%s): %s is already running or pending start.",
                    name,
                    existing.name,
                )
                return
            thread = iface.thread_coordinator._create_thread(
                target=iface._receive_from_radio_impl,
                name=name,
                daemon=True,
            )
            iface._receiveThread = thread
        try:
            iface.thread_coordinator._start_thread(thread)
        except Exception:  # noqa: BLE001 - start failure must clear stale thread reference
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receiveThread = None
            raise
        thread_is_alive = bool(
            callable(getattr(thread, "is_alive", None)) and thread.is_alive()
        )
        if not thread_is_alive:
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
        iface._reconnect_scheduler.schedule_reconnect(
            iface.auto_reconnect, iface._shutdown_event
        )

    @staticmethod
    def _disconnect_and_close_client(
        iface: "BLEInterface", client: "BLEClient"
    ) -> None:
        """Ensure BLE client resources are released."""
        iface._client_manager_safe_close_client(client)

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
        target_client = client
        previous_client: "BLEClient | None" = None
        client_at_start: "BLEClient | None" = None
        should_reconnect = False
        should_schedule_reconnect = False
        was_publish_pending = False
        was_replacement_pending = False
        address = "unknown"
        disconnect_keys: list[str] = []
        try:
            # Lock-order invariant: release _disconnect_lock before any
            # operations that compute/acquire address locks.
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
                    return True
                if is_closing:
                    logger.debug("Ignoring disconnect from %s during shutdown.", source)
                    return False

                # Resolve callback source against the currently active client.
                if target_client is None and bleak_client is not None:
                    if (
                        current_client
                        and getattr(current_client, "bleak_client", None)
                        is bleak_client
                    ):
                        target_client = current_client
                    elif current_client is not None:
                        logger.debug("Ignoring stale disconnect from %s.", source)
                        return True

                # Ignore stale disconnect callbacks from non-active clients.
                if (
                    target_client is not None
                    and current_client is not None
                    and target_client is not current_client
                ):
                    logger.debug("Ignoring stale disconnect from %s.", source)
                    return True

                # Prevent duplicate disconnect notifications.
                if iface._disconnect_notified:
                    logger.debug("Ignoring duplicate disconnect from %s.", source)
                    return True

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
                should_schedule_reconnect = should_reconnect and not iface._closed

                if target_client:
                    address = getattr(target_client, "address", repr(target_client))
                elif bleak_client:
                    address = getattr(bleak_client, "address", repr(bleak_client))
                elif previous_client:
                    address = getattr(previous_client, "address", repr(previous_client))

                if should_reconnect:
                    if previous_client:
                        previous_address = getattr(previous_client, "address", iface.address)
                        device_key = (
                            _addr_key(previous_address) if previous_address else None
                        )
                        disconnect_keys = iface._sorted_address_keys(device_key, alias_key)
                    else:
                        fallback_key = _addr_key(iface.address)
                        disconnect_keys = iface._sorted_address_keys(
                            fallback_key, alias_key
                        )
                else:
                    # Guard against sentinel "unknown" address; prefer previous
                    # active address because callback metadata can be stale.
                    address_for_registry = (
                        getattr(previous_client, "address", None)
                        if previous_client
                        else (address if address != "unknown" else iface.address)
                    )
                    addr_disconnect_key = _addr_key(address_for_registry)
                    disconnect_keys = iface._sorted_address_keys(
                        addr_disconnect_key, alias_key
                    )

            # Release the disconnect lock before any address-lock operations.
            iface._disconnect_lock.release()
            disconnect_lock_released = True
        finally:
            if not disconnect_lock_released:
                iface._disconnect_lock.release()

        skip_side_effects = False
        stale_disconnect_keys: list[str] = []
        with iface._state_lock:
            active_client = iface.client
            if active_client is not None and active_client is not client_at_start:
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

        def _close_previous_client_async() -> None:
            if previous_client:
                close_thread = iface.thread_coordinator._create_thread(
                    target=iface._client_manager_safe_close_client,
                    args=(previous_client,),
                    name="BLEClientClose",
                    daemon=True,
                )
                iface.thread_coordinator._start_thread(close_thread)

        if skip_side_effects:
            if stale_disconnect_keys:
                iface._mark_address_keys_disconnected(*stale_disconnect_keys)
            _close_previous_client_async()
            logger.debug(
                "Skipping stale disconnect side-effects from %s: newer client already active.",
                source,
            )
            return True

        logger.debug("BLE client %s disconnected (source: %s).", address, source)
        # Expose the most recent disconnect source for external listeners that
        # only receive the generic meshtastic.connection.lost event.
        iface._last_disconnect_source = f"ble.{source}"

        if disconnect_keys:
            iface._mark_address_keys_disconnected(*disconnect_keys)

        _close_previous_client_async()
        if not was_publish_pending or was_replacement_pending:
            iface._disconnected()
        else:
            logger.debug(
                "Skipping public disconnect event for provisional session from %s.",
                source,
            )

        if should_reconnect:
            if should_schedule_reconnect:
                iface.thread_coordinator._clear_events(
                    "read_trigger", "reconnected_event"
                )
                iface._schedule_auto_reconnect()
            return True

        logger.debug("Auto-reconnect disabled, staying disconnected.")
        return False

    @staticmethod
    def _emit_verified_connection_side_effects(
        iface: "BLEInterface", connected_client: "BLEClient"
    ) -> None:
        """Emit reconnect signaling/logging only after verified connect publish."""
        coordinator = getattr(iface, "thread_coordinator", None)
        if iface._prior_publish_was_reconnect and coordinator is not None:
            coordinator._set_event("reconnected_event")
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
        else:
            if is_closing:
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
    def _close(
        iface: "BLEInterface",
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
    ) -> None:
        """Shut down the BLE interface and release associated resources."""
        # Deliberately avoid _connect_lock here so close() can mark shutdown
        # immediately and in-flight connect/pair timeouts can observe it.
        management_wait_timed_out = False
        management_wait_started = time.monotonic()
        with iface._management_lock:
            with iface._state_lock:
                if iface._closed:
                    logger.debug(
                        "BLEInterface.close called on already closed interface; ignoring"
                    )
                    return
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

        try:
            if iface._shutdown_event is not None:
                iface._shutdown_event.set()

            discovery_manager = iface._discovery_manager
            iface._discovery_manager = None
            if discovery_manager is not None:
                iface.error_handler.safe_cleanup(
                    discovery_manager.close, "discovery manager close"
                )

            iface._set_receive_wanted(want_receive=False)
            iface.thread_coordinator._wake_waiting_threads(
                "read_trigger", "reconnected_event"
            )
            if iface._receiveThread:
                if iface._receiveThread is threading.current_thread():
                    logger.debug("close() called from receive thread; skipping self-join")
                else:
                    iface.thread_coordinator._join_thread(
                        iface._receiveThread, timeout=RECEIVE_THREAD_JOIN_TIMEOUT
                    )
                    if iface._receiveThread.is_alive():
                        logger.warning(
                            "BLE receive thread did not exit within %.1fs",
                            RECEIVE_THREAD_JOIN_TIMEOUT,
                        )
                iface._receiveThread = None

            iface.error_handler.safe_execute(
                lambda: MeshInterface.close(iface),
                error_msg="Error closing mesh interface",
            )

            if iface._exit_handler:
                atexit.unregister(iface._exit_handler)
                iface._exit_handler = None

            with iface._state_lock:
                client = iface.client
                publish_pending = iface._client_publish_pending
                if client is not None:
                    iface.client = None

            client_address = iface._extract_client_address(client)
            if client is not None:
                gate_context = (
                    iface._management_target_gate(client_address)
                    if client_address is not None
                    and not management_wait_timed_out
                    and not publish_pending
                    else contextlib.nullcontext()
                )
                with gate_context:
                    iface._notification_manager.unsubscribe_all(
                        client, timeout=NOTIFICATION_START_TIMEOUT
                    )
                    iface._disconnect_and_close_client(client)
            iface._notification_manager.cleanup_all()

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

            if notify:
                iface._disconnected()
                iface._wait_for_disconnect_notifications()

            iface.thread_coordinator._cleanup()
        finally:
            with iface._state_lock:
                # Record final state as DISCONNECTED for observers; instance remains closed.
                iface._state_manager._transition_to(ConnectionState.DISCONNECTED)
                alias_key = iface._connection_alias_key
                iface._connection_alias_key = None
            close_key = _addr_key(iface.address)
            iface._mark_address_keys_disconnected(close_key, alias_key)
