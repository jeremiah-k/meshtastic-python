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
    """Service helpers for compatibility event publication paths.

    Notes
    -----
    All helpers in this service are best-effort and must not raise during
    disconnect/close paths where callers are already unwinding state.
    """

    @staticmethod
    def _enqueue_publish_callback(
        publishing_thread: object,
        callback: Any,
        *,
        prefer_non_blocking: bool = False,
    ) -> bool:
        """Queue a callback on the publish thread.

        Parameters
        ----------
        publishing_thread : object
            Publishing-thread façade exposing ``queueWork`` and/or ``queue``.
        callback : Any
            Callable to enqueue for deferred execution.
        prefer_non_blocking : bool
            When ``True``, prefer ``queue.put_nowait`` before ``queueWork``.

        Returns
        -------
        bool
            ``True`` when the callback was queued, otherwise ``False``.
        """
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
        if prefer_non_blocking:
            thread = getattr(publishing_thread, "thread", None)
            is_alive = getattr(thread, "is_alive", None)
            thread_is_alive = False
            if callable(is_alive) and not _is_unconfigured_mock_callable(is_alive):
                try:
                    alive_result = is_alive()
                except Exception:  # noqa: BLE001 - enqueue path is best effort
                    alive_result = False
                thread_is_alive = (
                    alive_result if isinstance(alive_result, bool) else False
                )
            if thread_is_alive and put_nowait_callback is not None:
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
        """Wait for queued disconnect notifications to flush.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing error-handling helpers and fallback drain logic.
        timeout : float | None
            Maximum seconds to wait for flush completion. ``None`` uses the
            default disconnect timeout.
        publishing_thread : object
            Publishing-thread façade used to queue and drain callbacks.

        Returns
        -------
        None
            This method returns ``None`` and falls back to inline draining on
            queueing or wait failures.
        """
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
            alive_result = False
            if callable(is_alive) and not _is_unconfigured_mock_callable(is_alive):
                try:
                    alive_result = is_alive()
                except Exception:  # noqa: BLE001 - disconnect fallback must continue
                    alive_result = False
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
        """Drain pending publish callbacks on the current thread.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing safe execution helpers.
        flush_event : Event
            Event set by queued flush callbacks to indicate completion.
        publishing_thread : object
            Publishing-thread façade exposing queue-drain internals.

        Returns
        -------
        None
            Drains callbacks best-effort and suppresses callback failures.
        """
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
        """Publish legacy connection-status event for compatibility.

        Parameters
        ----------
        iface : BLEInterface
            Interface associated with the status event payload.
        connected : bool
            Connection status flag to publish.
        publishing_thread : object
            Publishing-thread façade used for deferred publish scheduling.

        Returns
        -------
        None
            Publication is best-effort; failures are logged and suppressed.
        """
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
                publishing_thread,
                _publish_status,
            )
            if not queued:
                _publish_status()
        except Exception:  # noqa: BLE001 - best-effort queueing path
            logger.debug(
                "Error queuing connection status publish via publishingThread.queueWork",
                exc_info=True,
            )
            _publish_status()
