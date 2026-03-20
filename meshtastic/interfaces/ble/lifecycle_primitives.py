"""Shared lifecycle primitives for BLE runtime ownership."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from meshtastic.interfaces.ble.constants import logger
from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.state import ConnectionState
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_callable,
    _is_unconfigured_mock_member,
    _is_unexpected_keyword_error,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient
    from meshtastic.interfaces.ble.interface import BLEInterface

THREAD_COORDINATOR_MISSING_FMT = "Thread coordinator is missing %s/%s"
RECONNECT_SCHEDULER_MISSING_MSG = (
    "Reconnect scheduler is missing schedule_reconnect/_schedule_reconnect"
)
STATE_MANAGER_MISSING_CONNECTED_MSG = (
    "State manager is missing is_connected/_is_connected boolean members"
)
STATE_MANAGER_MISSING_CURRENT_STATE_MSG = (
    "State manager is missing current_state/_current_state members"
)
STATE_MANAGER_MISSING_TRANSITION_MSG = (
    "State manager is missing transition_to/_transition_to"
)
STATE_MANAGER_MISSING_RESET_MSG = (
    "State manager is missing reset_to_disconnected/_reset_to_disconnected"
)
CLIENT_MISSING_CONNECTED_MSG = (
    "BLE client is missing isConnected/is_connected/_is_connected members"
)


@dataclass(frozen=True)
class _DisconnectPlan:
    """Resolved disconnect handling plan from state-locked prechecks."""

    early_return: bool | None
    previous_client: "BLEClient | None" = None
    client_at_start: "BLEClient | None" = None
    session_epoch: int = 0
    address: str = "unknown"
    disconnect_keys: tuple[str, ...] = ()
    should_reconnect: bool = False
    should_schedule_reconnect: bool = False
    was_publish_pending: bool = False
    was_replacement_pending: bool = False


@dataclass(frozen=True)
class _OwnershipSnapshot:
    """Snapshot of connect-result ownership and shutdown/gate status."""

    still_owned: bool
    is_closing: bool
    lost_gate_ownership: bool
    prior_ever_connected: bool


def _client_is_connected_compat(client: "BLEClient") -> bool:
    """Return connected-state flag from public/legacy BLEClient members."""
    for candidate_name in ("isConnected", "is_connected", "_is_connected"):
        candidate = getattr(client, candidate_name, None)
        if callable(candidate):
            if _is_unconfigured_mock_callable(candidate):
                continue
            try:
                connected = candidate()
            except Exception:  # noqa: BLE001 - connectivity probes stay best effort
                logger.debug(
                    "Error probing BLE client connectivity via %s",
                    candidate_name,
                    exc_info=True,
                )
                continue
            if isinstance(connected, bool):
                return connected
            continue
        if isinstance(candidate, bool) and not _is_unconfigured_mock_member(candidate):
            return candidate
    raise AttributeError(CLIENT_MISSING_CONNECTED_MSG)


class _LifecycleStateAccess:
    """Runtime state-manager access owned by lifecycle collaborators."""

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind state-manager access to a specific interface."""
        self._iface = iface

    def is_connected(self) -> bool:
        """Return connected-state flag from public-first state-manager members."""
        public_is_connected = getattr(self._iface._state_manager, "is_connected", None)
        if callable(public_is_connected) and not _is_unconfigured_mock_callable(
            public_is_connected
        ):
            try:
                result = public_is_connected()
            except Exception:  # noqa: BLE001 - compatibility probe must fall through
                logger.debug(
                    "Error probing state manager is_connected()",
                    exc_info=True,
                )
            else:
                if isinstance(result, bool):
                    return result
        if not _is_unconfigured_mock_member(public_is_connected) and isinstance(
            public_is_connected, bool
        ):
            return public_is_connected
        legacy_is_connected = getattr(self._iface._state_manager, "_is_connected", None)
        if callable(legacy_is_connected) and not _is_unconfigured_mock_callable(
            legacy_is_connected
        ):
            try:
                result = legacy_is_connected()
            except Exception:  # noqa: BLE001 - compatibility probe must fall through
                logger.debug(
                    "Error probing state manager _is_connected()",
                    exc_info=True,
                )
            else:
                if isinstance(result, bool):
                    return result
        if not _is_unconfigured_mock_member(legacy_is_connected) and isinstance(
            legacy_is_connected, bool
        ):
            return legacy_is_connected
        raise AttributeError(STATE_MANAGER_MISSING_CONNECTED_MSG)

    def current_state(self) -> ConnectionState:
        """Return current connection state from public-first state-manager members."""
        public_state = getattr(self._iface._state_manager, "current_state", None)
        if callable(public_state) and not _is_unconfigured_mock_callable(public_state):
            try:
                result = public_state()
            except Exception:  # noqa: BLE001 - compatibility probe must fall through
                logger.debug(
                    "Error probing state manager current_state()",
                    exc_info=True,
                )
            else:
                if isinstance(result, ConnectionState):
                    return result
        if not _is_unconfigured_mock_member(public_state) and isinstance(
            public_state, ConnectionState
        ):
            return public_state
        legacy_state = getattr(self._iface._state_manager, "_current_state", None)
        if callable(legacy_state) and not _is_unconfigured_mock_callable(legacy_state):
            try:
                result = legacy_state()
            except Exception:  # noqa: BLE001 - compatibility probe must fall through
                logger.debug(
                    "Error probing state manager _current_state()",
                    exc_info=True,
                )
            else:
                if isinstance(result, ConnectionState):
                    return result
        if not _is_unconfigured_mock_member(legacy_state) and isinstance(
            legacy_state, ConnectionState
        ):
            return legacy_state
        raise AttributeError(STATE_MANAGER_MISSING_CURRENT_STATE_MSG)

    def transition_to(self, new_state: ConnectionState) -> bool:
        """Transition state manager using public-first compatibility dispatch."""
        public_transition = getattr(self._iface._state_manager, "transition_to", None)
        if callable(public_transition) and not _is_unconfigured_mock_callable(
            public_transition
        ):
            try:
                result = public_transition(new_state)
            except Exception:  # noqa: BLE001 - compatibility probe must fall through
                logger.debug(
                    "Error calling state manager transition_to()",
                    exc_info=True,
                )
            else:
                if isinstance(result, bool):
                    return result
        legacy_transition = getattr(self._iface._state_manager, "_transition_to", None)
        if callable(legacy_transition) and not _is_unconfigured_mock_callable(
            legacy_transition
        ):
            try:
                result = legacy_transition(new_state)
            except Exception:  # noqa: BLE001 - compatibility probe must fall through
                logger.debug(
                    "Error calling state manager _transition_to()",
                    exc_info=True,
                )
            else:
                if isinstance(result, bool):
                    return result
        raise AttributeError(STATE_MANAGER_MISSING_TRANSITION_MSG)

    def reset_to_disconnected(self) -> bool:
        """Reset state manager to disconnected using public-first dispatch."""
        public_reset = getattr(
            self._iface._state_manager, "reset_to_disconnected", None
        )
        if callable(public_reset) and not _is_unconfigured_mock_callable(public_reset):
            try:
                result = public_reset()
            except Exception:  # noqa: BLE001 - compatibility probe must fall through
                logger.debug(
                    "Error calling state manager reset_to_disconnected()",
                    exc_info=True,
                )
            else:
                if isinstance(result, bool):
                    return result
        legacy_reset = getattr(
            self._iface._state_manager, "_reset_to_disconnected", None
        )
        if callable(legacy_reset) and not _is_unconfigured_mock_callable(legacy_reset):
            try:
                result = legacy_reset()
            except Exception:  # noqa: BLE001 - compatibility probe must fall through
                logger.debug(
                    "Error calling state manager _reset_to_disconnected()",
                    exc_info=True,
                )
            else:
                if isinstance(result, bool):
                    return result
        raise AttributeError(STATE_MANAGER_MISSING_RESET_MSG)

    def is_closing(self) -> bool:
        """Return closing-state flag from public-first state-manager members."""
        public_is_closing = getattr(self._iface._state_manager, "is_closing", None)
        if callable(public_is_closing) and not _is_unconfigured_mock_callable(
            public_is_closing
        ):
            try:
                result = public_is_closing()
            except Exception:  # noqa: BLE001 - compatibility probe must fall through
                logger.debug(
                    "Error probing state manager is_closing()",
                    exc_info=True,
                )
            else:
                if isinstance(result, bool):
                    return result
        if not _is_unconfigured_mock_member(public_is_closing) and isinstance(
            public_is_closing, bool
        ):
            return public_is_closing
        legacy_is_closing = getattr(self._iface._state_manager, "_is_closing", None)
        if callable(legacy_is_closing) and not _is_unconfigured_mock_callable(
            legacy_is_closing
        ):
            try:
                result = legacy_is_closing()
            except Exception:  # noqa: BLE001 - compatibility probe must fall through
                logger.debug(
                    "Error probing state manager _is_closing()",
                    exc_info=True,
                )
            else:
                if isinstance(result, bool):
                    return result
        if not _is_unconfigured_mock_member(legacy_is_closing) and isinstance(
            legacy_is_closing, bool
        ):
            return legacy_is_closing
        # Deliberate graceful-degradation asymmetry: close/shutdown paths should
        # not raise when optional state-manager hooks are absent.
        return False

    def client_is_connected(self, client: "BLEClient") -> bool:
        """Return connected-state flag from public/legacy BLEClient members."""
        return _client_is_connected_compat(client)


