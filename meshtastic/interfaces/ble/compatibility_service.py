"""Compatibility/event publication helpers for BLE interface orchestration."""

from queue import Empty, Full
from threading import Event
from typing import TYPE_CHECKING, Any, Callable, cast

from meshtastic.interfaces.ble.constants import (
    DISCONNECT_TIMEOUT_SECONDS,
    MAX_DRAIN_ITERATIONS,
    logger,
)
from meshtastic.interfaces.ble.utils import _is_unconfigured_mock_callable

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface


class BLECompatibilityEventService:
    """Service helpers for compatibility event publication paths."""

    @staticmethod
    def _enqueue_publish_callback(
        publishing_thread: object,
        callback: Any,
        *,
        prefer_non_blocking: bool = False,
    ) -> bool:
        """Queue callback for publishing thread, preferring non-blocking enqueue."""
        queue_work = getattr(publishing_thread, "queueWork", None)
        queue = getattr(publishing_thread, "queue", None)
        put_nowait = getattr(queue, "put_nowait", None)
        can_queue_work = callable(queue_work) and not _is_unconfigured_mock_callable(
            queue_work
        )
        can_put_nowait = callable(put_nowait) and not _is_unconfigured_mock_callable(
            put_nowait
        )
        queue_work_callback: Callable[[Any], object] | None = (
            cast(Callable[[Any], object], queue_work) if can_queue_work else None
        )
        put_nowait_callback: Callable[[Any], object] | None = (
            cast(Callable[[Any], object], put_nowait) if can_put_nowait else None
        )
        if prefer_non_blocking and put_nowait_callback is not None:
            try:
                put_nowait_callback(callback)
                return True
            except Full:
                return False
        if queue_work_callback is not None:
            queue_work_callback(callback)
            return True
        if put_nowait_callback is not None:
            try:
                put_nowait_callback(callback)
                return True
            except Full:
                return False
        return False

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
            return BLECompatibilityEventService._enqueue_publish_callback(
                publishing_thread,
                flush_event.set,
                prefer_non_blocking=True,
            )

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
            is_alive = getattr(thread, "is_alive", None)
            alive_result = is_alive() if callable(is_alive) else False
            thread_is_alive = (
                alive_result if isinstance(alive_result, bool) else False
            )
            if (
                callable(is_alive)
                and not _is_unconfigured_mock_callable(is_alive)
                and thread_is_alive
            ):
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
        if callable(thread_drain) and not _is_unconfigured_mock_callable(thread_drain):
            iface.error_handler.safe_execute(
                lambda: thread_drain(flush_event),
                error_msg="Error draining publish queue via publishing thread",
                reraise=False,
            )
            return

        # Do not call iface._drain_publish_queue here - it would recursively
        # call this method again. Instead, fall through to the direct queue
        # draining logic below.

        queue = getattr(publishing_thread, "queue", None)
        get_nowait = getattr(queue, "get_nowait", None)
        if (
            queue is None
            or not callable(get_nowait)
            or _is_unconfigured_mock_callable(get_nowait)
        ):
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
                runnable = get_nowait()
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
            queued = BLECompatibilityEventService._enqueue_publish_callback(
                publishing_thread, _publish_status
            )
            if not queued:
                _publish_status()
        except Exception:  # noqa: BLE001 - best-effort queueing path
            logger.debug(
                "Error queuing connection status publish via publishingThread.queueWork",
                exc_info=True,
            )
            _publish_status()
