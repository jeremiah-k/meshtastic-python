"""Lifecycle-oriented helpers for BLE interface orchestration."""

import threading
from typing import TYPE_CHECKING

from bleak import BleakClient as BleakRootClient

from meshtastic.interfaces.ble.constants import logger

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient
    from meshtastic.interfaces.ble.interface import BLEInterface


class BLELifecycleService:
    """Service helpers for BLEInterface lifecycle responsibilities."""

    @staticmethod
    def set_receive_wanted(iface: "BLEInterface", want_receive: bool) -> None:
        """Request or clear the background receive loop."""
        with iface._state_lock:
            iface._want_receive = want_receive

    @staticmethod
    def should_run_receive_loop(iface: "BLEInterface") -> bool:
        """Return whether the receive loop should run."""
        with iface._state_lock:
            return iface._want_receive and not iface._closed

    @staticmethod
    def start_receive_thread(
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
            thread = iface.thread_coordinator.create_thread(
                target=iface._receive_from_radio_impl,
                name=name,
                daemon=True,
            )
            iface._receiveThread = thread
        try:
            iface.thread_coordinator.start_thread(thread)
        except Exception:  # noqa: BLE001 - start failure must clear stale thread reference
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receiveThread = None
            raise
        if reset_recovery:
            # Reset recovery throttling on successful thread start (not during recovery).
            with iface._state_lock:
                iface._receive_recovery_attempts = 0

    @staticmethod
    def on_ble_disconnect(iface: "BLEInterface", client: BleakRootClient) -> None:
        """Handle Bleak client disconnect callback."""
        iface._handle_disconnect("bleak_callback", bleak_client=client)

    @staticmethod
    def schedule_auto_reconnect(iface: "BLEInterface") -> None:
        """Schedule background auto-reconnect attempts when enabled."""
        if not iface.auto_reconnect:
            return
        with iface._state_lock:
            if iface._closed:
                logger.debug(
                    "Skipping auto-reconnect scheduling because interface is closed."
                )
                return
            if iface._state_manager.is_closing:
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
    def disconnect_and_close_client(iface: "BLEInterface", client: "BLEClient") -> None:
        """Ensure BLE client resources are released."""
        iface._client_manager_safe_close_client(client)
