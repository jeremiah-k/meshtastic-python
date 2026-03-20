"""Receive-loop and recovery helpers for BLE interface orchestration."""

import math
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    ERROR_READING_BLE,
    FROMRADIO_UUID,
    GATT_IO_TIMEOUT,
    READ_TRIGGER_EVENT,
    RECEIVE_RECOVERY_MAX_BACKOFF_SEC,
    RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD,
    RECEIVE_RECOVERY_STABILITY_RESET_SEC,
    RECONNECTED_EVENT,
    BLEConfig,
    logger,
)
from meshtastic.interfaces.ble.errors import DecodeError
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_callable,
    _is_unconfigured_mock_member,
    _sleep,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.coordination import ThreadCoordinator
    from meshtastic.interfaces.ble.interface import BLEInterface

COORDINATOR_WAIT_FALLBACK_SLEEP_SEC = 0.001
RECEIVE_THREAD_FATAL_REASON = "receive_thread_fatal"
_INTERFACE_MODULE_NAME = "meshtastic.interfaces.ble.interface"
_DEFAULT_RECURSIVE_IFACE_RECEIVE_HOOK_QUALNAMES = {
    "_should_run_receive_loop": "BLEInterface._should_run_receive_loop",
    "_set_receive_wanted": "BLEInterface._set_receive_wanted",
    "_handle_disconnect": "BLEInterface._handle_disconnect",
    "_start_receive_thread": "BLEInterface._start_receive_thread",
    "_handle_read_loop_disconnect": "BLEInterface._handle_read_loop_disconnect",
    "_read_from_radio_with_retries": "BLEInterface._read_from_radio_with_retries",
    "_handle_transient_read_error": "BLEInterface._handle_transient_read_error",
    "_log_empty_read_warning": "BLEInterface._log_empty_read_warning",
    "close": "BLEInterface.close",
}


