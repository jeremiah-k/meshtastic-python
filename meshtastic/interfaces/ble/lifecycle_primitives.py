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
            connected = candidate()
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
        if not _is_unconfigured_mock_member(public_is_connected) and isinstance(
            public_is_connected, bool
        ):
            return public_is_connected
        legacy_is_connected = getattr(self._iface._state_manager, "_is_connected", None)
        if not _is_unconfigured_mock_member(legacy_is_connected) and isinstance(
            legacy_is_connected, bool
        ):
            return legacy_is_connected
        raise AttributeError(STATE_MANAGER_MISSING_CONNECTED_MSG)

    def current_state(self) -> ConnectionState:
        """Return current connection state from public-first state-manager members."""
        public_state = getattr(self._iface._state_manager, "current_state", None)
        if callable(public_state) and not _is_unconfigured_mock_callable(public_state):
            result = public_state()
            if isinstance(result, ConnectionState):
                return result
        if not _is_unconfigured_mock_member(public_state) and isinstance(
            public_state, ConnectionState
        ):
            return public_state
        legacy_state = getattr(self._iface._state_manager, "_current_state", None)
        if callable(legacy_state) and not _is_unconfigured_mock_callable(legacy_state):
            result = legacy_state()
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
            return bool(public_transition(new_state))
        legacy_transition = getattr(self._iface._state_manager, "_transition_to", None)
        if callable(legacy_transition) and not _is_unconfigured_mock_callable(
            legacy_transition
        ):
            return bool(legacy_transition(new_state))
        raise AttributeError(STATE_MANAGER_MISSING_TRANSITION_MSG)

    def reset_to_disconnected(self) -> bool:
        """Reset state manager to disconnected using public-first dispatch."""
        public_reset = getattr(
            self._iface._state_manager, "reset_to_disconnected", None
        )
        if callable(public_reset) and not _is_unconfigured_mock_callable(public_reset):
            return bool(public_reset())
        legacy_reset = getattr(
            self._iface._state_manager, "_reset_to_disconnected", None
        )
        if callable(legacy_reset) and not _is_unconfigured_mock_callable(legacy_reset):
            return bool(legacy_reset())
        raise AttributeError(STATE_MANAGER_MISSING_RESET_MSG)

    def is_closing(self) -> bool:
        """Return closing-state flag from public-first state-manager members."""
        public_is_closing = getattr(self._iface._state_manager, "is_closing", None)
        if not _is_unconfigured_mock_member(public_is_closing) and isinstance(
            public_is_closing, bool
        ):
            return public_is_closing
        legacy_is_closing = getattr(self._iface._state_manager, "_is_closing", None)
        if not _is_unconfigured_mock_member(legacy_is_closing) and isinstance(
            legacy_is_closing, bool
        ):
            return legacy_is_closing
        return False

    def client_is_connected(self, client: "BLEClient") -> bool:
        """Return connected-state flag from public/legacy BLEClient members."""
        return _client_is_connected_compat(client)