class _LifecycleThreadAccess:
    """Thread/event compatibility access owned by lifecycle collaborators.

    Contract: critical operations (`create_thread`, `start_thread`) raise
    `AttributeError` when required hooks are missing; non-critical operations
    (`join_thread`, `set_event`, `clear_events`, `wake_waiting_threads`) log and
    return to keep shutdown/recovery paths best effort.
    """

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind thread-coordinator access to a specific interface."""
        self._iface = iface

    def create_thread(
        self,
        *,
        target: Callable[..., object],
        name: str,
        daemon: bool,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
    ) -> ThreadLike:
        """Create thread via public-first coordinator compatibility dispatch."""
        coordinator = getattr(self._iface, "thread_coordinator", None)
        if coordinator is None or _is_unconfigured_mock_member(coordinator):
            raise AttributeError(
                THREAD_COORDINATOR_MISSING_FMT % ("create_thread", "_create_thread")
            )
        create_thread = getattr(coordinator, "create_thread", None)
        if callable(create_thread) and not _is_unconfigured_mock_callable(
            create_thread
        ):
            return cast(
                ThreadLike,
                create_thread(
                    target=target,
                    name=name,
                    daemon=daemon,
                    args=args,
                    kwargs=kwargs,
                ),
            )
        legacy_create_thread = getattr(coordinator, "_create_thread", None)
        if callable(legacy_create_thread) and not _is_unconfigured_mock_callable(
            legacy_create_thread
        ):
            return cast(
                ThreadLike,
                legacy_create_thread(
                    target=target,
                    name=name,
                    daemon=daemon,
                    args=args,
                    kwargs=kwargs,
                ),
            )
        raise AttributeError(
            THREAD_COORDINATOR_MISSING_FMT % ("create_thread", "_create_thread")
        )

    def start_thread(self, thread: object) -> None:
        """Start thread via public-first coordinator compatibility dispatch."""
        coordinator = getattr(self._iface, "thread_coordinator", None)
        if coordinator is None or _is_unconfigured_mock_member(coordinator):
            raise AttributeError(
                THREAD_COORDINATOR_MISSING_FMT % ("start_thread", "_start_thread")
            )
        start_thread = getattr(coordinator, "start_thread", None)
        if callable(start_thread) and not _is_unconfigured_mock_callable(start_thread):
            start_thread(thread)
            return
        legacy_start_thread = getattr(coordinator, "_start_thread", None)
        if callable(legacy_start_thread) and not _is_unconfigured_mock_callable(
            legacy_start_thread
        ):
            legacy_start_thread(thread)
            return
        raise AttributeError(
            THREAD_COORDINATOR_MISSING_FMT % ("start_thread", "_start_thread")
        )

    def join_thread(self, thread: object, *, timeout: float | None) -> None:
        """Join thread via public-first coordinator compatibility dispatch."""
        coordinator = getattr(self._iface, "thread_coordinator", None)
        if coordinator is None or _is_unconfigured_mock_member(coordinator):
            logger.debug("Thread coordinator is missing join_thread/_join_thread")
        else:
            join_thread = getattr(coordinator, "join_thread", None)
            if callable(join_thread) and not _is_unconfigured_mock_callable(
                join_thread
            ):
                try:
                    join_thread(thread, timeout=timeout)
                except Exception:  # noqa: BLE001 - non-critical join stays best effort
                    logger.debug(
                        "Error in thread_coordinator.join_thread for %r",
                        thread,
                        exc_info=True,
                    )
                else:
                    return
            legacy_join_thread = getattr(coordinator, "_join_thread", None)
            if callable(legacy_join_thread) and not _is_unconfigured_mock_callable(
                legacy_join_thread
            ):
                try:
                    legacy_join_thread(thread, timeout=timeout)
                except Exception:  # noqa: BLE001 - non-critical join stays best effort
                    logger.debug(
                        "Error in thread_coordinator._join_thread for %r",
                        thread,
                        exc_info=True,
                    )
                else:
                    return
        thread_join = getattr(thread, "join", None)
        if callable(thread_join) and not _is_unconfigured_mock_callable(thread_join):
            try:
                thread_join(timeout=timeout)
            except Exception:  # noqa: BLE001 - non-critical join stays best effort
                logger.debug("Error in thread.join for %r", thread, exc_info=True)
            return
        logger.debug("Thread coordinator is missing join_thread/_join_thread")

    def set_event(self, event_name: str) -> None:
        """Set event via public-first coordinator compatibility dispatch."""
        coordinator = getattr(self._iface, "thread_coordinator", None)
        if coordinator is None or _is_unconfigured_mock_member(coordinator):
            logger.debug("Thread coordinator is missing set_event/_set_event")
            return
        set_event = getattr(coordinator, "set_event", None)
        if callable(set_event) and not _is_unconfigured_mock_callable(set_event):
            try:
                set_event(event_name)
            except Exception:  # noqa: BLE001 - non-critical event set stays best effort
                logger.debug(
                    "Error in thread_coordinator.set_event for %s",
                    event_name,
                    exc_info=True,
                )
            else:
                return
        legacy_set_event = getattr(coordinator, "_set_event", None)
        if callable(legacy_set_event) and not _is_unconfigured_mock_callable(
            legacy_set_event
        ):
            try:
                legacy_set_event(event_name)
            except Exception:  # noqa: BLE001 - non-critical event set stays best effort
                logger.debug(
                    "Error in thread_coordinator._set_event for %s",
                    event_name,
                    exc_info=True,
                )
            else:
                return
        logger.debug("Thread coordinator is missing set_event/_set_event")

    def clear_events(self, *event_names: str) -> None:
        """Clear events via public-first coordinator compatibility dispatch."""
        coordinator = getattr(self._iface, "thread_coordinator", None)
        if coordinator is None or _is_unconfigured_mock_member(coordinator):
            logger.debug("Thread coordinator is missing clear_events/_clear_events")
            return
        clear_events = getattr(coordinator, "clear_events", None)
        if callable(clear_events) and not _is_unconfigured_mock_callable(clear_events):
            try:
                clear_events(*event_names)
            except Exception:  # noqa: BLE001 - non-critical clear stays best effort
                logger.debug(
                    "Error in thread_coordinator.clear_events for %s",
                    event_names,
                    exc_info=True,
                )
            else:
                return
        legacy_clear_events = getattr(coordinator, "_clear_events", None)
        if callable(legacy_clear_events) and not _is_unconfigured_mock_callable(
            legacy_clear_events
        ):
            try:
                legacy_clear_events(*event_names)
            except Exception:  # noqa: BLE001 - non-critical clear stays best effort
                logger.debug(
                    "Error in thread_coordinator._clear_events for %s",
                    event_names,
                    exc_info=True,
                )
            else:
                return
        logger.debug("Thread coordinator is missing clear_events/_clear_events")

    def wake_waiting_threads(self, *event_names: str) -> None:
        """Wake waiters via public-first coordinator compatibility dispatch."""
        coordinator = getattr(self._iface, "thread_coordinator", None)
        if coordinator is None or _is_unconfigured_mock_member(coordinator):
            logger.debug(
                "Thread coordinator is missing wake_waiting_threads/_wake_waiting_threads/set_event/_set_event"
            )
            return
        wake_waiting_threads = getattr(coordinator, "wake_waiting_threads", None)
        if callable(wake_waiting_threads) and not _is_unconfigured_mock_callable(
            wake_waiting_threads
        ):
            try:
                wake_waiting_threads(*event_names)
                return
            except Exception:  # noqa: BLE001 - non-critical wake stays best effort
                logger.debug(
                    "Error in thread_coordinator.wake_waiting_threads for %s",
                    event_names,
                    exc_info=True,
                )
        legacy_wake_waiting_threads = getattr(
            coordinator, "_wake_waiting_threads", None
        )
        if callable(legacy_wake_waiting_threads) and not _is_unconfigured_mock_callable(
            legacy_wake_waiting_threads
        ):
            try:
                legacy_wake_waiting_threads(*event_names)
                return
            except Exception:  # noqa: BLE001 - non-critical wake stays best effort
                logger.debug(
                    "Error in thread_coordinator._wake_waiting_threads for %s",
                    event_names,
                    exc_info=True,
                )
        set_event = getattr(coordinator, "set_event", None)
        if callable(set_event) and not _is_unconfigured_mock_callable(set_event):
            failed_events: list[str] = []
            for event_name in event_names:
                try:
                    set_event(event_name)
                except Exception:  # noqa: BLE001 - non-critical wake stays best effort
                    logger.debug(
                        "Error in thread_coordinator.set_event fallback for %s",
                        event_name,
                        exc_info=True,
                    )
                    failed_events.append(event_name)
            if not failed_events:
                return
            event_names = tuple(failed_events)
        legacy_set_event = getattr(coordinator, "_set_event", None)
        if callable(legacy_set_event) and not _is_unconfigured_mock_callable(
            legacy_set_event
        ):
            failed_events = []
            for event_name in event_names:
                try:
                    legacy_set_event(event_name)
                except Exception:  # noqa: BLE001 - non-critical wake stays best effort
                    logger.debug(
                        "Error in thread_coordinator._set_event fallback for %s",
                        event_name,
                        exc_info=True,
                    )
                    failed_events.append(event_name)
            if not failed_events:
                return
        logger.debug(
            "Thread coordinator is missing wake_waiting_threads/_wake_waiting_threads/set_event/_set_event"
        )


class _LifecycleErrorAccess:
    """Error-handler compatibility access owned by lifecycle collaborators."""

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind error-handler access to a specific interface."""
        self._iface = iface

    def resolve_hook(
        self, public_name: str, legacy_name: str
    ) -> Callable[..., object] | None:
        """Resolve an error-handler hook with public-first fallback behavior."""
        error_handler = getattr(self._iface, "error_handler", None)
        hook = getattr(error_handler, public_name, None)
        if callable(hook) and not _is_unconfigured_mock_callable(hook):
            return cast(Callable[..., object], hook)
        legacy_hook = getattr(error_handler, legacy_name, None)
        if callable(legacy_hook) and not _is_unconfigured_mock_callable(legacy_hook):
            return cast(Callable[..., object], legacy_hook)
        return None

    def safe_cleanup(self, cleanup: Callable[[], object], operation_name: str) -> None:
        """Run cleanup via resolved error-handler hook with best-effort fallback."""
        safe_cleanup = self.resolve_hook("safe_cleanup", "_safe_cleanup")
        cleanup_ran = False

        def _tracked_cleanup() -> object:
            nonlocal cleanup_ran
            cleanup_ran = True
            return cleanup()

        if safe_cleanup is not None:
            try:
                hook_result: object | None = None
                try:
                    hook_result = safe_cleanup(
                        func=_tracked_cleanup,
                        cleanup_name=operation_name,
                    )
                except TypeError as exc:
                    if not (
                        _is_unexpected_keyword_error(exc, "func")
                        or _is_unexpected_keyword_error(exc, "cleanup_name")
                    ):
                        raise
                    if cleanup_ran:
                        return
                    hook_result = safe_cleanup(_tracked_cleanup, operation_name)
                if cleanup_ran or bool(hook_result):
                    return
            except Exception:  # noqa: BLE001 - hook failure must not abort shutdown
                logger.debug(
                    "Error running safe_cleanup hook for %s",
                    operation_name,
                    exc_info=True,
                )
                if cleanup_ran:
                    return
        try:
            cleanup()
        except Exception:  # noqa: BLE001 - shutdown cleanup must remain best effort
            logger.debug("Error during %s", operation_name, exc_info=True)

    @staticmethod
    def _try_safe_execute_variants(
        safe_execute: Callable[..., object],
        tracked_func: Callable[[], object],
        *,
        error_msg: str,
        did_run: Callable[[], bool],
    ) -> tuple[bool, object | None]:
        """Attempt execute-hook signatures and report whether `tracked_func` ran.

        Tries signatures in order: ``(func, error_msg=...)``,
        ``(func, error_msg)``, then ``(func)``.
        Returns ``(True, result)`` only when ``tracked_func`` executed.
        """
        try:
            result = safe_execute(tracked_func, error_msg=error_msg)
            return did_run(), result
        except TypeError as exc:
            if not _is_unexpected_keyword_error(exc, "error_msg"):
                logger.debug(error_msg, exc_info=True)
                return did_run(), None
            if did_run():
                return True, None
            try:
                result = safe_execute(tracked_func, error_msg)
                if did_run():
                    return True, result
            except Exception:  # noqa: BLE001 - hook failures must not abort shutdown
                logger.debug(error_msg, exc_info=True)
                if did_run():
                    return True, None
            try:
                result = safe_execute(tracked_func)
                if did_run():
                    return True, result
            except Exception:  # noqa: BLE001 - hook failures must not abort shutdown
                logger.debug(error_msg, exc_info=True)
                if did_run():
                    return True, None
        except Exception:  # noqa: BLE001 - hook failures must not abort shutdown
            logger.debug(error_msg, exc_info=True)
            if did_run():
                return True, None
        return False, None

    def safe_execute(
        self,
        func: Callable[[], object],
        *,
        error_msg: str,
    ) -> object | None:
        """Run callable via resolved error-handler execute hook with fallback."""
        safe_execute = self.resolve_hook("safe_execute", "_safe_execute")
        func_ran = False

        def _tracked_func() -> object:
            nonlocal func_ran
            func_ran = True
            return func()

        if safe_execute is not None:
            hook_ran, hook_result = self._try_safe_execute_variants(
                safe_execute,
                _tracked_func,
                error_msg=error_msg,
                did_run=lambda: func_ran,
            )
            if hook_ran:
                return hook_result
        try:
            return func()
        except Exception:  # noqa: BLE001 - shutdown execution must remain best effort
            logger.debug(error_msg, exc_info=True)
            return None
