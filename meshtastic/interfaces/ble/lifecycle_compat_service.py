"""Lifecycle compatibility shim service for BLE."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast, NoReturn

from bleak import BleakClient as BleakRootClient

from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.gating import _is_currently_connected_elsewhere
from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
    BLEDisconnectLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
    BLEConnectionOwnershipLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_primitives import (
    _client_is_connected_compat,
    _DisconnectPlan,
    _LifecycleErrorAccess,
    _LifecycleStateAccess,
    _LifecycleThreadAccess,
    _OwnershipSnapshot,
)
from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
    BLEReceiveLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_shutdown_runtime import (
    BLEShutdownLifecycleCoordinator,
)
from meshtastic.interfaces.ble.state import ConnectionState

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient
    from meshtastic.interfaces.ble.interface import BLEInterface


@dataclass(frozen=True)
class _DisconnectCallbackBundle:
    """Typed callback bundle for disconnect coordinator compatibility dispatch."""

    is_closing_getter: Callable[[], bool]
    current_state_getter: Callable[[], ConnectionState]
    transition_to_disconnected: Callable[[], bool]
    reset_to_disconnected: Callable[[], bool]
    close_previous_client_async: Callable[["BLEClient | None"], None]
    clear_events: Callable[[tuple[str, ...]], None]


class BLELifecycleService:
    """Service helpers for BLEInterface lifecycle responsibilities."""

    @staticmethod
    def _resolve_error_handler_hook(
        iface: "BLEInterface", public_name: str, legacy_name: str
    ) -> Callable[..., object] | None:
        """Resolve an error-handler hook with public-first fallback behavior.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing the error-handler collaborator.
        public_name : str
            Preferred public hook name to resolve first.
        legacy_name : str
            Legacy fallback hook name used when the public hook is unavailable.

        Returns
        -------
        Callable[..., object] | None
            Resolved callable hook when available; otherwise ``None``.
        """
        return _LifecycleErrorAccess(iface).resolve_hook(public_name, legacy_name)

    @staticmethod
    def _error_handler_safe_cleanup(
        iface: "BLEInterface",
        cleanup: Callable[[], object],
        operation_name: str,
    ) -> None:
        """Run cleanup via resolved error-handler hook with best-effort fallback.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing error-handler hook resolution.
        cleanup : Callable[[], object]
            Cleanup callable to execute.
        operation_name : str
            Operation label used in fallback diagnostics.

        Returns
        -------
        None
            Always returns ``None``.

        Notes
        -----
        Exceptions are suppressed to preserve shutdown progress.
        """
        _LifecycleErrorAccess(iface).safe_cleanup(cleanup, operation_name)

    @staticmethod
    def _error_handler_safe_execute(
        iface: "BLEInterface",
        func: Callable[[], object],
        *,
        error_msg: str,
    ) -> object | None:
        """Run callable via resolved error-handler execute hook with fallback.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing error-handler hook resolution.
        func : Callable[[], object]
            Callable to execute.
        error_msg : str
            Error message used in fallback diagnostics.

        Returns
        -------
        object | None
            Callable return value on success; otherwise ``None``.

        Notes
        -----
        Exceptions are suppressed to preserve shutdown progress.
        """
        return _LifecycleErrorAccess(iface).safe_execute(func, error_msg=error_msg)

    @staticmethod
    def _set_receive_wanted(iface: "BLEInterface", *, want_receive: bool) -> None:
        """Request or clear the background receive loop.

        Parameters
        ----------
        iface : BLEInterface
            Interface owning receive-loop state.
        want_receive : bool
            Desired receive-loop intent flag.

        Returns
        -------
        None
            Always returns ``None``.
        """
        BLEReceiveLifecycleCoordinator(iface).set_receive_wanted(
            want_receive=want_receive
        )

    @staticmethod
    def _should_run_receive_loop(iface: "BLEInterface") -> bool:
        """Return whether the receive loop should run.

        Parameters
        ----------
        iface : BLEInterface
            Interface owning receive-loop state.

        Returns
        -------
        bool
            ``True`` when receive is requested and shutdown has not started.
        """
        return BLEReceiveLifecycleCoordinator(iface).should_run_receive_loop()

    @staticmethod
    def _thread_create_thread(
        iface: "BLEInterface",
        *,
        target: Callable[..., object],
        name: str,
        daemon: bool,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
    ) -> ThreadLike:
        """Create a thread via public-first coordinator compatibility dispatch.

        Parameters
        ----------
        iface : BLEInterface
            Interface exposing ``thread_coordinator``.
        target : Callable[..., object]
            Thread target callable.
        name : str
            Thread name.
        daemon : bool
            Whether the created thread should be daemonized.
        args : tuple[object, ...]
            Positional arguments forwarded to the thread target.
        kwargs : dict[str, object] | None
            Optional keyword arguments forwarded to the thread target.

        Returns
        -------
        ThreadLike
            Created thread-like object.

        Raises
        ------
        AttributeError
            If no compatible thread-creation method exists on the coordinator.
        """
        return _LifecycleThreadAccess(iface).create_thread(
            target=target,
            name=name,
            daemon=daemon,
            args=args,
            kwargs=kwargs,
        )

    @staticmethod
    def _thread_start_thread(iface: "BLEInterface", thread: object) -> None:
        """Start a thread via public-first coordinator compatibility dispatch.

        Parameters
        ----------
        iface : BLEInterface
            Interface exposing ``thread_coordinator``.
        thread : object
            Thread-like object to start.

        Returns
        -------
        None
            Always returns ``None``.

        Raises
        ------
        AttributeError
            If no compatible thread-start method exists on the coordinator.
        """
        _LifecycleThreadAccess(iface).start_thread(thread)

    @staticmethod
    def _thread_join_thread(
        iface: "BLEInterface", thread: object, *, timeout: float | None
    ) -> None:
        """Join a thread via public-first coordinator compatibility dispatch.

        Parameters
        ----------
        iface : BLEInterface
            Interface exposing ``thread_coordinator``.
        thread : object
            Thread-like object to join.
        timeout : float | None
            Join timeout in seconds.

        Returns
        -------
        None
            Always returns ``None``.
        """
        _LifecycleThreadAccess(iface).join_thread(thread, timeout=timeout)

    @staticmethod
    def _thread_set_event(iface: "BLEInterface", event_name: str) -> None:
        """Set a coordinator event via public-first compatibility dispatch.

        Parameters
        ----------
        iface : BLEInterface
            Interface exposing ``thread_coordinator``.
        event_name : str
            Event name to set.

        Returns
        -------
        None
            Always returns ``None``.
        """
        _LifecycleThreadAccess(iface).set_event(event_name)

    @staticmethod
    def _thread_clear_events(iface: "BLEInterface", *event_names: str) -> None:
        """Clear coordinator events via public-first compatibility dispatch.

        Parameters
        ----------
        iface : BLEInterface
            Interface exposing ``thread_coordinator``.
        *event_names : str
            Event names to clear.

        Returns
        -------
        None
            Always returns ``None``.
        """
        _LifecycleThreadAccess(iface).clear_events(*event_names)

    @staticmethod
    def _thread_wake_waiting_threads(iface: "BLEInterface", *event_names: str) -> None:
        """Wake coordinator waiters via public-first compatibility dispatch.

        Parameters
        ----------
        iface : BLEInterface
            Interface exposing ``thread_coordinator``.
        *event_names : str
            Event names used to wake waiting threads.

        Returns
        -------
        None
            Always returns ``None``.
        """
        _LifecycleThreadAccess(iface).wake_waiting_threads(*event_names)

    @staticmethod
    def _start_receive_thread(
        iface: "BLEInterface", *, name: str, reset_recovery: bool = True
    ) -> None:
        """Create and start the background receive thread.

        Parameters
        ----------
        iface : BLEInterface
            Interface owning receive-thread state.
        name : str
            Name assigned to the receive thread.
        reset_recovery : bool
            Whether to reset recovery-attempt counters after a successful start.

        Returns
        -------
        None
            Always returns ``None``.

        Raises
        ------
        Exception
            Propagates thread-start failures after clearing stale thread state.
        """
        BLEReceiveLifecycleCoordinator(iface).start_receive_thread(
            name=name,
            reset_recovery=reset_recovery,
            create_thread=lambda **kwargs: BLELifecycleService._thread_create_thread(
                iface,
                **kwargs,
            ),
            start_thread=lambda thread: BLELifecycleService._thread_start_thread(
                iface,
                thread,
            ),
        )

    @staticmethod
    def _on_ble_disconnect(iface: "BLEInterface", client: BleakRootClient) -> None:
        """Handle a Bleak disconnect callback from the active transport client.

        Parameters
        ----------
        iface : BLEInterface
            Interface receiving the disconnect callback.
        client : BleakRootClient
            Bleak client object that triggered the callback.

        Returns
        -------
        None
            Returns ``None`` after delegating to disconnect handling.
        """
        BLEDisconnectLifecycleCoordinator(iface).on_ble_disconnect(client)

    @staticmethod
    def _schedule_auto_reconnect(iface: "BLEInterface") -> None:
        """Schedule background auto-reconnect work when reconnect is enabled.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing reconnect scheduler and shutdown state.

        Returns
        -------
        None
            Returns ``None`` after scheduling or skipping reconnect work.

        Raises
        ------
        AttributeError
            If reconnect scheduler dispatch methods are missing.
        """
        BLEDisconnectLifecycleCoordinator(iface).schedule_auto_reconnect(
            is_closing_getter=lambda: BLELifecycleService._state_manager_is_closing(
                iface
            )
        )

    @staticmethod
    def _disconnect_and_close_client(
        iface: "BLEInterface", client: "BLEClient"
    ) -> None:
        """Release BLE client resources with best-effort disconnect/close handling.

        Parameters
        ----------
        iface : BLEInterface
            Interface exposing client-manager cleanup helpers.
        client : BLEClient
            Client to disconnect and close.

        Returns
        -------
        None
            Returns ``None`` after cleanup is attempted.
        """
        BLEDisconnectLifecycleCoordinator(iface).disconnect_and_close_client(client)

    @staticmethod
    def _compute_disconnect_keys(
        iface: "BLEInterface",
        *,
        previous_client: "BLEClient | None",
        alias_key: str | None,
        should_reconnect: bool,
        address: str,
    ) -> tuple[list[str], bool]:
        """Compute disconnect registry keys and reconnect scheduling intent.

        Parameters
        ----------
        iface : BLEInterface
            Interface used to normalize and sort address keys.
        previous_client : BLEClient | None
            Previously owned client at disconnect start, when available.
        alias_key : str | None
            Connection alias key tracked for ownership gating.
        should_reconnect : bool
            Whether reconnect behavior is enabled for this disconnect.
        address : str
            Best-effort callback address value for key derivation.

        Returns
        -------
        tuple[list[str], bool]
            Sorted registry keys to mark disconnected and whether reconnect
            scheduling should proceed.
        """
        return BLEDisconnectLifecycleCoordinator(iface)._compute_disconnect_keys(
            previous_client=previous_client,
            alias_key=alias_key,
            should_reconnect=should_reconnect,
            address=address,
        )

    @staticmethod
    def _resolve_disconnect_target(
        iface: "BLEInterface",
        source: str,
        client: "BLEClient | None",
        bleak_client: BleakRootClient | None,
    ) -> _DisconnectPlan:
        """Resolve disconnect ownership, mutate state, and build side-effect plan.

        Parameters
        ----------
        iface : BLEInterface
            Interface owning client/state references for disconnect handling.
        source : str
            Logical callback source label used for debug logging.
        client : BLEClient | None
            Optional explicit client associated with the disconnect signal.
        bleak_client : BleakRootClient | None
            Optional raw Bleak client associated with the disconnect signal.

        Returns
        -------
        _DisconnectPlan
            Planned side effects and ownership metadata for disconnect flow.
        """
        callbacks = BLELifecycleService._build_disconnect_callback_bundle(iface)
        return BLEDisconnectLifecycleCoordinator(iface)._resolve_disconnect_target(
            source,
            client,
            bleak_client,
            current_state_getter=callbacks.current_state_getter,
            is_closing_getter=callbacks.is_closing_getter,
            transition_to_disconnected=callbacks.transition_to_disconnected,
            reset_to_disconnected=callbacks.reset_to_disconnected,
        )

    @staticmethod
    def _build_disconnect_callback_bundle(
        iface: "BLEInterface",
    ) -> _DisconnectCallbackBundle:
        """Return canonical disconnect callback wiring for lifecycle compat entrypoints."""
        return _DisconnectCallbackBundle(
            is_closing_getter=lambda: BLELifecycleService._state_manager_is_closing(
                iface
            ),
            current_state_getter=lambda: BLELifecycleService._state_manager_current_state(
                iface
            ),
            transition_to_disconnected=lambda: BLELifecycleService._state_manager_transition_to(  # noqa: E501
                iface,
                ConnectionState.DISCONNECTED,
            ),
            reset_to_disconnected=lambda: BLELifecycleService._state_manager_reset_to_disconnected(  # noqa: E501
                iface
            ),
            close_previous_client_async=lambda previous_client: BLELifecycleService._close_previous_client_async(  # noqa: E501
                iface,
                previous_client,
            ),
            clear_events=lambda events: BLELifecycleService._thread_clear_events(
                iface, *events
            ),
        )

    @staticmethod
    def _close_previous_client_async(
        iface: "BLEInterface", previous_client: "BLEClient | None"
    ) -> None:
        """Close a disconnected previous client asynchronously.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing thread and cleanup helpers.
        previous_client : BLEClient | None
            Prior client to close. ``None`` is a no-op.

        Returns
        -------
        None
            Returns ``None`` after scheduling or inline cleanup.

        Notes
        -----
        Falls back to inline cleanup when thread creation/start fails.
        """
        BLEDisconnectLifecycleCoordinator(iface)._close_previous_client_async(
            previous_client,
            create_thread=lambda **kwargs: BLELifecycleService._thread_create_thread(
                iface,
                **kwargs,
            ),
            start_thread=lambda thread: BLELifecycleService._thread_start_thread(
                iface,
                thread,
            ),
            safe_cleanup=lambda cleanup, operation_name: BLELifecycleService._error_handler_safe_cleanup(  # noqa: E501
                iface,
                cleanup,
                operation_name,
            ),
        )

    @staticmethod
    def _execute_disconnect_side_effects(
        iface: "BLEInterface", *, plan: _DisconnectPlan, source: str
    ) -> bool:
        """Execute disconnect publication/cleanup side effects.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing disconnect side-effect helpers.
        plan : _DisconnectPlan
            Disconnect plan produced by ``_resolve_disconnect_target``.
        source : str
            Disconnect-source label used in logging.

        Returns
        -------
        bool
            ``True`` when reconnect flow should continue; ``False`` when
            disconnect handling should stop.
        """
        callbacks = BLELifecycleService._build_disconnect_callback_bundle(iface)
        return BLEDisconnectLifecycleCoordinator(
            iface
        )._execute_disconnect_side_effects(
            plan=plan,
            source=source,
            close_previous_client_async=callbacks.close_previous_client_async,
            clear_events=callbacks.clear_events,
        )

    @staticmethod
    def _handle_disconnect(
        iface: "BLEInterface",
        source: str,
        client: "BLEClient | None" = None,
        bleak_client: BleakRootClient | None = None,
    ) -> bool:
        """Handle disconnect orchestration and reconnect decisions.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose disconnect lifecycle is being processed.
        source : str
            Disconnect-source label used in logs and emitted metadata.
        client : BLEClient | None
            Optional client that triggered the disconnect callback.
        bleak_client : BleakRootClient | None
            Optional Bleak transport object used when the wrapped client is not
            directly available.

        Returns
        -------
        bool
            ``True`` when reconnect processing should continue; ``False`` when
            disconnect flow should stop.
        """
        callbacks = BLELifecycleService._build_disconnect_callback_bundle(iface)
        return BLEDisconnectLifecycleCoordinator(iface).handle_disconnect(
            source,
            client=client,
            bleak_client=bleak_client,
            is_closing_getter=callbacks.is_closing_getter,
            current_state_getter=callbacks.current_state_getter,
            transition_to_disconnected=callbacks.transition_to_disconnected,
            reset_to_disconnected=callbacks.reset_to_disconnected,
            close_previous_client_async=callbacks.close_previous_client_async,
            clear_events=callbacks.clear_events,
        )

    @staticmethod
    def _emit_verified_connection_side_effects(
        iface: "BLEInterface", connected_client: "BLEClient"
    ) -> None:
        """Emit reconnect wake signal and success logging after verified publish.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing reconnect event coordination state.
        connected_client : BLEClient
            Connected client used for normalized success logging.

        Returns
        -------
        None
            Returns ``None`` after side effects are emitted.
        """
        BLEConnectionOwnershipLifecycleCoordinator(
            iface
        )._emit_verified_connection_side_effects(connected_client)

    @staticmethod
    def _discard_invalidated_connected_client(
        iface: "BLEInterface",
        client: "BLEClient",
        *,
        restore_address: str | None = None,
        restore_last_connection_request: str | None = None,
    ) -> None:
        """Clean up a client invalidated before connect publication completes.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing state and client-close helpers.
        client : BLEClient
            Client to detach and close.
        restore_address : str | None
            Address restored when ownership remains local after invalidation.
        restore_last_connection_request : str | None
            Last-request value restored when ownership remains local.

        Returns
        -------
        None
            Returns ``None`` after best-effort cleanup.
        """
        BLEConnectionOwnershipLifecycleCoordinator(
            iface
        )._discard_invalidated_connected_client(
            client,
            restore_address=restore_address,
            restore_last_connection_request=restore_last_connection_request,
            is_closing_getter=lambda: BLELifecycleService._state_manager_is_closing(
                iface
            ),
            reset_to_disconnected=lambda: BLELifecycleService._state_manager_reset_to_disconnected(  # noqa: E501
                iface
            ),
            current_state_getter=lambda: BLELifecycleService._state_manager_current_state(
                iface
            ),
            transition_to_disconnected=lambda: BLELifecycleService._state_manager_transition_to(  # noqa: E501
                iface,
                ConnectionState.DISCONNECTED,
            ),
            safe_cleanup=lambda cleanup, operation_name: BLELifecycleService._error_handler_safe_cleanup(  # noqa: E501
                iface,
                cleanup,
                operation_name,
            ),
        )

    @staticmethod
    def _state_manager_is_connected(iface: "BLEInterface") -> bool:
        """Return connected-state flag from public-first state-manager members.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing state-manager compatibility access.

        Returns
        -------
        bool
            ``True`` when the state manager reports connected state.

        Raises
        ------
        AttributeError
            If no valid connected-state compatibility member is available.

        Notes
        -----
        Dispatches through ``_LifecycleStateAccess``.
        """
        return _LifecycleStateAccess(iface).is_connected()

    @staticmethod
    def _state_manager_current_state(iface: "BLEInterface") -> ConnectionState:
        """Return current connection state from public-first state-manager members.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing state-manager compatibility access.

        Returns
        -------
        ConnectionState
            Current state-manager connection state value.

        Raises
        ------
        AttributeError
            If no valid current-state compatibility member is available.

        Notes
        -----
        Dispatches through ``_LifecycleStateAccess``.
        """
        return _LifecycleStateAccess(iface).current_state()

    @staticmethod
    def _state_manager_transition_to(
        iface: "BLEInterface", new_state: ConnectionState
    ) -> bool:
        """Transition state manager using public-first compatibility dispatch.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing state-manager compatibility access.
        new_state : ConnectionState
            Target state for the transition request.

        Returns
        -------
        bool
            ``True`` when transition succeeds.

        Raises
        ------
        AttributeError
            If no valid transition compatibility member is available.

        Notes
        -----
        Dispatches through ``_LifecycleStateAccess``.
        """
        return _LifecycleStateAccess(iface).transition_to(new_state)

    @staticmethod
    def _state_manager_reset_to_disconnected(iface: "BLEInterface") -> bool:
        """Reset state manager to disconnected using public-first dispatch.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing state-manager compatibility access.

        Returns
        -------
        bool
            ``True`` when reset succeeds.

        Raises
        ------
        AttributeError
            If no valid reset compatibility member is available.

        Notes
        -----
        Dispatches through ``_LifecycleStateAccess``.
        """
        return _LifecycleStateAccess(iface).reset_to_disconnected()

    @staticmethod
    def _state_manager_is_closing(iface: "BLEInterface") -> bool:
        """Return closing-state flag from public-first state-manager members.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing state-manager compatibility access.

        Returns
        -------
        bool
            ``True`` when the state manager reports an active close/shutdown
            transition, otherwise ``False``.

        Notes
        -----
        Dispatches through ``_LifecycleStateAccess``.
        """
        return _LifecycleStateAccess(iface).is_closing()

    @staticmethod
    def _client_is_connected(client: "BLEClient") -> bool:
        """Return connected-state flag from public/legacy BLEClient members.

        Parameters
        ----------
        client : BLEClient
            Client instance whose connected state should be probed.

        Returns
        -------
        bool
            ``True`` when client reports an active connection.

        Raises
        ------
        AttributeError
            If no valid connected-state compatibility member is available.

        Notes
        -----
        Dispatches through ``_client_is_connected_compat``.
        """
        return _client_is_connected_compat(client)

    @staticmethod
    def _get_connected_client_status_locked(
        iface: "BLEInterface", client: "BLEClient"
    ) -> tuple[bool, bool]:
        """Return ownership and closing flags for ``client`` under state lock.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose state is being evaluated.
        client : BLEClient
            Client candidate for ownership verification.

        Returns
        -------
        tuple[bool, bool]
            ``(is_owned, is_closing)`` for the current snapshot.
        """
        return BLEConnectionOwnershipLifecycleCoordinator(
            iface
        )._get_connected_client_status_locked(
            client,
            is_closing_getter=lambda: BLELifecycleService._state_manager_is_closing(
                iface
            ),
            state_connected_getter=lambda: BLELifecycleService._state_manager_is_connected(  # noqa: E501
                iface
            ),
            client_connected_getter=BLELifecycleService._client_is_connected,
        )

    @staticmethod
    def _get_connected_client_status(
        iface: "BLEInterface", client: "BLEClient"
    ) -> tuple[bool, bool]:
        """Return ownership and closing flags with internal state locking.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose state is being evaluated.
        client : BLEClient
            Client candidate for ownership verification.

        Returns
        -------
        tuple[bool, bool]
            ``(is_owned, is_closing)`` for the current snapshot.
        """
        return BLEConnectionOwnershipLifecycleCoordinator(
            iface
        )._get_connected_client_status(
            client,
            is_closing_getter=lambda: BLELifecycleService._state_manager_is_closing(
                iface
            ),
            state_connected_getter=lambda: BLELifecycleService._state_manager_is_connected(  # noqa: E501
                iface
            ),
            client_connected_getter=BLELifecycleService._client_is_connected,
        )

    @staticmethod
    def _has_lost_gate_ownership(iface: "BLEInterface", *keys: str | None) -> bool:
        """Return whether any supplied address key is now owned elsewhere.

        Parameters
        ----------
        iface : BLEInterface
            Interface used as the ownership identity.
        *keys : str | None
            Address keys to probe for ownership loss.

        Returns
        -------
        bool
            ``True`` when any non-``None`` key is currently owned by another
            interface; otherwise ``False``.
        """
        # Support lifecycle_service module monkeypatches in compatibility tests;
        # fall back to the canonical gating import when no override is present.
        from meshtastic.interfaces.ble import lifecycle_service as lifecycle_service_mod

        connected_elsewhere = getattr(
            lifecycle_service_mod,
            "_is_currently_connected_elsewhere",
            _is_currently_connected_elsewhere,
        )
        if not callable(connected_elsewhere):
            connected_elsewhere = _is_currently_connected_elsewhere
        connected_elsewhere_fn = cast(
            Callable[[str | None, object | None], bool],
            connected_elsewhere,
        )

        return any(
            key is not None
            and connected_elsewhere_fn(key, iface)
            for key in keys
        )

    @staticmethod
    def _raise_for_invalidated_connect_result(
        iface: "BLEInterface",
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        is_closing: bool,
        lost_gate_ownership: bool,
        restore_address: str | None,
        restore_last_connection_request: str | None,
    ) -> NoReturn:
        """Clean up invalidated connect result and raise the corresponding BLEError.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose stale connect result is being rejected.
        connected_client : BLEClient
            Client produced by a stale connect result.
        connected_device_key : str | None
            Address key for the connected client candidate.
        connection_alias_key : str | None
            Alias key associated with the connect attempt.
        is_closing : bool
            Whether shutdown is active for this interface.
        lost_gate_ownership : bool
            Whether another interface claimed ownership of the target.
        restore_address : str | None
            Address restored when cleanup preserves local state.
        restore_last_connection_request : str | None
            Last-request value restored when cleanup preserves local state.

        Returns
        -------
        None
            This method always raises and does not return normally.

        Raises
        ------
        BLEError
            ``ERROR_INTERFACE_CLOSING`` when closing; otherwise
            ``CONNECTION_ERROR_LOST_OWNERSHIP``.
        """
        iface._raise_for_invalidated_connect_result(
            connected_client,
            connected_device_key,
            connection_alias_key,
            is_closing=is_closing,
            lost_gate_ownership=lost_gate_ownership,
            restore_address=restore_address,
            restore_last_connection_request=restore_last_connection_request,
        )

    @staticmethod
    def _verify_ownership_snapshot(
        iface: "BLEInterface",
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
    ) -> _OwnershipSnapshot:
        """Capture a connect-result ownership snapshot.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose ownership is being verified.
        connected_client : BLEClient
            Client candidate for ownership verification.
        connected_device_key : str | None
            Address key for the connected client candidate.
        connection_alias_key : str | None
            Alias key associated with the connect attempt.

        Returns
        -------
        _OwnershipSnapshot
            Snapshot containing ownership, closing, gate-loss, and reconnect
            publication context.
        """
        return BLEConnectionOwnershipLifecycleCoordinator(
            iface
        )._verify_ownership_snapshot(
            connected_client,
            connected_device_key,
            connection_alias_key,
            get_connected_client_status_locked=lambda client: BLELifecycleService._get_connected_client_status_locked(  # noqa: E501
                iface,
                client,
            ),
        )

    @staticmethod
    def _ever_connected_flag(iface: "BLEInterface") -> bool:
        """Return a mock-safe boolean view of ``iface._ever_connected``.

        Parameters
        ----------
        iface : BLEInterface
            Interface containing connection publication state.

        Returns
        -------
        bool
            ``True`` only when ``_ever_connected`` is explicitly boolean true.
        """
        return BLEConnectionOwnershipLifecycleCoordinator(
            iface
        )._has_ever_connected_session()

    @staticmethod
    def _verify_and_publish_connected(
        iface: "BLEInterface",
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        restore_address: str | None,
        restore_last_connection_request: str | None,
    ) -> None:
        """Publish connected state only when ownership is still valid.

        Parameters
        ----------
        iface : BLEInterface
            Interface owning the candidate connected client.
        connected_client : BLEClient
            Client candidate produced by connection orchestration.
        connected_device_key : str | None
            Address key for ownership validation.
        connection_alias_key : str | None
            Alias key for ownership validation.
        restore_address : str | None
            Address restored when invalidation cleanup is required.
        restore_last_connection_request : str | None
            Last-request value restored when invalidation cleanup is required.

        Returns
        -------
        None
            Returns ``None`` after publication or invalidation handling.

        Raises
        ------
        BLEError
            Raised when ownership is invalidated before or during publication.
        """
        BLEConnectionOwnershipLifecycleCoordinator(iface)._verify_and_publish_connected(
            connected_client,
            connected_device_key,
            connection_alias_key,
            restore_address=restore_address,
            restore_last_connection_request=restore_last_connection_request,
            verify_ownership_snapshot=lambda client, device_key, alias_key: BLELifecycleService._verify_ownership_snapshot(  # noqa: E501
                iface,
                client,
                device_key,
                alias_key,
            ),
            get_connected_client_status_locked=lambda client: BLELifecycleService._get_connected_client_status_locked(  # noqa: E501
                iface,
                client,
            ),
        )

    @staticmethod
    def _cleanup_thread_coordinator(iface: "BLEInterface") -> None:
        """Run thread-coordinator cleanup via public/legacy compatibility hooks.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing the thread coordinator.

        Returns
        -------
        None
            Returns ``None`` after best-effort cleanup.
        """
        BLEShutdownLifecycleCoordinator(iface)._cleanup_thread_coordinator()

    @staticmethod
    def _close(
        iface: "BLEInterface",
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
    ) -> None:
        """Shut down BLE interface resources and finalize lifecycle state.

        Parameters
        ----------
        iface : BLEInterface
            Interface instance being closed.
        management_shutdown_wait_timeout : float
            Maximum seconds to wait for inflight management operations.
        management_wait_poll_seconds : float
            Poll interval used while waiting for management completion.

        Returns
        -------
        None
            Returns ``None`` after best-effort shutdown cleanup.
        """
        BLEShutdownLifecycleCoordinator(iface).close(
            management_shutdown_wait_timeout=management_shutdown_wait_timeout,
            management_wait_poll_seconds=management_wait_poll_seconds,
        )

    @staticmethod
    def _finalize_connection_gates(
        iface: "BLEInterface",
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
    ) -> None:
        """Finalize address-gate ownership after successful connection.

        Parameters
        ----------
        iface : BLEInterface
            Interface owning connection-gate state.
        connected_client : BLEClient
            Connected client candidate associated with the gate claim.
        connected_device_key : str | None
            Device key to mark connected/disconnected.
        connection_alias_key : str | None
            Alias key to mark connected/disconnected.

        Returns
        -------
        None
            Returns ``None`` after gate publication or stale-claim cleanup.
        """
        BLEConnectionOwnershipLifecycleCoordinator(iface)._finalize_connection_gates(
            connected_client,
            connected_device_key,
            connection_alias_key,
            get_connected_client_status=lambda client: BLELifecycleService._get_connected_client_status(  # noqa: E501
                iface,
                client,
            ),
            get_connected_client_status_locked=lambda client: BLELifecycleService._get_connected_client_status_locked(  # noqa: E501
                iface,
                client,
            ),
        )

    @staticmethod
    def _is_owned_connected_client(iface: "BLEInterface", client: "BLEClient") -> bool:
        """Return whether the interface still owns the provided connected client.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose ownership state is being checked.
        client : BLEClient
            Client candidate for ownership verification.

        Returns
        -------
        bool
            ``True`` when the interface still owns ``client``.
        """
        is_owned, _ = BLELifecycleService._get_connected_client_status(iface, client)
        return is_owned

    @staticmethod
    def _await_management_shutdown(
        iface: "BLEInterface",
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
    ) -> bool | None:
        """Mark interface as closed and wait for inflight management operations.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose management in-flight counter is being awaited.
        management_shutdown_wait_timeout : float
            Maximum seconds to wait for in-flight management completion.
        management_wait_poll_seconds : float
            Poll interval used while waiting on the management idle condition.

        Returns
        -------
        bool | None
            ``True`` when waiting timed out, ``False`` when all operations
            drained cleanly, or ``None`` when the interface was already closed.
        """
        return BLEShutdownLifecycleCoordinator(iface)._await_management_shutdown(
            management_shutdown_wait_timeout=management_shutdown_wait_timeout,
            management_wait_poll_seconds=management_wait_poll_seconds,
            current_state_getter=lambda: BLELifecycleService._state_manager_current_state(
                iface
            ),
            is_closing_getter=lambda: BLELifecycleService._state_manager_is_closing(
                iface
            ),
            transition_to_state=lambda state: BLELifecycleService._state_manager_transition_to(  # noqa: E501
                iface,
                state,
            ),
            reset_to_disconnected=lambda: BLELifecycleService._state_manager_reset_to_disconnected(  # noqa: E501
                iface
            ),
        )

    @staticmethod
    def _shutdown_discovery(iface: "BLEInterface") -> None:
        """Close discovery resources and clear receive-loop intent.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing discovery manager and receive intent state.

        Returns
        -------
        None
            Returns ``None`` after discovery cleanup is attempted.
        """
        BLEShutdownLifecycleCoordinator(iface)._shutdown_discovery(
            safe_cleanup=lambda cleanup, operation_name: BLELifecycleService._error_handler_safe_cleanup(  # noqa: E501
                iface,
                cleanup,
                operation_name,
            ),
        )

    @staticmethod
    def _shutdown_receive_thread(iface: "BLEInterface") -> None:
        """Wake and join receive thread, then clear cached thread reference.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing receive-thread state and thread hooks.

        Returns
        -------
        None
            Returns ``None`` after best-effort receive-thread shutdown.
        """
        BLEShutdownLifecycleCoordinator(iface)._shutdown_receive_thread(
            wake_waiting_threads=lambda *event_names: BLELifecycleService._thread_wake_waiting_threads(  # noqa: E501
                iface,
                *event_names,
            ),
            join_thread=lambda thread, timeout: BLELifecycleService._thread_join_thread(
                iface,
                thread,
                timeout=timeout,
            ),
        )

    @staticmethod
    def _close_mesh_interface(iface: "BLEInterface") -> None:
        """Run ``MeshInterface.close`` through guarded error-handler execution.

        Parameters
        ----------
        iface : BLEInterface
            Interface instance forwarded to ``MeshInterface.close``.

        Returns
        -------
        None
            Returns ``None`` after guarded close execution.
        """
        BLEShutdownLifecycleCoordinator(iface)._close_mesh_interface(
            safe_execute=lambda func: BLELifecycleService._error_handler_safe_execute(
                iface,
                func,
                error_msg="Error closing mesh interface",
            )
        )

    @staticmethod
    def _unregister_exit_handler(iface: "BLEInterface") -> None:
        """Unregister process exit handler when present.

        Parameters
        ----------
        iface : BLEInterface
            Interface containing optional ``_exit_handler`` state.

        Returns
        -------
        None
            Returns ``None`` after best-effort unregister.
        """
        BLEShutdownLifecycleCoordinator(iface)._unregister_exit_handler()

    @staticmethod
    def _detach_client_for_shutdown(
        iface: "BLEInterface",
    ) -> tuple["BLEClient | None", bool]:
        """Detach active client reference and return detached client plus publish state.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose client and publish-pending state are detached.

        Returns
        -------
        tuple[BLEClient | None, bool]
            ``(client, publish_pending)`` snapshot captured during detach.
        """
        return BLEShutdownLifecycleCoordinator(iface)._detach_client_for_shutdown()

    @staticmethod
    def _consume_disconnect_notification_state(iface: "BLEInterface") -> bool:
        """Consume pending publish flags and decide disconnect notification emission.

        Parameters
        ----------
        iface : BLEInterface
            Interface containing disconnect-publication state flags.

        Returns
        -------
        bool
            ``True`` when callers should emit a public disconnect event.
        """
        return BLEShutdownLifecycleCoordinator(
            iface
        )._consume_disconnect_notification_state()

    @staticmethod
    def _shutdown_client(
        iface: "BLEInterface", *, management_wait_timed_out: bool
    ) -> None:
        """Shutdown active client resources and notification publication state.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing client, notification, and gate helpers.
        management_wait_timed_out : bool
            Whether shutdown skipped management-target gating due timeout.

        Returns
        -------
        None
            Returns ``None`` after best-effort shutdown cleanup.
        """
        BLEShutdownLifecycleCoordinator(iface)._shutdown_client(
            management_wait_timed_out=management_wait_timed_out,
            detach_client_for_shutdown=lambda: BLELifecycleService._detach_client_for_shutdown(  # noqa: E501
                iface
            ),
            safe_cleanup=lambda cleanup, operation_name: BLELifecycleService._error_handler_safe_cleanup(  # noqa: E501
                iface,
                cleanup,
                operation_name,
            ),
            consume_disconnect_notification_state=lambda: BLELifecycleService._consume_disconnect_notification_state(  # noqa: E501
                iface
            ),
        )

    @staticmethod
    def _finalize_close_state(iface: "BLEInterface") -> None:
        """Persist terminal disconnected state and clear address registry claims.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose lifecycle state is being finalized.

        Returns
        -------
        None
            Returns ``None`` after final state and ownership cleanup.
        """
        BLEShutdownLifecycleCoordinator(iface)._finalize_close_state(
            current_state_getter=lambda: BLELifecycleService._state_manager_current_state(
                iface
            ),
            transition_to_state=lambda state: BLELifecycleService._state_manager_transition_to(  # noqa: E501
                iface,
                state,
            ),
            reset_to_disconnected=lambda: BLELifecycleService._state_manager_reset_to_disconnected(  # noqa: E501
                iface
            ),
        )


# COMPAT_STABLE_SHIM: captured original symbol for monkeypatch detection.
_ORIGINAL_GET_CONNECTED_CLIENT_STATUS = BLELifecycleService._get_connected_client_status
# COMPAT_STABLE_SHIM: captured original symbol for monkeypatch detection.
_ORIGINAL_GET_CONNECTED_CLIENT_STATUS_LOCKED = (
    BLELifecycleService._get_connected_client_status_locked
)
