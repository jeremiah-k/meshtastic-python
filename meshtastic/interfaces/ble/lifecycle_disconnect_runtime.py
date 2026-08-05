"""Disconnect lifecycle coordinator runtime ownership for BLE."""

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from bleak import BleakClient as BleakRootClient

from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_member,
    _resolve_declared_callable,
)
from meshtastic.interfaces.ble.ports import _BLESessionStatePort
from meshtastic.interfaces.ble.session_state import _session_state_for
from meshtastic.interfaces.ble.constants import (
    READ_TRIGGER_EVENT,
    RECONNECTED_EVENT,
    logger,
)
from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.gating import _addr_key
from meshtastic.interfaces.ble.lifecycle_primitives import (
    RECONNECT_SCHEDULER_MISSING_MSG,
    _DisconnectPlan,
    _LifecycleErrorAccess,
    _LifecycleStateAccess,
    _LifecycleThreadAccess,
)
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from meshtastic.interfaces.ble.utils import (
    _sleep,
    _thread_start_probe,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient
    from meshtastic.interfaces.ble.interface import BLEInterface

CLOSE_THREAD_START_PROBE_DELAY_SEC = 0.001


class BLEDisconnectLifecycleCoordinator:
    """Own disconnect orchestration and reconnect scheduling behavior."""

    def __init__(
        self, iface: "BLEInterface", *, session_state: _BLESessionStatePort | None = None
    ) -> None:
        """Bind disconnect orchestration ownership to a specific interface.

        Parameters
        ----------
        iface : BLEInterface
            Interface instance whose disconnect orchestration is managed.
        session_state : _BLESessionStatePort | None
            Optional shared lifecycle state. When ``None``, resolve the
            interface-owned state or use the legacy adapter.

        Returns
        -------
        None
            Initializes bound disconnect-orchestration collaborator state.
        """
        self._iface = iface
        self._session = _session_state_for(iface, session_state)
        self._state_access = _LifecycleStateAccess(iface)
        self._thread_access = _LifecycleThreadAccess(iface)
        self._error_access = _LifecycleErrorAccess(iface)

    def schedule_auto_reconnect(
        self,
        *,
        is_closing_getter: Callable[[], bool] | None = None,
    ) -> None:
        """Schedule background auto-reconnect work when reconnect is enabled."""
        iface = self._iface
        get_is_closing = is_closing_getter or self._state_access.is_closing
        if not iface.auto_reconnect:
            return
        with self._session.lock:
            if self._session.closed:
                logger.debug(
                    "Skipping auto-reconnect scheduling because interface is closed."
                )
                return
        compatibility_is_closing = get_is_closing()
        with self._session.lock:
            # Revalidate terminal/session state after the lock-free compatibility
            # probe before clearing the shutdown event and scheduling work.
            if not iface.auto_reconnect or self._session.closed:
                return
            if compatibility_is_closing:
                logger.debug(
                    "Skipping auto-reconnect scheduling because interface is closing."
                )
                return
            iface._shutdown_event.clear()
        reconnect_scheduler = _get_declared_member(iface, "_reconnect_scheduler")
        schedule_reconnect = _resolve_declared_callable(
            reconnect_scheduler, "schedule_reconnect", "_schedule_reconnect"
        )
        if schedule_reconnect is None:
            raise AttributeError(RECONNECT_SCHEDULER_MISSING_MSG)
        schedule_reconnect(iface.auto_reconnect, iface._shutdown_event)

    def disconnect_and_close_client(
        self,
        client: "BLEClient",
        *,
        timeout: float | None = None,
    ) -> None:
        """Release BLE client resources with best-effort disconnect/close handling.

        Parameters
        ----------
        client : BLEClient
            Client to disconnect and close.
        timeout : float | None, optional
            Optional maximum seconds to wait for client disconnect/close.
        """
        self._iface._client_manager_safe_close_client(
            client,
            disconnect_timeout=timeout,
        )

    def on_ble_disconnect(self, client: BleakRootClient) -> None:
        """Handle a Bleak disconnect callback from the active transport client."""
        self.handle_disconnect("bleak_callback", bleak_client=client)

    def _compute_disconnect_keys(
        self,
        *,
        previous_client: "BLEClient | None",
        alias_key: str | None,
        should_reconnect: bool,
        address: str,
    ) -> tuple[list[str], bool]:
        """Compute disconnect registry keys and reconnect scheduling intent."""
        iface = self._iface
        with self._session.lock:
            session_closed = self._session.closed
        should_schedule_reconnect = should_reconnect and not session_closed
        if should_reconnect:
            if previous_client is not None:
                previous_address = (
                    iface._extract_client_address(previous_client) or address
                )
                if not previous_address or previous_address == "unknown":
                    previous_address = (
                        address
                        if address != "unknown"
                        else (iface.address or "unknown")
                    )
                device_key = _addr_key(previous_address) if previous_address else None
                return (
                    iface._sorted_address_keys(device_key, alias_key),
                    should_schedule_reconnect,
                )
            resolved_fallback = address if address != "unknown" else iface.address
            fallback_key = _addr_key(resolved_fallback) if resolved_fallback else None
            return (
                iface._sorted_address_keys(fallback_key, alias_key),
                should_schedule_reconnect,
            )

        address_for_registry = (
            iface._extract_client_address(previous_client)
            if previous_client is not None
            else None
        )
        if not address_for_registry or address_for_registry == "unknown":
            address_for_registry = address if address != "unknown" else iface.address
        addr_disconnect_key = (
            _addr_key(address_for_registry) if address_for_registry else None
        )
        return (
            iface._sorted_address_keys(addr_disconnect_key, alias_key),
            should_schedule_reconnect,
        )

    def _resolve_disconnect_target(
        self,
        source: str,
        client: "BLEClient | None",
        bleak_client: BleakRootClient | None,
        *,
        current_state_getter: Callable[[], ConnectionState] | None = None,
        is_closing_getter: Callable[[], bool] | None = None,
        transition_to_disconnected: Callable[[], bool] | None = None,
        reset_to_disconnected: Callable[[], bool] | None = None,
    ) -> _DisconnectPlan:
        """Resolve disconnect ownership, mutate state, and build side-effect plan."""
        iface = self._iface
        get_current_state = current_state_getter or self._state_access.current_state
        get_is_closing = is_closing_getter or self._state_access.is_closing
        do_transition_to_disconnected = transition_to_disconnected or (
            lambda: self._state_access.transition_to(ConnectionState.DISCONNECTED)
        )
        do_reset_to_disconnected = reset_to_disconnected or (
            self._state_access.reset_to_disconnected
        )
        target_client = client
        should_reconnect = False

        state_manager = _get_declared_member(iface, "_state_manager")
        canonical_state_manager = (
            state_manager
            if type(state_manager) is BLEStateManager  # pylint: disable=unidiomatic-typecheck
            and state_manager.lock is self._session.lock
            and current_state_getter is None
            and transition_to_disconnected is None
            and reset_to_disconnected is None
            else None
        )

        # Compatibility closing probes may execute collaborator code. Shutdown
        # claims ``session.closed`` under this same lock, so combining the
        # lock-free compatibility snapshot with the locked terminal flag avoids
        # holding shared lifecycle state around that probe.
        compatibility_is_closing = get_is_closing()
        compatibility_current_state = (
            None if canonical_state_manager is not None else get_current_state()
        )
        with self._session.lock:
            # Connection state and session ownership share this RLock on the real
            # interface. Keep the state transition atomic with clearing the owned
            # client/session fields. Compatibility probes/callbacks are excluded
            # from this critical section; the exact BLEStateManager uses its
            # lock-owned primitive instead.
            current_state = cast(
                ConnectionState,
                BLEStateManager._current_state_unlocked(canonical_state_manager)
                if canonical_state_manager is not None
                else compatibility_current_state,
            )
            current_client = iface.client
            is_closing = compatibility_is_closing or self._session.closed
            was_publish_pending = self._session.client_publish_pending
            was_replacement_pending = self._session.client_replacement_pending

            if current_state == ConnectionState.CONNECTING:
                disconnect_from_owned_client = current_client is not None and (
                    target_client is current_client
                    or (
                        target_client is None
                        and bleak_client is not None
                        and getattr(current_client, "bleak_client", None)
                        is bleak_client
                    )
                )
                if not disconnect_from_owned_client:
                    logger.debug(
                        "Ignoring disconnect from %s while a connection is in progress.",
                        source,
                    )
                    return _DisconnectPlan(early_return=True)
            if is_closing:
                logger.debug("Ignoring disconnect from %s during shutdown.", source)
                return _DisconnectPlan(early_return=False)

            if target_client is None and bleak_client is not None:
                if (
                    current_client is not None
                    and getattr(current_client, "bleak_client", None) is bleak_client
                ):
                    target_client = current_client
                elif current_client is not None:
                    logger.debug("Ignoring stale disconnect from %s.", source)
                    return _DisconnectPlan(early_return=True)

            if (
                current_client is None
                and not was_publish_pending
                and not was_replacement_pending
            ):
                logger.debug(
                    "Ignoring stale disconnect from %s: no active client is owned.",
                    source,
                )
                return _DisconnectPlan(early_return=True)

            if (
                target_client is not None
                and current_client is not None
                and target_client is not current_client
            ):
                logger.debug("Ignoring stale disconnect from %s.", source)
                return _DisconnectPlan(early_return=True)

            if self._session.disconnect_notified:
                logger.debug("Ignoring duplicate disconnect from %s.", source)
                return _DisconnectPlan(early_return=True)

            previous_client = current_client
            client_at_start = current_client
            alias_key = self._session.connection_alias_key
            session_epoch = self._session.connection_session_epoch
            iface.client = None
            self._session.client_publish_pending = False
            self._session.client_replacement_pending = False
            self._session.disconnect_notified = True
            self._session.connection_alias_key = None
            if canonical_state_manager is not None:
                transitioned = BLEStateManager._transition_to_unlocked(
                    canonical_state_manager, ConnectionState.DISCONNECTED
                )
                if not transitioned:
                    logger.error(
                        "Failed state transition to %s during disconnect target resolution (alias=%s current=%s); forcing reset.",
                        ConnectionState.DISCONNECTED.value,
                        alias_key,
                        getattr(current_state, "value", current_state),
                    )
                    reset_succeeded = BLEStateManager._reset_to_disconnected_unlocked(
                        canonical_state_manager
                    )
                    if not reset_succeeded:
                        fallback_state = BLEStateManager._current_state_unlocked(
                            canonical_state_manager
                        )
                        logger.error(
                            "Failed forced reset to %s during disconnect target resolution (alias=%s current=%s).",
                            ConnectionState.DISCONNECTED.value,
                            alias_key,
                            getattr(fallback_state, "value", fallback_state),
                        )
                should_reconnect = iface.auto_reconnect

        if canonical_state_manager is None:
            if not do_transition_to_disconnected():
                logger.error(
                    "Failed state transition to %s during disconnect target resolution (alias=%s current=%s); forcing reset.",
                    ConnectionState.DISCONNECTED.value,
                    alias_key,
                    getattr(current_state, "value", current_state),
                )
                if not do_reset_to_disconnected():
                    fallback_state = get_current_state()
                    logger.error(
                        "Failed forced reset to %s during disconnect target resolution (alias=%s current=%s).",
                        ConnectionState.DISCONNECTED.value,
                        alias_key,
                        getattr(fallback_state, "value", fallback_state),
                    )
            should_reconnect = iface.auto_reconnect

        def _normalize_disconnect_address(value: object | None) -> str:
            if isinstance(value, str) and value:
                return value
            return iface.address or "unknown"

        address = iface.address or "unknown"
        if target_client is not None:
            address = _normalize_disconnect_address(
                iface._extract_client_address(target_client)
                or getattr(target_client, "address", None)
            )
        elif bleak_client is not None:
            address = _normalize_disconnect_address(getattr(bleak_client, "address", None))
        elif previous_client is not None:
            address = _normalize_disconnect_address(
                iface._extract_client_address(previous_client)
                or getattr(previous_client, "address", None)
            )

        disconnect_keys, should_schedule_reconnect = self._compute_disconnect_keys(
            previous_client=previous_client,
            alias_key=alias_key,
            should_reconnect=should_reconnect,
            address=address,
        )
        return _DisconnectPlan(
            early_return=None,
            previous_client=previous_client,
            client_at_start=client_at_start,
            session_epoch=session_epoch,
            address=address,
            disconnect_keys=tuple(disconnect_keys),
            should_reconnect=should_reconnect,
            should_schedule_reconnect=should_schedule_reconnect,
            was_publish_pending=was_publish_pending,
            was_replacement_pending=was_replacement_pending,
        )

    def _close_previous_client_async(
        self,
        previous_client: "BLEClient | None",
        *,
        create_thread: Callable[..., ThreadLike] | None = None,
        start_thread: Callable[[object], None] | None = None,
        safe_cleanup: Callable[[Callable[[], object], str], None] | None = None,
    ) -> None:
        """Close a disconnected previous client asynchronously."""
        iface = self._iface
        create_runtime_thread = create_thread or self._thread_access.create_thread
        start_runtime_thread = start_thread or self._thread_access.start_thread
        run_safe_cleanup = safe_cleanup or self._error_access.safe_cleanup
        if previous_client is None:
            return

        def _close_inline() -> None:
            run_safe_cleanup(
                lambda: iface._client_manager_safe_close_client(previous_client),
                "BLE client close during disconnect",
            )

        try:
            close_thread = create_runtime_thread(
                target=_close_inline,
                name="BLEClientClose",
                daemon=True,
            )
            start_runtime_thread(close_thread)
            thread_ident, thread_is_alive = _thread_start_probe(close_thread)
            if thread_ident is None and not thread_is_alive:
                _sleep(CLOSE_THREAD_START_PROBE_DELAY_SEC)
                thread_ident, thread_is_alive = _thread_start_probe(close_thread)
            if thread_ident is None and not thread_is_alive:
                # Probe Thread._started to distinguish "failed to start" from
                # custom thread-likes with delayed ident/is_alive publication.
                started_event = getattr(close_thread, "_started", None)
                is_started = getattr(started_event, "is_set", None)
                start_failure_confirmed = False
                if callable(is_started):
                    try:
                        start_failure_confirmed = not bool(is_started())
                    except Exception:  # noqa: BLE001 - probe remains best effort
                        start_failure_confirmed = False
                elif isinstance(close_thread, threading.Thread):
                    start_failure_confirmed = True
                if not start_failure_confirmed:
                    logger.debug(
                        "BLE client close thread start probe inconclusive; keeping async close path."
                    )
                    return
                logger.warning(
                    "BLE client close thread did not start; closing inline.",
                    exc_info=False,
                )
                _close_inline()
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            logger.warning(
                "Failed to start async BLE client close; closing inline.",
                exc_info=True,
            )
            _close_inline()
            raise
        except Exception:  # noqa: BLE001 - cleanup must not abort disconnect flow
            logger.warning(
                "Failed to start async BLE client close; closing inline.",
                exc_info=True,
            )
            _close_inline()

    def _compute_stale_disconnect_keys(
        self,
        *,
        disconnect_keys: list[str],
        active_client: "BLEClient | None",
        client_at_start: "BLEClient | None",
        active_alias_key: str | None,
    ) -> tuple[list[str], bool]:
        """Return stale disconnect keys and whether active client differs."""
        iface = self._iface
        active_client_differs = active_client is not client_at_start
        if active_client is not None:
            active_address = iface._extract_client_address(active_client)
            active_keys = set(
                iface._sorted_address_keys(
                    _addr_key(active_address) if active_address else None,
                    active_alias_key,
                )
            )
            return (
                [key for key in disconnect_keys if key not in active_keys],
                active_client_differs,
            )
        return list(disconnect_keys), active_client_differs

    def _execute_disconnect_side_effects(
        self,
        *,
        plan: _DisconnectPlan,
        source: str,
        close_previous_client_async: Callable[["BLEClient | None"], None] | None = None,
        clear_events: Callable[..., None] | None = None,
    ) -> bool:
        """Execute disconnect publication/cleanup side effects."""
        iface = self._iface
        close_previous = (
            close_previous_client_async or self._close_previous_client_async
        )
        clear_runtime_events = clear_events or self._thread_access.clear_events
        disconnect_keys = list(plan.disconnect_keys)
        skip_side_effects = False
        with self._session.lock:
            active_client = iface.client
            active_session_epoch = self._session.connection_session_epoch
            if active_session_epoch != plan.session_epoch or (
                active_client is not None and active_client is not plan.client_at_start
            ):
                skip_side_effects = True

        if skip_side_effects:
            stale_disconnect_keys: list[str] = []
            close_stale_client = False
            still_stale = False
            active_alias_key: str | None = None
            with self._session.lock:
                active_client = iface.client
                active_session_epoch = self._session.connection_session_epoch
                active_alias_key = self._session.connection_alias_key
                still_stale = active_session_epoch != plan.session_epoch or (
                    active_client is not None
                    and active_client is not plan.client_at_start
                )
            if still_stale:
                stale_disconnect_keys, active_client_differs = (
                    self._compute_stale_disconnect_keys(
                        disconnect_keys=disconnect_keys,
                        active_client=active_client,
                        client_at_start=plan.client_at_start,
                        active_alias_key=active_alias_key,
                    )
                )
                close_stale_client = active_client_differs
                if stale_disconnect_keys:
                    iface._mark_address_keys_disconnected(*stale_disconnect_keys)
                if close_stale_client:
                    close_previous(plan.previous_client)
                logger.debug(
                    "Skipping stale disconnect side-effects from %s: newer client already active.",
                    source,
                )
                return True

        logger.debug("BLE client %s disconnected (source: %s).", plan.address, source)
        with self._session.lock:
            self._session.last_disconnect_source = f"ble.{source}"

        close_previous(plan.previous_client)
        stale_after_close = False
        with self._session.lock:
            active_client = iface.client
            active_session_epoch = self._session.connection_session_epoch
            stale_after_close = active_session_epoch != plan.session_epoch or (
                active_client is not None and active_client is not plan.client_at_start
            )
        if stale_after_close:
            still_stale_after_close = False
            rechecked_stale_disconnect_keys_after_close: list[str] = []
            active_alias_key = None
            with self._session.lock:
                active_client = iface.client
                active_session_epoch = self._session.connection_session_epoch
                active_alias_key = self._session.connection_alias_key
                still_stale_after_close = (
                    active_session_epoch != plan.session_epoch
                    or (
                        active_client is not None
                        and active_client is not plan.client_at_start
                    )
                )
            if still_stale_after_close:
                rechecked_stale_disconnect_keys_after_close, _ = (
                    self._compute_stale_disconnect_keys(
                        disconnect_keys=disconnect_keys,
                        active_client=active_client,
                        client_at_start=plan.client_at_start,
                        active_alias_key=active_alias_key,
                    )
                )
                if rechecked_stale_disconnect_keys_after_close:
                    iface._mark_address_keys_disconnected(
                        *rechecked_stale_disconnect_keys_after_close
                    )
                logger.debug(
                    "Skipping stale disconnect publication/reconnect from %s after close_previous().",
                    source,
                )
                return True
        if disconnect_keys:
            iface._mark_address_keys_disconnected(*disconnect_keys)
        if not plan.was_publish_pending or plan.was_replacement_pending:
            iface._disconnected()
        else:
            logger.debug(
                "Skipping public disconnect event for provisional session from %s.",
                source,
            )

        if plan.should_reconnect:
            if plan.should_schedule_reconnect:
                clear_runtime_events(READ_TRIGGER_EVENT, RECONNECTED_EVENT)
                self.schedule_auto_reconnect()
            return True

        logger.debug("Auto-reconnect disabled, staying disconnected.")
        return False

    def handle_disconnect(
        self,
        source: str,
        *,
        client: "BLEClient | None" = None,
        bleak_client: BleakRootClient | None = None,
        is_closing_getter: Callable[[], bool] | None = None,
        current_state_getter: Callable[[], ConnectionState] | None = None,
        transition_to_disconnected: Callable[[], bool] | None = None,
        reset_to_disconnected: Callable[[], bool] | None = None,
        close_previous_client_async: Callable[["BLEClient | None"], None] | None = None,
        clear_events: Callable[..., None] | None = None,
    ) -> bool:
        """Handle disconnect orchestration and reconnect decisions."""
        iface = self._iface
        get_is_closing = is_closing_getter or self._state_access.is_closing
        if not iface._disconnect_lock.acquire(blocking=False):
            logger.debug(
                "Disconnect from %s skipped: another disconnect handler is active.",
                source,
            )
            compatibility_is_closing = get_is_closing()
            with self._session.lock:
                return (
                    not self._session.closed
                    and not compatibility_is_closing
                    and (
                        iface.auto_reconnect
                        or self._session.want_receive
                        or iface.client is not None
                        or self._session.client_publish_pending
                        or self._session.client_replacement_pending
                    )
                )

        disconnect_lock_released = False
        plan = _DisconnectPlan(early_return=False)
        try:
            plan = self._resolve_disconnect_target(
                source,
                client,
                bleak_client,
                current_state_getter=current_state_getter,
                is_closing_getter=is_closing_getter,
                transition_to_disconnected=transition_to_disconnected,
                reset_to_disconnected=reset_to_disconnected,
            )
            if plan.early_return is not None:
                return plan.early_return

            iface._disconnect_lock.release()
            disconnect_lock_released = True
        finally:
            if not disconnect_lock_released:
                iface._disconnect_lock.release()

        return self._execute_disconnect_side_effects(
            plan=plan,
            source=source,
            close_previous_client_async=close_previous_client_async,
            clear_events=clear_events,
        )
