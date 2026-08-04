"""Shared lifecycle primitives for BLE runtime ownership."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_member,
    _iter_declared_callables,
    _iter_declared_members,
    _resolve_declared_callable,
)
from meshtastic.interfaces.ble.constants import logger
from meshtastic.interfaces.ble.failure_policy import (
    _BLEFailureDisposition,
    _log_ble_failure,
)
from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.ports import _BLEStateManagerPort
from meshtastic.interfaces.ble.state import ConnectionState
from meshtastic.interfaces.ble.utils import (
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


def _client_is_connected_compat(client: object) -> bool:
    """Return connected-state flag from public/legacy BLEClient members."""
    for candidate_name, candidate in _iter_declared_members(
        client, "isConnected", "is_connected", "_is_connected"
    ):
        if callable(candidate):
            try:
                connected = candidate()
            except Exception:  # noqa: BLE001 - connectivity probes stay best effort
                _log_ble_failure(
                    _BLEFailureDisposition.COMPATIBILITY_FALLBACK,
                    "Error probing BLE client connectivity via %s",
                    candidate_name,
                )
                continue
        else:
            connected = candidate
        if isinstance(connected, bool):
            return connected
    raise AttributeError(CLIENT_MISSING_CONNECTED_MSG)


class _LifecycleStateAccess:
    """Runtime state-manager access owned by lifecycle collaborators."""

    def __init__(self, target: _BLEStateManagerPort | object) -> None:
        """Bind compatibility access to a state manager or legacy interface."""
        self._state_manager = _get_declared_member(target, "_state_manager", target)

    def _read_typed(
        self,
        expected_type: type[object],
        *names: str,
    ) -> object:
        """Read a typed value from public/legacy members in precedence order."""
        for member_name, candidate in _iter_declared_members(
            self._state_manager, *names
        ):
            if callable(candidate):
                try:
                    result = candidate()
                except Exception:  # noqa: BLE001 - compatibility probe must fall through
                    _log_ble_failure(
                        _BLEFailureDisposition.COMPATIBILITY_FALLBACK,
                        "Error probing state manager %s()",
                        member_name,
                    )
                    continue
            else:
                result = candidate
            if isinstance(result, expected_type):
                return result
        raise AttributeError

    def _call_bool(
        self, missing_message: str, names: tuple[str, ...], *args: object
    ) -> bool:
        """Call public/legacy state-manager members until one returns ``bool``."""
        for member_name, candidate in _iter_declared_callables(
            self._state_manager, *names
        ):
            try:
                result = candidate(*args)
            except Exception:  # noqa: BLE001 - compatibility probe must fall through
                _log_ble_failure(
                    _BLEFailureDisposition.COMPATIBILITY_FALLBACK,
                    "Error calling state manager %s()",
                    member_name,
                )
                continue
            if isinstance(result, bool):
                return result
        raise AttributeError(missing_message)

    def is_connected(self) -> bool:
        """Return connected-state flag from public-first state-manager members."""
        try:
            return cast(bool, self._read_typed(bool, "is_connected", "_is_connected"))
        except AttributeError as exc:
            raise AttributeError(STATE_MANAGER_MISSING_CONNECTED_MSG) from exc

    def current_state(self) -> ConnectionState:
        """Return current connection state from public-first state-manager members."""
        try:
            return cast(
                ConnectionState,
                self._read_typed(ConnectionState, "current_state", "_current_state"),
            )
        except AttributeError as exc:
            raise AttributeError(STATE_MANAGER_MISSING_CURRENT_STATE_MSG) from exc

    def transition_to(self, new_state: ConnectionState) -> bool:
        """Transition state manager using public-first compatibility dispatch."""
        return self._call_bool(
            STATE_MANAGER_MISSING_TRANSITION_MSG,
            ("transition_to", "_transition_to"),
            new_state,
        )

    def reset_to_disconnected(self) -> bool:
        """Reset state manager to disconnected using public-first dispatch."""
        return self._call_bool(
            STATE_MANAGER_MISSING_RESET_MSG,
            ("reset_to_disconnected", "_reset_to_disconnected"),
        )

    def is_closing(self) -> bool:
        """Return closing-state flag from public-first state-manager members."""
        try:
            return cast(bool, self._read_typed(bool, "is_closing", "_is_closing"))
        except AttributeError:
            # Close/shutdown paths deliberately degrade when optional state hooks
            # are absent on legacy or partial collaborators.
            return False

    def client_is_connected(self, client: "BLEClient") -> bool:
        """Return client connectivity using compatibility member probing."""
        return _client_is_connected_compat(client)


class _LifecycleThreadAccess:
    """Thread/event compatibility access owned by lifecycle collaborators.

    Critical operations (thread creation/start) propagate collaborator failures.
    Shutdown/recovery operations are best effort and fall through from current
    to legacy hook names before logging a missing-hook message.
    """

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind thread-coordinator access to a specific interface."""
        self._iface = iface

    def _coordinator(self) -> object | None:
        """Return the explicitly declared thread coordinator when available."""
        return _get_declared_member(self._iface, "thread_coordinator")

    def _required_callable(
        self, public_name: str, legacy_name: str
    ) -> Callable[..., object]:
        """Resolve a required current/legacy coordinator callable."""
        coordinator = self._coordinator()
        resolved = _resolve_declared_callable(
            coordinator, public_name, legacy_name
        )
        if resolved is None:
            raise AttributeError(
                THREAD_COORDINATOR_MISSING_FMT % (public_name, legacy_name)
            )
        return cast(Callable[..., object], resolved)

    def _best_effort_call(
        self,
        public_name: str,
        legacy_name: str,
        *args: object,
        **kwargs: object,
    ) -> bool:
        """Try current/legacy coordinator hooks, logging and falling through."""
        coordinator = self._coordinator()
        if coordinator is None:
            return False
        for member_name, hook in _iter_declared_callables(
            coordinator, public_name, legacy_name
        ):
            try:
                hook(*args, **kwargs)
            except Exception:  # noqa: BLE001 - non-critical lifecycle hook
                _log_ble_failure(
                    _BLEFailureDisposition.BEST_EFFORT,
                    "Error in thread_coordinator.%s",
                    member_name,
                )
                continue
            return True
        return False

    def create_thread(
        self,
        *,
        target: Callable[..., object],
        name: str,
        daemon: bool,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
    ) -> ThreadLike:
        """Create a thread through the current/legacy coordinator contract."""
        create_thread = self._required_callable("create_thread", "_create_thread")
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

    def start_thread(self, thread: object) -> None:
        """Start a thread through the current/legacy coordinator contract."""
        self._required_callable("start_thread", "_start_thread")(thread)

    def join_thread(self, thread: object, *, timeout: float | None) -> None:
        """Join a thread through coordinator hooks with direct-thread fallback."""
        if self._best_effort_call(
            "join_thread", "_join_thread", thread, timeout=timeout
        ):
            return
        thread_join = _resolve_declared_callable(thread, "join")
        if thread_join is not None:
            try:
                thread_join(timeout=timeout)
            except Exception:  # noqa: BLE001 - non-critical join stays best effort
                _log_ble_failure(
                    _BLEFailureDisposition.BEST_EFFORT,
                    "Error in thread.join for %r",
                    thread,
                )
            return
        logger.debug("Thread coordinator is missing join_thread/_join_thread")

    def set_event(self, event_name: str) -> None:
        """Set a coordinator event using current/legacy hooks."""
        if not self._best_effort_call("set_event", "_set_event", event_name):
            logger.debug("Thread coordinator is missing set_event/_set_event")

    def clear_events(self, *event_names: str) -> None:
        """Clear coordinator events using current/legacy hooks."""
        if not self._best_effort_call("clear_events", "_clear_events", *event_names):
            logger.debug("Thread coordinator is missing clear_events/_clear_events")

    def wake_waiting_threads(self, *event_names: str) -> None:
        """Wake waiters with bulk hooks, then fall back to per-event hooks."""
        coordinator = self._coordinator()
        if coordinator is None:
            logger.debug(
                "Thread coordinator is missing wake_waiting_threads/_wake_waiting_threads/set_event/_set_event"
            )
            return
        if self._best_effort_call(
            "wake_waiting_threads", "_wake_waiting_threads", *event_names
        ):
            return

        remaining = list(event_names)
        for member_name, set_event in _iter_declared_callables(
            coordinator, "set_event", "_set_event"
        ):
            failed: list[str] = []
            for event_name in remaining:
                try:
                    set_event(event_name)
                except Exception:  # noqa: BLE001 - non-critical wake stays best effort
                    _log_ble_failure(
                        _BLEFailureDisposition.BEST_EFFORT,
                        "Error in thread_coordinator.%s fallback for %s",
                        member_name,
                        event_name,
                    )
                    failed.append(event_name)
            if not failed:
                return
            remaining = failed
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
        error_handler = _get_declared_member(self._iface, "error_handler")
        hook = _resolve_declared_callable(error_handler, public_name, legacy_name)
        return cast(Callable[..., object], hook) if hook is not None else None

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
                _log_ble_failure(
                    _BLEFailureDisposition.BEST_EFFORT,
                    "Error running safe_cleanup hook for %s",
                    operation_name,
                )
                if cleanup_ran:
                    return
        try:
            cleanup()
        except Exception:  # noqa: BLE001 - shutdown cleanup must remain best effort
            _log_ble_failure(
                _BLEFailureDisposition.BEST_EFFORT,
                "Error during %s",
                operation_name,
            )

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
                _log_ble_failure(
                    _BLEFailureDisposition.BEST_EFFORT,
                    error_msg,
                )
                return did_run(), None
            if did_run():
                return True, None
            try:
                result = safe_execute(tracked_func, error_msg)
                if did_run():
                    return True, result
            except Exception:  # noqa: BLE001 - hook failures must not abort shutdown
                _log_ble_failure(
                    _BLEFailureDisposition.BEST_EFFORT,
                    error_msg,
                )
                if did_run():
                    return True, None
            try:
                result = safe_execute(tracked_func)
                if did_run():
                    return True, result
            except Exception:  # noqa: BLE001 - hook failures must not abort shutdown
                _log_ble_failure(
                    _BLEFailureDisposition.BEST_EFFORT,
                    error_msg,
                )
                if did_run():
                    return True, None
        except Exception:  # noqa: BLE001 - hook failures must not abort shutdown
            _log_ble_failure(
                _BLEFailureDisposition.BEST_EFFORT,
                error_msg,
            )
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
            _log_ble_failure(
                _BLEFailureDisposition.BEST_EFFORT,
                error_msg,
            )
            return None
