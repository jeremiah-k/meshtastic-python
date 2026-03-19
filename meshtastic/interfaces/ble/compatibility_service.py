"""Compatibility/event publication helpers for BLE interface orchestration."""

from queue import Empty, Full
from threading import Event
from typing import TYPE_CHECKING, Any, Callable, cast

from meshtastic.interfaces.ble.constants import (
    DISCONNECT_TIMEOUT_SECONDS,
    MAX_DRAIN_ITERATIONS,
    logger,
)
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_callable,
)
from meshtastic.interfaces.ble.utils import (
    _resolve_safe_execute as _resolve_safe_execute_hook,
)
from meshtastic.interfaces.ble.utils import (
    _safe_execute_through_adapter,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface


class BLECompatibilityEventPublisher:
    """Instance-bound collaborator for compatibility event publication."""

    def __init__(
        self, iface: "BLEInterface", *, publishing_thread_provider: Callable[[], object]
    ) -> None:
        """Bind publisher helpers to a specific interface instance."""
        self._iface = iface
        self._publishing_thread_provider = publishing_thread_provider

    def _resolve_publishing_thread(self) -> object | None:
        """Return publishing thread when available, else ``None`` on provider failure."""
        try:
            return self._publishing_thread_provider()
        except Exception:  # noqa: BLE001 - teardown path must stay best effort
            logger.debug(
                "Error resolving publishing thread for compatibility publisher.",
                exc_info=True,
            )
            return None

    def wait_for_disconnect_notifications(self, timeout: float | None = None) -> None:
        """Wait for disconnect-notification queue flush on the bound interface."""
        BLECompatibilityEventService.wait_for_disconnect_notifications(
            self._iface,
            timeout,
            publishing_thread=self._resolve_publishing_thread(),
        )

    def drain_publish_queue(self, flush_event: Event) -> None:
        """Drain queued publish callbacks for the bound interface."""
        BLECompatibilityEventService.drain_publish_queue(
            self._iface,
            flush_event,
            publishing_thread=self._resolve_publishing_thread(),
        )

    def publish_connection_status(self, *, connected: bool) -> None:
        """Publish connection status via the bound interface publishing thread."""
        BLECompatibilityEventService.publish_connection_status(
            self._iface,
            connected=connected,
            publishing_thread=self._resolve_publishing_thread(),
        )

    # COMPAT_STABLE_SHIM: retained bound alias for compatibility callers.
    def publish_connection_status_legacy(
        self, connected: bool
    ) -> None:  # noqa: FBT001 - compatibility positional bool
        """Publish status through legacy positional API retained for compatibility.

        This alias preserves historical call sites that invoke
        ``publish_connection_status_legacy(connected)`` directly on the bound
        compatibility publisher while runtime behavior remains delegated to the
        canonical ``publish_connection_status`` implementation.

        Parameters
        ----------
        connected : bool
            Connection status flag (``True`` for connected, ``False`` for
            disconnected).

        Returns
        -------
        None
            Always returns ``None``.
        """
        BLECompatibilityEventService.publish_connection_status_legacy(
            self._iface,
            connected,
            publishing_thread=self._resolve_publishing_thread(),
        )


class BLECompatibilityEventService:
    """Service helpers for compatibility event publication paths.

    Notes
    -----
    All helpers in this service are best-effort and must not raise during
    disconnect/close paths where callers are already unwinding state.
    """

    @staticmethod
    def _resolve_safe_execute(iface: "BLEInterface") -> Callable[..., Any] | None:
        """Resolve an error-handler ``safe_execute`` hook with underscore fallback.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing an optional ``error_handler`` attribute.

        Returns
        -------
        Callable[..., Any] | None
            Resolved ``safe_execute`` callable when available and configured,
            otherwise ``None``.
        """
        return _resolve_safe_execute_hook(iface)

    @staticmethod
    def _safe_execute(
        iface: "BLEInterface",
        func: Callable[[], Any],
        *,
        default_return: Any = None,
        error_msg: str,
        reraise: bool = False,
    ) -> Any:
        """Execute a callable through resolved safe execution with best-effort fallback.

        Parameters
        ----------
        iface : BLEInterface
            Interface used to resolve ``error_handler.safe_execute`` helpers.
        func : Callable[[], Any]
            Zero-argument callable to execute.
        default_return : Any
            Value returned when execution fails and ``reraise`` is ``False``.
        error_msg : str
            Log message used when execution fails.
        reraise : bool
            When ``True``, propagate execution exceptions after logging.

        Returns
        -------
        Any
            Callable return value on success; otherwise ``default_return`` when
            failures are handled.

        Raises
        ------
        Exception
            Re-raises execution failures only when ``reraise`` is ``True``.
        """
        return _safe_execute_through_adapter(
            iface,
            func,
            default_return=default_return,
            error_msg=error_msg,
            reraise=reraise,
        )

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
            When ``True``, only attempt ``queue.put_nowait``. If a non-blocking
            enqueue path is unavailable or full, return ``False`` so callers
            can apply an inline fallback.

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
        thread = getattr(publishing_thread, "thread", None)
        is_alive = getattr(thread, "is_alive", None)
        thread_is_alive: bool | None = None
        if callable(is_alive) and not _is_unconfigured_mock_callable(is_alive):
            try:
                alive_result = is_alive()
            except Exception:  # noqa: BLE001 - enqueue path is best effort
                alive_result = False
            thread_is_alive = alive_result if isinstance(alive_result, bool) else False
            if thread_is_alive is False:
                return False
        if prefer_non_blocking:
            if put_nowait_callback is not None:
                try:
                    put_nowait_callback(callback)
                    return True
                except Full:
                    return False
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

        queued = BLECompatibilityEventService._safe_execute(
            iface,
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
            thread_is_alive = alive_result if isinstance(alive_result, bool) else False
            if thread_is_alive:
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
            try:
                BLECompatibilityEventService._safe_execute(
                    iface,
                    lambda: thread_drain(flush_event),
                    error_msg="Error draining publish queue via publishing thread",
                    reraise=True,
                )
                return
            except Exception:  # noqa: BLE001 - fall back to direct draining path
                logger.debug(
                    "Error draining publish queue via publishing thread; falling back to direct drain.",
                    exc_info=True,
                )

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
            except Exception:  # noqa: BLE001 - direct drain must stay best effort
                logger.debug(
                    "Error reading deferred publish queue during direct drain",
                    exc_info=True,
                )
                break
            iterations += 1
            BLECompatibilityEventService._safe_execute(
                iface,
                cast(Callable[[], Any], runnable),
                error_msg="Error in deferred publish callback",
                reraise=False,
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
                prefer_non_blocking=True,
            )
            if not queued:
                _publish_status()
        except Exception:  # noqa: BLE001 - best-effort queueing path
            logger.debug(
                "Error queuing connection status publish via publishingThread.queueWork",
                exc_info=True,
            )
            _publish_status()

    # COMPAT_STABLE_SHIM: retained for existing callers during service migration.
    @staticmethod
    def publish_connection_status_legacy(
        iface: "BLEInterface", connected: bool, *, publishing_thread: object
    ) -> None:
        """Backward-compatible alias for ``publish_connection_status``.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose connection status is being published.
        connected : bool
            ``True`` for connected status and ``False`` for disconnected.
        publishing_thread : object
            Publishing-thread facade used to enqueue/send the legacy message.

        Returns
        -------
        None
            This compatibility alias returns ``None``.
        """
        BLECompatibilityEventService.publish_connection_status(
            iface,
            connected,
            publishing_thread=publishing_thread,
        )