class BLEReceiveRecoveryController:
    """Instance-bound collaborator for BLE receive-loop and recovery paths.

    Parameters
    ----------
    iface : BLEInterface
        Interface instance whose receive-loop/recovery behavior is coordinated.

    Attributes
    ----------
    _iface : BLEInterface
        Bound interface used for receive-loop state and hook dispatch.
    _dispatching_iface_receive_hooks : threading.local
        Per-thread recursion guard storage for interface receive-hook overrides.
    """

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind receive/recovery helpers to a specific interface instance.

        Parameters
        ----------
        iface : BLEInterface
            Interface instance whose receive/recovery lifecycle is managed.

        Returns
        -------
        None
            Initializes bound controller state.
        """
        self._iface = iface
        self._dispatching_iface_receive_hooks = threading.local()

    def _dispatching_hooks(self) -> set[str]:
        """Return the per-thread receive-hook reentrancy guard set.

        Returns
        -------
        set[str]
            Mutable per-thread set of currently-dispatching hook names.
        """
        hooks = getattr(self._dispatching_iface_receive_hooks, "value", None)
        if hooks is None:
            hooks = set()
            self._dispatching_iface_receive_hooks.value = hooks
        return hooks

    @staticmethod
    def _as_usable_callable(
        candidate: object | None,
    ) -> Callable[..., object] | None:
        """Return a callable hook when candidate is usable.

        Parameters
        ----------
        candidate : object | None
            Candidate hook object.

        Returns
        -------
        Callable[..., object] | None
            Callable candidate when it is not an unconfigured mock; otherwise
            ``None``.
        """
        if callable(candidate) and not _is_unconfigured_mock_callable(candidate):
            return cast(Callable[..., object], candidate)
        return None

    def _resolve_private_public_callable(
        self,
        owner: object,
        *,
        private_name: str,
        public_name: str,
    ) -> Callable[..., object] | None:
        """Resolve private/public hook names to the first usable callable.

        Parameters
        ----------
        owner : object
            Object that may expose private/public hook variants.
        private_name : str
            Private hook attribute name to probe first.
        public_name : str
            Public hook attribute name used as fallback when private hook is
            unavailable.

        Returns
        -------
        Callable[..., object] | None
            First usable callable hook, preferring private then public; else
            ``None``.
        """
        private_hook = self._as_usable_callable(getattr(owner, private_name, None))
        if private_hook is not None:
            return private_hook
        return self._as_usable_callable(getattr(owner, public_name, None))

    @classmethod
    def _is_unusable_mock_value(cls, value: object | None) -> bool:
        """Return whether value is absent or an unconfigured mock placeholder."""
        if value is None or _is_unconfigured_mock_member(value):
            return True
        return callable(value) and _is_unconfigured_mock_callable(value)

    def _get_lifecycle_controller(self) -> object | None:
        """Return lifecycle controller when available on the bound interface."""
        get_lifecycle_controller = getattr(
            self._iface, "_get_lifecycle_controller", None
        )
        get_lifecycle_controller_fn = self._as_usable_callable(get_lifecycle_controller)
        if get_lifecycle_controller_fn is not None:
            try:
                controller = get_lifecycle_controller_fn()
            except Exception:  # noqa: BLE001 - probe remains best effort
                return None
            if self._is_unusable_mock_value(controller):
                return None
            return controller
        return None

    def _has_ever_connected_session(self) -> bool:
        """Return mock-safe ever-connected state for receive-loop behavior."""
        lifecycle_controller = self._get_lifecycle_controller()
        if lifecycle_controller is not None:
            has_ever_connected_session_fn = self._resolve_private_public_callable(
                lifecycle_controller,
                private_name="_has_ever_connected_session",
                public_name="has_ever_connected_session",
            )
            if has_ever_connected_session_fn is not None:
                try:
                    result = has_ever_connected_session_fn()
                except Exception:  # noqa: BLE001 - probe remains best effort
                    logger.debug(
                        "Probe has_ever_connected_session_fn failed",
                        exc_info=True,
                    )
                else:
                    return result if isinstance(result, bool) else False
        raw_ever_connected = getattr(self._iface, "_ever_connected", False)
        if _is_unconfigured_mock_member(raw_ever_connected):
            return False
        return raw_ever_connected is True

    @staticmethod
    def _normalize_bool_probe(raw_value: object) -> bool:
        """Normalize a bool-like probe while filtering mock placeholders."""
        if _is_unconfigured_mock_member(raw_value):
            return False
        if callable(raw_value) and not _is_unconfigured_mock_callable(raw_value):
            try:
                result = raw_value()
                return result if isinstance(result, bool) else False
            except Exception:  # noqa: BLE001 - probe remains best effort
                return False
        if isinstance(raw_value, bool):
            return raw_value
        return False

    @staticmethod
    def _call_bool_hook(
        hook: Callable[..., object], *args: object, **kwargs: object
    ) -> bool:
        """Call a hook and return True only if result is a real bool True."""
        try:
            result = hook(*args, **kwargs)
        except Exception:  # noqa: BLE001 - hook probes remain best effort
            return False
        return result if isinstance(result, bool) else False

    def _is_connection_closing_locked(self) -> bool:
        """Return whether connection-closing probes indicate closing while lock is held."""
        iface = self._iface
        state_manager = getattr(iface, "_state_manager", None)
        state_is_closing: bool | None = None
        if state_manager is None:
            raw_is_closing = getattr(iface, "_is_connection_closing", False)
            state_is_closing = self._normalize_bool_probe(raw_is_closing)
        else:
            raw_is_closing = getattr(state_manager, "is_closing", None)
            if callable(raw_is_closing) and not _is_unconfigured_mock_callable(
                raw_is_closing
            ):
                try:
                    result = raw_is_closing()
                    if isinstance(result, bool):
                        state_is_closing = result
                except Exception:  # noqa: BLE001 - closing probe must remain best effort
                    state_is_closing = None
            elif not _is_unconfigured_mock_member(raw_is_closing) and isinstance(
                raw_is_closing, bool
            ):
                state_is_closing = raw_is_closing
            if state_is_closing is None:
                legacy_is_closing = getattr(state_manager, "_is_closing", None)
                state_is_closing = self._normalize_bool_probe(legacy_is_closing)

        raw_closed = getattr(iface, "_closed", False)
        closed = self._normalize_bool_probe(raw_closed)
        return state_is_closing or closed

    def _is_connection_closing(self) -> bool:
        """Return whether receive-loop behavior should treat the interface as closing."""
        lifecycle_controller = self._get_lifecycle_controller()
        if lifecycle_controller is not None:
            is_connection_closing_fn = self._resolve_private_public_callable(
                lifecycle_controller,
                private_name="_is_connection_closing",
                public_name="is_connection_closing",
            )
            if is_connection_closing_fn is not None:
                try:
                    result = is_connection_closing_fn()
                except Exception:  # noqa: BLE001 - probe remains best effort
                    logger.debug(
                        "Probe is_connection_closing_fn failed",
                        exc_info=True,
                    )
                else:
                    return result if isinstance(result, bool) else False
        iface = self._iface
        with iface._state_lock:
            return self._is_connection_closing_locked()

    @staticmethod
    def _coordinator_wait_for_event(
        coordinator: "ThreadCoordinator",
        event_name: str,
        timeout: float | None,
    ) -> bool:
        """Wait for a coordinator event using compatibility dispatch."""
        wait_for_event = getattr(coordinator, "wait_for_event", None)
        if callable(wait_for_event) and not _is_unconfigured_mock_callable(
            wait_for_event
        ):
            result = wait_for_event(event_name, timeout=timeout)
            return result if isinstance(result, bool) else False
        legacy_wait_for_event = getattr(coordinator, "_wait_for_event", None)
        if callable(legacy_wait_for_event) and not _is_unconfigured_mock_callable(
            legacy_wait_for_event
        ):
            result = legacy_wait_for_event(event_name, timeout=timeout)
            return result if isinstance(result, bool) else False
        if timeout is None:
            _sleep(COORDINATOR_WAIT_FALLBACK_SLEEP_SEC)
        elif timeout > 0:
            _sleep(min(timeout, COORDINATOR_WAIT_FALLBACK_SLEEP_SEC))
        return False

    @staticmethod
    def _coordinator_check_and_clear_event(
        coordinator: "ThreadCoordinator",
        event_name: str,
    ) -> bool:
        """Check and clear a coordinator event using compatibility dispatch."""
        check_and_clear_event = getattr(coordinator, "check_and_clear_event", None)
        if callable(check_and_clear_event) and not _is_unconfigured_mock_callable(
            check_and_clear_event
        ):
            result = check_and_clear_event(event_name)
            return result if isinstance(result, bool) else False
        legacy_check_and_clear_event = getattr(
            coordinator, "_check_and_clear_event", None
        )
        if callable(
            legacy_check_and_clear_event
        ) and not _is_unconfigured_mock_callable(legacy_check_and_clear_event):
            result = legacy_check_and_clear_event(event_name)
            return result if isinstance(result, bool) else False
        return False

    @staticmethod
    def _coordinator_clear_event(
        coordinator: "ThreadCoordinator", event_name: str
    ) -> None:
        """Clear a coordinator event using compatibility dispatch."""
        clear_events = getattr(coordinator, "clear_events", None)
        if callable(clear_events) and not _is_unconfigured_mock_callable(clear_events):
            clear_events(event_name)
            return
        legacy_clear_events = getattr(coordinator, "_clear_events", None)
        if callable(legacy_clear_events) and not _is_unconfigured_mock_callable(
            legacy_clear_events
        ):
            legacy_clear_events(event_name)
            return
        clear_event = getattr(coordinator, "clear_event", None)
        if callable(clear_event) and not _is_unconfigured_mock_callable(clear_event):
            clear_event(event_name)
            return
        legacy_clear_event = getattr(coordinator, "_clear_event", None)
        if callable(legacy_clear_event) and not _is_unconfigured_mock_callable(
            legacy_clear_event
        ):
            legacy_clear_event(event_name)

    def _should_run_receive_loop(self) -> bool:
        """Return whether the lifecycle currently wants receive-loop execution."""
        override_should_run_receive_loop = self._resolve_iface_receive_hook_override(
            "_should_run_receive_loop"
        )
        if callable(override_should_run_receive_loop):
            try:
                result = override_should_run_receive_loop()
            except Exception:
                logger.error(
                    "Receive-loop override should_run_receive_loop raised",
                    exc_info=True,
                )
                raise
            return result if isinstance(result, bool) else False
        lifecycle_controller = self._get_lifecycle_controller()
        if lifecycle_controller is not None:
            should_run_receive_loop_fn = self._resolve_private_public_callable(
                lifecycle_controller,
                private_name="_should_run_receive_loop",
                public_name="should_run_receive_loop",
            )
            if should_run_receive_loop_fn is not None:
                try:
                    result = should_run_receive_loop_fn()
                except Exception:
                    logger.error(
                        "Lifecycle should_run_receive_loop raised",
                        exc_info=True,
                    )
                    raise
                return result if isinstance(result, bool) else False
        return False

    def _set_receive_wanted(self, *, want_receive: bool) -> None:
        """Set receive-loop intent through lifecycle controller or legacy iface hook."""
        override_set_receive_wanted = self._resolve_iface_receive_hook_override(
            "_set_receive_wanted"
        )
        if callable(override_set_receive_wanted):
            override_set_receive_wanted(want_receive=want_receive)
            return
        lifecycle_controller = self._get_lifecycle_controller()
        if lifecycle_controller is not None:
            set_receive_wanted_fn = self._resolve_private_public_callable(
                lifecycle_controller,
                private_name="_set_receive_wanted",
                public_name="set_receive_wanted",
            )
            if set_receive_wanted_fn is not None:
                set_receive_wanted_fn(want_receive=want_receive)

    def _handle_disconnect(
        self,
        disconnect_reason: str,
        *,
        client: BLEClient | None,
    ) -> bool:
        """Invoke disconnect handling through lifecycle controller or legacy hook."""
        override_handle_disconnect = self._resolve_iface_receive_hook_override(
            "_handle_disconnect"
        )
        if callable(override_handle_disconnect):
            try:
                result = override_handle_disconnect(
                    disconnect_reason,
                    client=client,
                )
            except Exception:  # noqa: BLE001 - disconnect hook must surface failures
                logger.warning(
                    "Disconnect handler override raised",
                    exc_info=True,
                )
                raise
            return result if isinstance(result, bool) else False
        lifecycle_controller = self._get_lifecycle_controller()
        if lifecycle_controller is not None:
            handle_disconnect_fn = self._resolve_private_public_callable(
                lifecycle_controller,
                private_name="_handle_disconnect",
                public_name="handle_disconnect",
            )
            if handle_disconnect_fn is None:
                return False
            try:
                result = handle_disconnect_fn(
                    disconnect_reason,
                    client=client,
                )
            except Exception:  # noqa: BLE001 - disconnect hook must surface failures
                logger.warning(
                    "Lifecycle disconnect handler raised",
                    exc_info=True,
                )
                raise
            return result if isinstance(result, bool) else False
        return False

    def _start_receive_thread(self, *, name: str, reset_recovery: bool) -> None:
        """Start receive thread through lifecycle controller or legacy hook."""
        override_start_receive_thread = self._resolve_iface_receive_hook_override(
            "_start_receive_thread"
        )
        if callable(override_start_receive_thread):
            override_start_receive_thread(name=name, reset_recovery=reset_recovery)
            return
        lifecycle_controller = self._get_lifecycle_controller()
        if lifecycle_controller is not None:
            start_receive_thread_fn = self._resolve_private_public_callable(
                lifecycle_controller,
                private_name="_start_receive_thread",
                public_name="start_receive_thread",
            )
            if start_receive_thread_fn is None:
                return
            start_receive_thread_fn(
                name=name,
                reset_recovery=reset_recovery,
            )

    def _close_after_fatal_read(self) -> None:
        """Close interface via lifecycle collaborator after fatal read failures."""
        iface = self._iface
        override_close = self._resolve_iface_receive_hook_override("close")
        if callable(override_close):
            override_close()
            return
        lifecycle_controller = self._get_lifecycle_controller()
        if lifecycle_controller is not None:
            close_fn = self._resolve_private_public_callable(
                lifecycle_controller,
                private_name="_close",
                public_name="close",
            )
            if close_fn is not None:
                from meshtastic.interfaces.ble import interface as interface_mod

                close_fn(
                    management_shutdown_wait_timeout=(
                        interface_mod._MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS
                    ),
                    management_wait_poll_seconds=interface_mod._MANAGEMENT_CONNECT_WAIT_POLL_SECONDS,
                )
                return
        from meshtastic.interfaces.ble import interface as interface_mod
        from meshtastic.interfaces.ble import lifecycle_service as lifecycle_service_mod

        lifecycle_service_mod.BLELifecycleService._close(
            iface,
            management_shutdown_wait_timeout=(
                interface_mod._MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS
            ),
            management_wait_poll_seconds=interface_mod._MANAGEMENT_CONNECT_WAIT_POLL_SECONDS,
        )

    @staticmethod
    def _is_default_iface_receive_hook(method_name: str, hook: object) -> bool:
        """Return whether a hook resolves to the default BLEInterface shim."""
        expected_qualname = _DEFAULT_RECURSIVE_IFACE_RECEIVE_HOOK_QUALNAMES.get(
            method_name
        )
        if expected_qualname is None:
            return False
        hook_func = getattr(hook, "__func__", hook)
        return (
            getattr(hook_func, "__module__", None) == _INTERFACE_MODULE_NAME
            and getattr(hook_func, "__qualname__", None) == expected_qualname
        )

    def _resolve_iface_receive_hook_override(
        self, method_name: str
    ) -> Callable[..., object] | None:
        """Resolve instance/class override while avoiding default recursion shims."""
        if method_name in self._dispatching_hooks():
            return None

        def _wrap_override(
            override: Callable[..., object],
        ) -> Callable[..., object]:
            def _guarded_override(*args: object, **kwargs: object) -> object:
                if method_name in self._dispatching_hooks():
                    return None
                self._dispatching_hooks().add(method_name)
                try:
                    return override(*args, **kwargs)
                finally:
                    self._dispatching_hooks().discard(method_name)

            return _guarded_override

        iface = self._iface
        instance_override = iface.__dict__.get(method_name)
        if callable(instance_override) and not _is_unconfigured_mock_callable(
            instance_override
        ):
            if self._is_default_iface_receive_hook(method_name, instance_override):
                return None
            return _wrap_override(cast(Callable[..., object], instance_override))
        class_or_subclass_override = getattr(iface, method_name, None)
        if callable(class_or_subclass_override) and not _is_unconfigured_mock_callable(
            class_or_subclass_override
        ):
            if self._is_default_iface_receive_hook(
                method_name,
                class_or_subclass_override,
            ):
                return None
            return _wrap_override(
                cast(Callable[..., object], class_or_subclass_override)
            )
        return None

    def handle_read_loop_disconnect(
        self, error_message: str, previous_client: BLEClient
    ) -> bool:
        """Handle read-loop disconnect logic for the bound interface."""
        iface = self._iface
        override_handle_read_loop_disconnect = (
            self._resolve_iface_receive_hook_override("_handle_read_loop_disconnect")
        )
        if callable(override_handle_read_loop_disconnect):
            try:
                result = override_handle_read_loop_disconnect(
                    error_message,
                    previous_client,
                )
            except Exception:  # noqa: BLE001 - disconnect hook must surface failures
                logger.warning(
                    "Read-loop disconnect override raised",
                    exc_info=True,
                )
                raise
            return result if isinstance(result, bool) else False
        logger.debug("Device disconnected: %s", error_message)
        should_continue = self._handle_disconnect(
            f"read_loop: {error_message}",
            client=previous_client,
        )
        iface._read_retry_count = 0
        if not should_continue:
            self._set_receive_wanted(want_receive=False)
        return should_continue

    def _should_poll_without_notify(self) -> bool:
        """Return whether fallback polling is allowed without notify callbacks."""
        iface = self._iface
        with iface._state_lock:
            notify_enabled: object = getattr(iface, "_fromnum_notify_enabled", False)
            is_unconfigured_member = getattr(
                iface, "_is_unconfigured_mock_member", None
            )
            if callable(is_unconfigured_member) and not _is_unconfigured_mock_callable(
                is_unconfigured_member
            ):
                try:
                    if bool(is_unconfigured_member("_fromnum_notify_enabled")):
                        notify_enabled = False
                except Exception:  # noqa: BLE001 - probe remains best effort
                    notify_enabled = False
            if _is_unconfigured_mock_member(notify_enabled):
                notify_enabled = False
            return not bool(notify_enabled)

    def _wait_for_read_trigger(
        self,
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
        wait_for_event: (
            Callable[["ThreadCoordinator", str, float | None], bool] | None
        ) = None,
        check_and_clear_event: Callable[["ThreadCoordinator", str], bool] | None = None,
        clear_event: Callable[["ThreadCoordinator", str], None] | None = None,
    ) -> tuple[bool, bool]:
        """Wait for read trigger and compute fallback poll mode."""
        wait_for_runtime_event: Callable[["ThreadCoordinator", str, float | None], bool]
        if wait_for_event is not None:
            wait_for_runtime_event = wait_for_event
        else:

            def _wait_for_runtime_event(
                target_coordinator: "ThreadCoordinator",
                event_name: str,
                timeout: float | None,
            ) -> bool:
                return self._coordinator_wait_for_event(
                    target_coordinator,
                    event_name,
                    timeout=timeout,
                )

            wait_for_runtime_event = _wait_for_runtime_event

        check_and_clear_runtime_event = check_and_clear_event or (
            self._coordinator_check_and_clear_event
        )
        clear_runtime_event = clear_event or self._coordinator_clear_event

        event_signaled = wait_for_runtime_event(
            coordinator,
            READ_TRIGGER_EVENT,
            wait_timeout,
        )
        poll_without_notify = False
        ever_connected = self._has_ever_connected_session()
        if not event_signaled:
            if ever_connected and check_and_clear_runtime_event(
                coordinator,
                RECONNECTED_EVENT,
            ):
                logger.debug("Detected reconnection, resuming normal operation")
            poll_without_notify = self._should_poll_without_notify()
            return poll_without_notify, poll_without_notify

        clear_runtime_event(coordinator, READ_TRIGGER_EVENT)
        return True, poll_without_notify

    def _snapshot_client_state(self) -> tuple[BLEClient | None, bool, bool, bool]:
        """Snapshot client and gating flags needed by the read loop."""
        iface = self._iface
        with iface._state_lock:
            client = iface.client
            state_is_connecting = getattr(iface._state_manager, "is_connecting", None)
            if callable(state_is_connecting) and not _is_unconfigured_mock_callable(
                state_is_connecting
            ):
                try:
                    connecting_result = state_is_connecting()
                except Exception:  # noqa: BLE001 - snapshot probe must remain best effort
                    logger.debug(
                        "Error probing state manager is_connecting()",
                        exc_info=True,
                    )
                    is_connecting = False
                else:
                    is_connecting = (
                        connecting_result
                        if isinstance(connecting_result, bool)
                        else False
                    )
            elif not _is_unconfigured_mock_member(state_is_connecting) and isinstance(
                state_is_connecting, bool
            ):
                is_connecting = state_is_connecting
            else:
                legacy_is_connecting = getattr(
                    iface._state_manager, "_is_connecting", None
                )
                if callable(
                    legacy_is_connecting
                ) and not _is_unconfigured_mock_callable(legacy_is_connecting):
                    try:
                        connecting_result = legacy_is_connecting()
                    except Exception:  # noqa: BLE001 - snapshot probe must remain best effort
                        logger.debug(
                            "Error probing state manager _is_connecting()",
                            exc_info=True,
                        )
                        is_connecting = False
                    else:
                        is_connecting = (
                            connecting_result
                            if isinstance(connecting_result, bool)
                            else False
                        )
                elif not _is_unconfigured_mock_member(
                    legacy_is_connecting
                ) and isinstance(legacy_is_connecting, bool):
                    is_connecting = legacy_is_connecting
                else:
                    is_connecting = False
            publish_pending = iface._client_publish_pending
            is_closing = self._is_connection_closing_locked()
        return client, is_connecting, publish_pending, is_closing

    def _process_client_state(
        self,
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
        client: BLEClient | None,
        is_connecting: bool,
        publish_pending: bool,
        is_closing: bool,
        wait_for_event: (
            Callable[["ThreadCoordinator", str, float | None], bool] | None
        ) = None,
    ) -> bool:
        """Process current client state and decide whether to break loop."""
        iface = self._iface
        wait_for_runtime_event: Callable[["ThreadCoordinator", str, float | None], bool]
        if wait_for_event is not None:
            wait_for_runtime_event = wait_for_event
        else:

            def _wait_for_runtime_event(
                target_coordinator: "ThreadCoordinator",
                event_name: str,
                timeout: float | None,
            ) -> bool:
                return self._coordinator_wait_for_event(
                    target_coordinator,
                    event_name,
                    timeout=timeout,
                )

            wait_for_runtime_event = _wait_for_runtime_event

        if client is None and is_closing:
            logger.debug("BLE client is None, shutting down")
            self._set_receive_wanted(want_receive=False)
            return True

        if publish_pending:
            logger.debug(
                "Skipping BLE read while connect publication is pending verification."
            )
            wait_for_runtime_event(
                coordinator,
                RECONNECTED_EVENT,
                wait_timeout,
            )
            return True

        if client is not None:
            return False

        if iface.auto_reconnect or is_connecting:
            wait_reason = (
                "connection establishment" if is_connecting else "auto-reconnect"
            )
            logger.debug("BLE client is None; waiting for %s", wait_reason)
            wait_for_runtime_event(
                coordinator,
                RECONNECTED_EVENT,
                wait_timeout,
            )
            return True

        logger.debug("BLE client is None; re-checking connection state")
        return True

    def _reset_recovery_after_stability(self) -> None:
        """Reset recovery-attempt counter after sustained stable reads."""
        iface = self._iface
        now = time.monotonic()
        with iface._state_lock:
            if (
                iface._receive_recovery_attempts > 0
                and now - iface._last_recovery_time
                >= RECEIVE_RECOVERY_STABILITY_RESET_SEC
            ):
                logger.debug(
                    "Resetting receive recovery attempts after %.1fs of stability.",
                    now - iface._last_recovery_time,
                )
                iface._receive_recovery_attempts = 0

    def _read_and_handle_payload(
        self,
        client: BLEClient,
        *,
        poll_without_notify: bool,
    ) -> bool:
        """Read a payload iteration and dispatch packet handling."""
        iface = self._iface
        payload = self.read_from_radio_with_retries(
            client,
            retry_on_empty=not poll_without_notify,
        )
        if not payload:
            iface._read_retry_count = 0
            return False
        logger.debug("FROMRADIO read: %s", payload.hex())
        try:
            iface._handle_from_radio(payload)
        except DecodeError as exc:
            logger.warning("Failed to parse FromRadio packet, discarding: %s", exc)
            iface._read_retry_count = 0
            return True
        self._reset_recovery_after_stability()
        iface._read_retry_count = 0
        return True

    def _handle_payload_read(
        self,
        client: BLEClient,
        *,
        poll_without_notify: bool,
    ) -> tuple[bool, bool]:
        """Handle one payload-read attempt and classify loop control flow."""
        iface = self._iface
        try:
            if not self._read_and_handle_payload(
                client,
                poll_without_notify=poll_without_notify,
            ):
                return True, False
            return False, False
        except BleakDBusError as exc:
            if self.handle_read_loop_disconnect(repr(exc), client):
                return True, False
            return True, True
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            raise
        except (BleakError, BLEClient.BLEError) as exc:
            try:
                self.handle_transient_read_error(exc)
                return False, False
            except iface.BLEError:
                logger.error("Fatal BLE read error after retries: %s", exc)
                if not self._is_connection_closing():
                    self._close_after_fatal_read()
                return True, True
        except (RuntimeError, OSError) as exc:
            logger.error("Fatal error in BLE receive thread: %s", exc)
            if not self._is_connection_closing():
                self._close_after_fatal_read()
            return True, True
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            logger.exception("Unexpected error in BLE read loop")
            if self.handle_read_loop_disconnect(repr(exc), client):
                return True, False
            return True, True

    def _run_receive_cycle(
        self,
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
    ) -> bool:
        """Run receive-loop orchestration until shutdown or fatal stop."""
        while self._should_run_receive_loop():
            proceed, poll_without_notify = self._wait_for_read_trigger(
                coordinator=coordinator,
                wait_timeout=wait_timeout,
            )
            if not proceed:
                continue

            while self._should_run_receive_loop():
                client, is_connecting, publish_pending, is_closing = (
                    self._snapshot_client_state()
                )
                if self._process_client_state(
                    coordinator=coordinator,
                    wait_timeout=wait_timeout,
                    client=client,
                    is_connecting=is_connecting,
                    publish_pending=publish_pending,
                    is_closing=is_closing,
                ):
                    break

                if client is None:  # pragma: no cover
                    break

                break_inner_loop, stop_receive_loop = self._handle_payload_read(
                    client,
                    poll_without_notify=poll_without_notify,
                )
                if stop_receive_loop:
                    return False
                if break_inner_loop:
                    break
        return True

    def receive_from_radio_impl(self) -> None:
        """Run the receive loop for the bound interface."""
        iface = self._iface
        coordinator = iface.thread_coordinator
        wait_timeout = BLEConfig.RECEIVE_WAIT_TIMEOUT
        try:
            should_continue = self._run_receive_cycle(
                coordinator=coordinator,
                wait_timeout=wait_timeout,
            )
            if not should_continue:
                return
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            raise
        except (
            BleakDBusError,
            BLEClient.BLEError,
            BleakError,
            DecodeError,
            RuntimeError,
            OSError,
        ):
            logger.exception("Fatal error in BLE receive thread")
            self.recover_receive_thread(RECEIVE_THREAD_FATAL_REASON)
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected fatal error in BLE receive thread")
            self.recover_receive_thread(RECEIVE_THREAD_FATAL_REASON)

    def recover_receive_thread(self, disconnect_reason: str) -> None:
        """Recover the receive thread for the bound interface."""
        iface = self._iface
        if self._is_connection_closing():
            return
        with iface._state_lock:
            current_client = iface.client
        should_continue = self._handle_disconnect(
            disconnect_reason,
            client=current_client,
        )
        if not should_continue:
            self._set_receive_wanted(want_receive=False)
            return
        now = time.monotonic()
        with iface._state_lock:
            iface._receive_recovery_attempts += 1
            attempts = iface._receive_recovery_attempts
            last_recovery = iface._last_recovery_time
        logger.debug(
            "BLE receive recovery attempt scheduled: attempts=%d last_recovery_time=%.3f",
            attempts,
            last_recovery,
        )
        if attempts > RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD:
            max_exponent = max(
                math.ceil(math.log2(max(RECEIVE_RECOVERY_MAX_BACKOFF_SEC, 1.0))),
                0,
            )
            exponent = min(
                attempts - RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD,
                max_exponent,
            )
            backoff = min(2.0**exponent, RECEIVE_RECOVERY_MAX_BACKOFF_SEC)
            remaining_wait = backoff - (now - last_recovery)
            if remaining_wait > 0:
                logger.warning(
                    "Throttling BLE receive recovery: waiting %.1fs before retry (attempt %d)",
                    remaining_wait,
                    attempts,
                )
                logger.debug(
                    "BLE receive recovery backoff: attempts=%d backoff=%.3f remaining_wait=%.3f",
                    attempts,
                    backoff,
                    remaining_wait,
                )
                if iface._shutdown_event.wait(timeout=remaining_wait):
                    logger.debug(
                        "BLE receive recovery aborted by shutdown during backoff wait (attempt %d)",
                        attempts,
                    )
                    return
                logger.debug(
                    "BLE receive recovery backoff wait elapsed; retrying restart (attempt %d)",
                    attempts,
                )
        with iface._state_lock:
            iface._last_recovery_time = time.monotonic()
            updated_last_recovery_time = iface._last_recovery_time
        iface._read_retry_count = 0
        logger.debug(
            "BLE receive recovery timestamp updated: attempts=%d last_recovery_time=%.3f",
            attempts,
            updated_last_recovery_time,
        )
        should_restart = self._should_run_receive_loop()
        logger.debug(
            "BLE receive recovery restart decision: attempts=%d should_restart=%s reset_recovery=%s",
            attempts,
            should_restart,
            False,
        )
        if should_restart:
            try:
                self._start_receive_thread(
                    name="BLEReceiveRecovery", reset_recovery=False
                )
            except Exception:  # noqa: BLE001 - preserve existing failure behavior
                logger.warning(
                    "BLE receive recovery restart failed (attempt %d)",
                    attempts,
                    exc_info=True,
                )
                raise
            logger.debug(
                "BLE receive recovery restart started (attempt %d)",
                attempts,
            )

    def read_from_radio_with_retries(
        self, client: BLEClient, *, retry_on_empty: bool = True
    ) -> bytes | None:
        """Read FROMRADIO payload for the bound interface with retry policy."""
        iface = self._iface
        override_read_from_radio_with_retries = (
            self._resolve_iface_receive_hook_override("_read_from_radio_with_retries")
        )
        if callable(override_read_from_radio_with_retries):
            return cast(
                "bytes | None",
                override_read_from_radio_with_retries(
                    client,
                    retry_on_empty=retry_on_empty,
                ),
            )
        max_retries = BLEConfig.EMPTY_READ_MAX_RETRIES if retry_on_empty else 0
        read_timeout = (
            GATT_IO_TIMEOUT if retry_on_empty else BLEConfig.RECEIVE_WAIT_TIMEOUT
        )
        for attempt in range(max_retries + 1):
            payload = client.read_gatt_char(FROMRADIO_UUID, timeout=read_timeout)
            if payload:
                iface._suppressed_empty_read_warnings = 0
                return payload
            if attempt < max_retries:
                _sleep(iface._retry_policy_get_delay(iface._empty_read_policy, attempt))
        if retry_on_empty:
            self.log_empty_read_warning()
        return None

    def handle_transient_read_error(
        self, error: BleakError | BLEClient.BLEError
    ) -> None:
        """Apply transient-read retry policy for the bound interface."""
        iface = self._iface
        override_handle_transient_read_error = (
            self._resolve_iface_receive_hook_override("_handle_transient_read_error")
        )
        if callable(override_handle_transient_read_error):
            override_handle_transient_read_error(error)
            return
        transient_policy = iface._transient_read_policy
        if iface._retry_policy_should_retry(transient_policy, iface._read_retry_count):
            attempt_index = iface._read_retry_count
            iface._read_retry_count += 1
            logger.debug(
                "Transient BLE read error, retrying (%d/%d)",
                iface._read_retry_count,
                BLEConfig.TRANSIENT_READ_MAX_RETRIES,
            )
            _sleep(iface._retry_policy_get_delay(transient_policy, attempt_index))
            return
        iface._read_retry_count = 0
        raise iface.BLEError(ERROR_READING_BLE) from error

    def log_empty_read_warning(self) -> None:
        """Emit empty-read warning for the bound interface."""
        iface = self._iface
        override_log_empty_read_warning = self._resolve_iface_receive_hook_override(
            "_log_empty_read_warning"
        )
        if callable(override_log_empty_read_warning):
            override_log_empty_read_warning()
            return
        now = time.monotonic()
        cooldown = BLEConfig.EMPTY_READ_WARNING_COOLDOWN
        if now - iface._last_empty_read_warning >= cooldown:
            suppressed = iface._suppressed_empty_read_warnings
            message = f"Exceeded max retries for empty BLE read from {FROMRADIO_UUID}"
            if suppressed:
                message = (
                    f"{message} (suppressed {suppressed} repeats in the last "
                    f"{cooldown:.0f}s)"
                )
            logger.warning(message)
            iface._last_empty_read_warning = now
            iface._suppressed_empty_read_warnings = 0
            return

        iface._suppressed_empty_read_warnings += 1
        logger.debug(
            "Suppressed repeated empty BLE read warning (%d within %.0fs window)",
            iface._suppressed_empty_read_warnings,
            cooldown,
        )


# COMPAT_STABLE_SHIM: Re-export legacy receive service name for compatibility.
# Deferred import avoids circular dependency with `receive_compat_service`.
from meshtastic.interfaces.ble.receive_compat_service import (  # noqa: E402
    BLEReceiveRecoveryService,
)

__all__ = ["BLEReceiveRecoveryController", "BLEReceiveRecoveryService"]