class _LifecycleThreadAccess:
    """Thread/event compatibility access owned by lifecycle collaborators."""

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
        create_thread = getattr(self._iface.thread_coordinator, "create_thread", None)
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
        legacy_create_thread = getattr(
            self._iface.thread_coordinator, "_create_thread", None
        )
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
        start_thread = getattr(self._iface.thread_coordinator, "start_thread", None)
        if callable(start_thread) and not _is_unconfigured_mock_callable(start_thread):
            start_thread(thread)
            return
        legacy_start_thread = getattr(
            self._iface.thread_coordinator, "_start_thread", None
        )
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
        join_thread = getattr(self._iface.thread_coordinator, "join_thread", None)
        if callable(join_thread) and not _is_unconfigured_mock_callable(join_thread):
            join_thread(thread, timeout=timeout)
            return
        legacy_join_thread = getattr(self._iface.thread_coordinator, "_join_thread", None)
        if callable(legacy_join_thread) and not _is_unconfigured_mock_callable(
            legacy_join_thread
        ):
            legacy_join_thread(thread, timeout=timeout)
            return
        thread_join = getattr(thread, "join", None)
        if callable(thread_join) and not _is_unconfigured_mock_callable(thread_join):
            thread_join(timeout=timeout)
            return
        logger.debug("Thread coordinator is missing join_thread/_join_thread")

    def set_event(self, event_name: str) -> None:
        """Set event via public-first coordinator compatibility dispatch."""
        set_event = getattr(self._iface.thread_coordinator, "set_event", None)
        if callable(set_event) and not _is_unconfigured_mock_callable(set_event):
            set_event(event_name)
            return
        legacy_set_event = getattr(self._iface.thread_coordinator, "_set_event", None)
        if callable(legacy_set_event) and not _is_unconfigured_mock_callable(
            legacy_set_event
        ):
            legacy_set_event(event_name)
            return
        logger.debug("Thread coordinator is missing set_event/_set_event")

    def clear_events(self, *event_names: str) -> None:
        """Clear events via public-first coordinator compatibility dispatch."""
        clear_events = getattr(self._iface.thread_coordinator, "clear_events", None)
        if callable(clear_events) and not _is_unconfigured_mock_callable(clear_events):
            clear_events(*event_names)
            return
        legacy_clear_events = getattr(
            self._iface.thread_coordinator, "_clear_events", None
        )
        if callable(legacy_clear_events) and not _is_unconfigured_mock_callable(
            legacy_clear_events
        ):
            legacy_clear_events(*event_names)
            return
        logger.debug("Thread coordinator is missing clear_events/_clear_events")

    def wake_waiting_threads(self, *event_names: str) -> None:
        """Wake waiters via public-first coordinator compatibility dispatch."""
        wake_waiting_threads = getattr(
            self._iface.thread_coordinator, "wake_waiting_threads", None
        )
        if callable(wake_waiting_threads) and not _is_unconfigured_mock_callable(
            wake_waiting_threads
        ):
            wake_waiting_threads(*event_names)
            return
        legacy_wake_waiting_threads = getattr(
            self._iface.thread_coordinator, "_wake_waiting_threads", None
        )
        if callable(legacy_wake_waiting_threads) and not _is_unconfigured_mock_callable(
            legacy_wake_waiting_threads
        ):
            legacy_wake_waiting_threads(*event_names)
            return
        logger.debug(
            "Thread coordinator is missing wake_waiting_threads/_wake_waiting_threads"
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
                try:
                    safe_cleanup(func=_tracked_cleanup, cleanup_name=operation_name)
                except TypeError as exc:
                    if not (
                        _is_unexpected_keyword_error(exc, "func")
                        or _is_unexpected_keyword_error(exc, "cleanup_name")
                    ):
                        raise
                    safe_cleanup(_tracked_cleanup, operation_name)
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
            try:
                return safe_execute(_tracked_func, error_msg=error_msg)
            except TypeError as exc:
                if _is_unexpected_keyword_error(exc, "error_msg"):
                    try:
                        return safe_execute(_tracked_func, error_msg)
                    except Exception:  # noqa: BLE001 - hook failures must not abort shutdown
                        logger.debug(error_msg, exc_info=True)
                        if func_ran:
                            return None
                    try:
                        return safe_execute(_tracked_func)
                    except Exception:  # noqa: BLE001 - hook failures must not abort shutdown
                        logger.debug(error_msg, exc_info=True)
                        if func_ran:
                            return None
                else:
                    logger.debug(error_msg, exc_info=True)
                    if func_ran:
                        return None
            except Exception:  # noqa: BLE001 - hook failures must not abort shutdown
                logger.debug(error_msg, exc_info=True)
                if func_ran:
                    return None
        try:
            return func()
        except Exception:  # noqa: BLE001 - shutdown execution must remain best effort
            logger.debug(error_msg, exc_info=True)
            return None
