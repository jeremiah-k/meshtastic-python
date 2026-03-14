"""Lifecycle-oriented helpers for BLE interface orchestration."""

import threading
from typing import TYPE_CHECKING

from bleak import BleakClient as BleakRootClient

from meshtastic.interfaces.ble.constants import logger
from meshtastic.interfaces.ble.utils import sanitize_address

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
