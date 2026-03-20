"""Disconnect lifecycle coordinator runtime ownership for BLE."""

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from bleak import BleakClient as BleakRootClient

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
from meshtastic.interfaces.ble.state import ConnectionState
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_callable,
    _sleep,
    _thread_start_probe,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient
    from meshtastic.interfaces.ble.interface import BLEInterface

CLOSE_THREAD_START_PROBE_DELAY_SEC = 0.001


class BLEDisconnectLifecycleCoordinator:
    """Own disconnect orchestration and reconnect scheduling behavior."""

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind disconnect orchestration ownership to a specific interface."""
        self._iface = iface
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
        with iface._state_lock:
            if not iface.auto_reconnect:
                return
            if iface._closed:
                logger.debug(
                    "Skipping auto-reconnect scheduling because interface is closed."
                )
                return
            if get_is_closing():
                logger.debug(
                    "Skipping auto-reconnect scheduling because interface is closing."
                )
                return
            iface._shutdown_event.clear()
        schedule_reconnect = getattr(
            iface._reconnect_scheduler, "schedule_reconnect", None
        )
        if not callable(schedule_reconnect) or _is_unconfigured_mock_callable(
            schedule_reconnect
        ):
            schedule_reconnect = getattr(
                iface._reconnect_scheduler, "_schedule_reconnect", None
            )
        if not callable(schedule_reconnect) or _is_unconfigured_mock_callable(
            schedule_reconnect
        ):
            raise AttributeError(RECONNECT_SCHEDULER_MISSING_MSG)
        schedule_reconnect(iface.auto_reconnect, iface._shutdown_event)

    def disconnect_and_close_client(self, client: "BLEClient") -> None:
        """Release BLE client resources with best-effort disconnect/close handling."""
        self._iface._client_manager_safe_close_client(client)

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
        should_schedule_reconnect = should_reconnect and not iface._closed
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
        with iface._state_lock:
            current_state = get_current_state()
            current_client = iface.client
            is_closing = get_is_closing() or iface._closed
            was_publish_pending = iface._client_publish_pending
            was_replacement_pending = iface._client_replacement_pending

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
                address = _normalize_disconnect_address(
                    getattr(bleak_client, "address", None)
                )
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
                session_epoch=getattr(iface, "_connection_session_epoch", 0),
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
        with iface._state_lock:
            active_client = iface.client
            active_session_epoch = getattr(iface, "_connection_session_epoch", 0)
            if active_session_epoch != plan.session_epoch or (
                active_client is not None and active_client is not plan.client_at_start
            ):
                skip_side_effects = True

        if skip_side_effects:
            stale_disconnect_keys: list[str] = []
            close_stale_client = False
            still_stale = False
            with iface._state_lock:
                active_client = iface.client
                active_session_epoch = getattr(iface, "_connection_session_epoch", 0)
                still_stale = active_session_epoch != plan.session_epoch or (
                    active_client is not None
                    and active_client is not plan.client_at_start
                )
                if still_stale:
                    close_stale_client = (
                        active_client is not plan.client_at_start
                        and plan.previous_client is not active_client
                    )
                    if active_client is not None:
                        active_address = iface._extract_client_address(active_client)
                        active_keys = set(
                            iface._sorted_address_keys(
                                _addr_key(active_address) if active_address else None,
                                iface._connection_alias_key,
                            )
                        )
                        stale_disconnect_keys = [
                            key for key in disconnect_keys if key not in active_keys
                        ]
                    else:
                        stale_disconnect_keys = list(disconnect_keys)
            if still_stale:
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
        iface._last_disconnect_source = f"ble.{source}"

        close_previous(plan.previous_client)
        stale_after_close = False
        with iface._state_lock:
            active_client = iface.client
            active_session_epoch = getattr(iface, "_connection_session_epoch", 0)
            stale_after_close = active_session_epoch != plan.session_epoch or (
                active_client is not None and active_client is not plan.client_at_start
            )
        if stale_after_close:
            still_stale_after_close = False
            rechecked_stale_disconnect_keys_after_close: list[str] = []
            with iface._state_lock:
                active_client = iface.client
                active_session_epoch = getattr(iface, "_connection_session_epoch", 0)
                still_stale_after_close = (
                    active_session_epoch != plan.session_epoch
                    or (
                        active_client is not None
                        and active_client is not plan.client_at_start
                    )
                )
                if still_stale_after_close:
                    if active_client is not None:
                        active_address = iface._extract_client_address(active_client)
                        active_keys = set(
                            iface._sorted_address_keys(
                                _addr_key(active_address) if active_address else None,
                                iface._connection_alias_key,
                            )
                        )
                        rechecked_stale_disconnect_keys_after_close = [
                            key for key in disconnect_keys if key not in active_keys
                        ]
                    else:
                        rechecked_stale_disconnect_keys_after_close = list(
                            disconnect_keys
                        )
            if still_stale_after_close:
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
            with iface._state_lock:
                return (
                    not iface._closed
                    and not get_is_closing()
                    and (
                        iface.auto_reconnect
                        or iface._want_receive
                        or iface.client is not None
                        or iface._client_publish_pending
                        or iface._client_replacement_pending
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
