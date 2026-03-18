"""Shutdown lifecycle coordinator runtime ownership for BLE."""

import atexit
import contextlib
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from meshtastic.interfaces.ble.constants import (
    NOTIFICATION_START_TIMEOUT,
    READ_TRIGGER_EVENT,
    RECEIVE_THREAD_JOIN_TIMEOUT,
    RECONNECTED_EVENT,
    logger,
)
from meshtastic.interfaces.ble.gating import _addr_key
from meshtastic.interfaces.ble.lifecycle_primitives import (
    _LifecycleErrorAccess,
    _LifecycleStateAccess,
    _LifecycleThreadAccess,
)
from meshtastic.interfaces.ble.state import ConnectionState
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_callable,
    _is_unconfigured_mock_member,
    _thread_start_probe,
)
from meshtastic.mesh_interface import MeshInterface

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient
    from meshtastic.interfaces.ble.interface import BLEInterface


class BLEShutdownLifecycleCoordinator:
    """Own interface shutdown orchestration and terminal cleanup behavior."""

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind shutdown ownership to a specific interface."""
        self._iface = iface
        self._state_access = _LifecycleStateAccess(iface)
        self._thread_access = _LifecycleThreadAccess(iface)
        self._error_access = _LifecycleErrorAccess(iface)

    def is_connection_closing(self) -> bool:
        """Return whether this interface is closing or already closed."""
        iface = self._iface
        with iface._state_lock:
            return self._state_access.is_closing() or iface._closed

    def _cleanup_thread_coordinator(self) -> None:
        """Run thread-coordinator cleanup via public/legacy compatibility hooks."""
        iface = self._iface
        cleanup = getattr(iface.thread_coordinator, "cleanup", None)
        if callable(cleanup) and not _is_unconfigured_mock_callable(cleanup):
            try:
                cleanup()
            except Exception:  # noqa: BLE001 - shutdown cleanup is best effort
                logger.debug(
                    "Error running thread coordinator cleanup()", exc_info=True
                )
            return

        legacy_cleanup = getattr(iface.thread_coordinator, "_cleanup", None)
        if callable(legacy_cleanup) and not _is_unconfigured_mock_callable(
            legacy_cleanup
        ):
            try:
                legacy_cleanup()
            except Exception:  # noqa: BLE001 - shutdown cleanup is best effort
                logger.debug(
                    "Error running thread coordinator _cleanup()", exc_info=True
                )
            return

        logger.debug("Thread coordinator is missing cleanup/_cleanup")

    def _await_management_shutdown(
        self,
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
        current_state_getter: Callable[[], ConnectionState] | None = None,
        is_closing_getter: Callable[[], bool] | None = None,
        transition_to_state: Callable[[ConnectionState], bool] | None = None,
        reset_to_disconnected: Callable[[], bool] | None = None,
    ) -> bool | None:
        """Mark interface closed and wait for in-flight management operations."""
        iface = self._iface
        get_current_state = current_state_getter or self._state_access.current_state
        get_is_closing = is_closing_getter or self._state_access.is_closing
        do_transition_to = transition_to_state or self._state_access.transition_to
        do_reset_to_disconnected = (
            reset_to_disconnected or self._state_access.reset_to_disconnected
        )
        management_wait_timed_out = False
        management_wait_started = time.monotonic()
        with iface._management_lock:
            with iface._state_lock:
                if iface._closed:
                    logger.debug(
                        "BLEInterface.close called on already closed interface; ignoring"
                    )
                    return None
                was_closing = get_is_closing()
                iface._closed = True
                if was_closing:
                    logger.debug(
                        "BLEInterface.close called while another shutdown is in progress; continuing with cleanup"
                    )
                if get_current_state() not in (
                    ConnectionState.DISCONNECTED,
                    ConnectionState.DISCONNECTING,
                ) and not do_transition_to(ConnectionState.DISCONNECTING):
                    current_state = get_current_state()
                    logger.error(
                        "Failed state transition to %s during shutdown (alias=%s current=%s); forcing reset.",
                        ConnectionState.DISCONNECTING.value,
                        iface._connection_alias_key,
                        getattr(current_state, "value", current_state),
                    )
                    if not do_reset_to_disconnected() and not do_transition_to(
                        ConnectionState.DISCONNECTED
                    ):
                        fallback_state = get_current_state()
                        logger.error(
                            "Failed forced transition to %s during shutdown fallback (alias=%s current=%s).",
                            ConnectionState.DISCONNECTED.value,
                            iface._connection_alias_key,
                            getattr(fallback_state, "value", fallback_state),
                        )
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

    def _shutdown_discovery(
        self,
        *,
        safe_cleanup: Callable[[Callable[[], object], str], None] | None = None,
    ) -> None:
        """Close discovery resources and clear receive-loop intent."""
        iface = self._iface
        run_safe_cleanup = safe_cleanup or self._error_access.safe_cleanup
        discovery_manager = iface._discovery_manager
        iface._discovery_manager = None
        if discovery_manager is not None:
            run_safe_cleanup(discovery_manager.close, "discovery manager close")
        iface._set_receive_wanted(want_receive=False)

    def _shutdown_receive_thread(
        self,
        *,
        wake_waiting_threads: Callable[..., None] | None = None,
        join_thread: Callable[..., None] | None = None,
    ) -> None:
        """Wake and join receive thread, then clear cached thread reference."""
        iface = self._iface
        wake_waiters = wake_waiting_threads or self._thread_access.wake_waiting_threads
        if join_thread is None:
            join_runtime_thread = self._thread_access.join_thread
        else:

            def join_runtime_thread(thread: object, *, timeout: float | None) -> None:
                try:
                    join_thread(thread, timeout=timeout)
                except TypeError:
                    join_thread(thread, timeout)

        wake_waiters(READ_TRIGGER_EVENT, RECONNECTED_EVENT)
        receive_thread = iface._receiveThread
        if receive_thread is None:
            return
        thread_ident, thread_is_alive = _thread_start_probe(receive_thread)
        if thread_ident is None and not thread_is_alive:
            with iface._state_lock:
                if iface._receiveThread is receive_thread:
                    iface._receiveThread = None
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
            logger.debug(
                "Skipping receive thread join during close: worker never started."
            )
            return
        current_ident = threading.get_ident()
        if (
            thread_ident is not None and thread_ident == current_ident
        ) or receive_thread is threading.current_thread():
            logger.debug("close() called from receive thread; skipping self-join")
        else:
            join_runtime_thread(
                receive_thread,
                timeout=RECEIVE_THREAD_JOIN_TIMEOUT,
            )
            _, thread_is_alive = _thread_start_probe(receive_thread)
            if thread_is_alive:
                logger.warning(
                    "BLE receive thread did not exit within %.1fs",
                    RECEIVE_THREAD_JOIN_TIMEOUT,
                )
        with iface._state_lock:
            if iface._receiveThread is receive_thread and not thread_is_alive:
                iface._receiveThread = None
                iface._receive_start_pending = False
                iface._receive_start_pending_since = None

    def _close_mesh_interface(
        self,
        *,
        safe_execute: Callable[[Callable[[], object]], object | None] | None = None,
    ) -> None:
        """Run `MeshInterface.close` through guarded error-handler execution."""
        iface = self._iface
        run_safe_execute = safe_execute or (
            lambda func: self._error_access.safe_execute(
                func,
                error_msg="Error closing mesh interface",
            )
        )
        run_safe_execute(lambda: MeshInterface.close(iface))

    def _unregister_exit_handler(self) -> None:
        """Unregister process exit handler when present."""
        iface = self._iface
        if iface._exit_handler:
            atexit.unregister(iface._exit_handler)
            iface._exit_handler = None

    def _detach_client_for_shutdown(self) -> tuple["BLEClient | None", bool]:
        """Detach active client reference and return detached client plus publish state."""
        iface = self._iface
        with iface._state_lock:
            client = iface.client
            publish_pending = iface._client_publish_pending
            if client is not None:
                iface.client = None
        return client, publish_pending

    def _consume_disconnect_notification_state(self) -> bool:
        """Consume publish flags and decide disconnect notification emission."""
        iface = self._iface
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
                raw_ever_connected = getattr(iface, "_ever_connected", False)
                if _is_unconfigured_mock_member(raw_ever_connected):
                    notify = False
                else:
                    notify = raw_ever_connected is True
        return notify

    def _shutdown_client(
        self,
        *,
        management_wait_timed_out: bool,
        detach_client_for_shutdown: (
            Callable[[], tuple["BLEClient | None", bool]] | None
        ) = None,
        safe_cleanup: Callable[[Callable[[], object], str], None] | None = None,
        consume_disconnect_notification_state: Callable[[], bool] | None = None,
    ) -> None:
        """Shutdown active client resources and notification publication state."""
        iface = self._iface
        detach_client = detach_client_for_shutdown or self._detach_client_for_shutdown
        run_safe_cleanup = safe_cleanup or self._error_access.safe_cleanup
        consume_disconnect_state = (
            consume_disconnect_notification_state
            or self._consume_disconnect_notification_state
        )
        client, publish_pending = detach_client()
        client_address = iface._extract_client_address(client)
        notification_manager = iface._notification_manager

        def _resolve_notification_cleanup(method_name: str) -> Callable[..., object] | None:
            method = getattr(notification_manager, method_name, None)
            if callable(method) and not _is_unconfigured_mock_callable(method):
                return cast(Callable[..., object], method)
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
                unsubscribe_all = _resolve_notification_cleanup("_unsubscribe_all")
                if unsubscribe_all is not None:
                    run_safe_cleanup(
                        lambda: unsubscribe_all(
                            client, timeout=NOTIFICATION_START_TIMEOUT
                        ),
                        "notification unsubscribe_all",
                    )
                else:
                    logger.debug(
                        "Notification manager is missing _unsubscribe_all"
                    )
                run_safe_cleanup(
                    lambda: iface._disconnect_and_close_client(client),
                    "BLE client disconnect/close",
                )
        cleanup_all = _resolve_notification_cleanup("_cleanup_all")
        if cleanup_all is not None:
            run_safe_cleanup(cleanup_all, "notification manager cleanup")
        else:
            logger.debug("Notification manager is missing _cleanup_all")

        if consume_disconnect_state():
            iface._disconnected()
            iface._wait_for_disconnect_notifications()

    def _finalize_close_state(
        self,
        *,
        current_state_getter: Callable[[], ConnectionState] | None = None,
        transition_to_state: Callable[[ConnectionState], bool] | None = None,
        reset_to_disconnected: Callable[[], bool] | None = None,
    ) -> None:
        """Persist terminal disconnected state and clear address registry claims."""
        iface = self._iface
        get_current_state = current_state_getter or self._state_access.current_state
        do_transition_to = transition_to_state or self._state_access.transition_to
        do_reset_to_disconnected = (
            reset_to_disconnected or self._state_access.reset_to_disconnected
        )
        with iface._state_lock:
            # Record final state as DISCONNECTED for observers; instance remains closed.
            if not do_transition_to(ConnectionState.DISCONNECTED):
                current_state = get_current_state()
                logger.error(
                    "Failed state transition to %s during close finalization (alias=%s current=%s); forcing reset.",
                    ConnectionState.DISCONNECTED.value,
                    iface._connection_alias_key,
                    getattr(current_state, "value", current_state),
                )
                if not do_reset_to_disconnected() and not do_transition_to(
                    ConnectionState.DISCONNECTED
                ):
                    fallback_state = get_current_state()
                    logger.error(
                        "Failed forced transition to %s during close finalization (alias=%s current=%s).",
                        ConnectionState.DISCONNECTED.value,
                        iface._connection_alias_key,
                        getattr(fallback_state, "value", fallback_state),
                    )
            alias_key = iface._connection_alias_key
            iface._connection_alias_key = None
        close_key = _addr_key(iface.address)
        iface._mark_address_keys_disconnected(close_key, alias_key)

    def close(
        self,
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
    ) -> None:
        """Shut down BLE interface resources and finalize lifecycle state."""
        management_wait_timed_out = self._await_management_shutdown(
            management_shutdown_wait_timeout=management_shutdown_wait_timeout,
            management_wait_poll_seconds=management_wait_poll_seconds,
        )
        if management_wait_timed_out is None:
            return

        try:
            iface = self._iface
            if iface._shutdown_event is not None:
                iface._shutdown_event.set()
            self._shutdown_discovery()
            self._shutdown_receive_thread()
            self._close_mesh_interface()
            self._unregister_exit_handler()
            self._shutdown_client(management_wait_timed_out=management_wait_timed_out)
        finally:
            try:
                self._cleanup_thread_coordinator()
            finally:
                self._finalize_close_state()
