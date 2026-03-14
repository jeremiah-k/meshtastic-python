"""Compatibility/event publication helpers for BLE interface orchestration."""

from queue import Empty
from threading import Event
from typing import TYPE_CHECKING, Any

from meshtastic.interfaces.ble.constants import (
    DISCONNECT_TIMEOUT_SECONDS,
    MAX_DRAIN_ITERATIONS,
    logger,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface


class BLECompatibilityEventService:
    """Service helpers for compatibility event publication paths."""

    @staticmethod
    def wait_for_disconnect_notifications(
        iface: "BLEInterface",
        timeout: float | None = None,
        *,
        publishing_thread: object,
    ) -> None:
        """Wait for publishing thread to flush disconnect notifications."""
        if timeout is None:
            timeout = DISCONNECT_TIMEOUT_SECONDS
        flush_event = Event()

        def _queue_flush_notification() -> bool:
            publishing_thread.queueWork(flush_event.set)  # type: ignore[attr-defined]
            return True

        queued = iface.error_handler.safe_execute(
            _queue_flush_notification,
            default_return=False,
            error_msg="Runtime error during disconnect notification flush (possible threading issue)",
            reraise=False,
        )
        if not queued:
            BLECompatibilityEventService.drain_publish_queue(
                iface,
                flush_event,
                publishing_thread=publishing_thread,
            )
            return

        if not flush_event.wait(timeout=timeout):
            thread = getattr(publishing_thread, "thread", None)
            if thread is not None and thread.is_alive():
                logger.debug("Timed out waiting for publish queue flush")
            else:
                BLECompatibilityEventService.drain_publish_queue(
                    iface,
                    flush_event,
                    publishing_thread=publishing_thread,
                )

    @staticmethod
    def drain_publish_queue(
        iface: "BLEInterface", flush_event: Event, *, publishing_thread: object
    ) -> None:
        """Drain and run pending publish callbacks on the current thread."""
        thread = getattr(publishing_thread, "thread", None)
        thread_drain = getattr(thread, "_drain_publish_queue", None)
        if callable(thread_drain):
            iface.error_handler.safe_execute(
                lambda: thread_drain(flush_event),
                error_msg="Error draining publish queue via publishing thread",
                reraise=False,
            )
            return

        iface_drain = getattr(iface, "_drain_publish_queue", None)
        if callable(iface_drain):
            iface.error_handler.safe_execute(
                lambda: iface_drain(flush_event),
                error_msg="Error draining publish queue via interface fallback",
                reraise=False,
            )
            return

        queue = getattr(publishing_thread, "queue", None)
        if queue is None:
            return
        iterations = 0
        while not flush_event.is_set():
            if iterations >= MAX_DRAIN_ITERATIONS:
                logger.debug(
                    "Stopping publish queue drain after %d callbacks to avoid shutdown starvation",
                    MAX_DRAIN_ITERATIONS,
                )
                break
            try:
                runnable = queue.get_nowait()
            except Empty:
                break
            iterations += 1
            iface.error_handler.safe_execute(
                runnable, error_msg="Error in deferred publish callback", reraise=False
            )

    @staticmethod
    def publish_connection_status(
        iface: "BLEInterface", connected: bool, *, publishing_thread: object
    ) -> None:
        """Publish legacy connection status event for compatibility."""
        from meshtastic import mesh_interface as mesh_iface_module

        mesh_pub: Any = getattr(mesh_iface_module, "pub", None)
        if mesh_pub is None:
            logger.debug("Skipping connection status publish: mesh pub is unavailable")
            return

        def _publish_status() -> None:
            try:
                mesh_pub.sendMessage(
                    "meshtastic.connection.status", interface=iface, connected=connected
                )
            except Exception:  # noqa: BLE001 - best-effort publish path
                logger.debug(
                    "Error publishing %s status via mesh_pub.sendMessage",
                    "connect" if connected else "disconnect",
                    exc_info=True,
                )

        try:
            publishing_thread.queueWork(_publish_status)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - best-effort queueing path
            logger.debug(
                "Error queuing connection status publish via publishingThread.queueWork",
                exc_info=True,
            )
