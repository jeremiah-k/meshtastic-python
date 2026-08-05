"""Connection ownership lifecycle coordinator runtime ownership for BLE."""

from collections.abc import Callable
from typing import TYPE_CHECKING

from meshtastic.interfaces.ble.compat_adapter import (
    _get_declared_member,
    _iter_declared_members,
)
from meshtastic.interfaces.ble.constants import RECONNECTED_EVENT, logger
from meshtastic.interfaces.ble.failure_policy import (
    _BLEFailureDisposition,
    _log_ble_failure,
)
from meshtastic.interfaces.ble.lifecycle_primitives import (
    _LifecycleErrorAccess,
    _LifecycleStateAccess,
    _LifecycleThreadAccess,
    _OwnershipSnapshot,
)
from meshtastic.interfaces.ble.ports import _BLESessionStatePort
from meshtastic.interfaces.ble.session_state import _session_state_for
from meshtastic.interfaces.ble.state import ConnectionState
from meshtastic.interfaces.ble.utils import (
    sanitize_address,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.client import BLEClient
    from meshtastic.interfaces.ble.interface import BLEInterface

_LOG_INTERFACE_CLOSED_DURING_CONNECT = (
    "Interface closed during connect(), cleaning up gate claim for %s"
)
_LOG_INTERFACE_LOST_OWNERSHIP_DURING_CONNECT = (
    "Interface lost ownership during connect(), cleaning up gate claim for %s"
)


class BLEConnectionOwnershipLifecycleCoordinator:
    """Own verified-connection publication and ownership/finalization behavior.

    Parameters
    ----------
    iface : BLEInterface
        Interface instance whose connected-session ownership lifecycle is
        coordinated by this collaborator.
    """

    def __init__(
        self,
        iface: "BLEInterface",
        *,
        session_state: _BLESessionStatePort | None = None,
    ) -> None:
        """Bind connection ownership coordination to a specific interface.

        Parameters
        ----------
        iface : BLEInterface
            Interface instance whose ownership lifecycle is managed.
        session_state : _BLESessionStatePort | None
            Optional shared lifecycle state. When ``None``, resolve the
            interface-owned state or use the legacy adapter.

        Returns
        -------
        None
            Initializes bound collaborator state.
        """
        self._iface = iface
        self._session = _session_state_for(iface, session_state)
        self._state_access = _LifecycleStateAccess(iface)
        self._thread_access = _LifecycleThreadAccess(iface)
        self._error_access = _LifecycleErrorAccess(iface)

    def _get_connected_client_status_locked(
        self,
        client: "BLEClient",
        *,
        is_closing_getter: Callable[[], bool] | None = None,
        state_connected_getter: Callable[[], bool] | None = None,
        client_connected_getter: Callable[["BLEClient"], bool] | None = None,
    ) -> tuple[bool, bool]:
        """Return ownership and closing flags for ``client`` while holding lock.

        Parameters
        ----------
        client : BLEClient
            Client candidate whose owned/connected status is being evaluated.
        is_closing_getter : Callable[[], bool] | None
            Optional closure-state probe override.
        state_connected_getter : Callable[[], bool] | None
            Optional state-manager connected probe override.
        client_connected_getter : Callable[[BLEClient], bool] | None
            Optional per-client connected probe override.

        Returns
        -------
        tuple[bool, bool]
            ``(is_owned, is_closing)`` where ``is_owned`` indicates active
            ownership of ``client`` and ``is_closing`` reflects interface
            close/shutdown state.
        """
        iface = self._iface
        state_manager = _get_declared_member(iface, "_state_manager")
        if is_closing_getter is not None:
            is_closing_result = is_closing_getter()
            is_closing = (
                is_closing_result if isinstance(is_closing_result, bool) else False
            )
        else:
            is_closing = self._probe_bool_member(
                state_manager,
                "is_closing",
                "_is_closing",
            )
        is_closing = is_closing or self._session.closed
        if self._session.closed or iface.client is not client:
            return False, is_closing
        if state_connected_getter is not None:
            state_connected_result = state_connected_getter()
            state_connected = (
                state_connected_result
                if isinstance(state_connected_result, bool)
                else False
            )
        else:
            state_connected = self._probe_bool_member(
                state_manager,
                "is_connected",
                "_is_connected",
            )
        if not state_connected:
            return False, is_closing
        if client_connected_getter is not None:
            client_connected_result = client_connected_getter(client)
            client_connected = (
                client_connected_result
                if isinstance(client_connected_result, bool)
                else False
            )
        else:
            client_connected = self._probe_bool_member(
                client,
                "isConnected",
                "is_connected",
                "_is_connected",
            )
        return client_connected, is_closing

    @staticmethod
    def _probe_bool_member(owner: object | None, *member_names: str) -> bool:
        """Return first bool probe result from callable/bool members.

        Parameters
        ----------
        owner : object | None
            Object that may provide callable/bool probe members.
        *member_names : str
            Member names to probe in priority order.

        Returns
        -------
        bool
            First authoritative bool probe result; otherwise ``False``.
        """
        for _member_name, member in _iter_declared_members(owner, *member_names):
            if callable(member):
                try:
                    result = member()
                except Exception:  # noqa: BLE001 - probe remains best effort
                    _log_ble_failure(
                        _BLEFailureDisposition.COMPATIBILITY_FALLBACK,
                        "Error probing ownership member %s()",
                        _member_name,
                    )
                    continue
            else:
                result = member
            if isinstance(result, bool):
                return result
        return False

    def _get_connected_client_status(
        self,
        client: "BLEClient",
        *,
        is_closing_getter: Callable[[], bool] | None = None,
        state_connected_getter: Callable[[], bool] | None = None,
        client_connected_getter: Callable[["BLEClient"], bool] | None = None,
    ) -> tuple[bool, bool]:
        """Return ownership and closing flags with internal state locking.

        Parameters
        ----------
        client : BLEClient
            Candidate connected client to evaluate.
        is_closing_getter : Callable[[], bool] | None, optional
            Optional closing-state probe used while lock is held.
        state_connected_getter : Callable[[], bool] | None, optional
            Optional connection-state probe used while lock is held.
        client_connected_getter : Callable[[BLEClient], bool] | None, optional
            Optional client-connected probe used while lock is held.

        Returns
        -------
        tuple[bool, bool]
            ``(is_owned, is_closing)`` for ``client``.
        """
        with self._session.lock:
            return self._get_connected_client_status_locked(
                client,
                is_closing_getter=is_closing_getter,
                state_connected_getter=state_connected_getter,
                client_connected_getter=client_connected_getter,
            )

    def _verify_ownership_snapshot(
        self,
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        get_connected_client_status_locked: (
            Callable[["BLEClient"], tuple[bool, bool]] | None
        ) = None,
    ) -> _OwnershipSnapshot:
        """Capture ownership and shutdown/gate status for a connect result.

        Parameters
        ----------
        connected_client : BLEClient
            Candidate connected client under verification.
        connected_device_key : str | None
            Concrete device key used for gate ownership checks.
        connection_alias_key : str | None
            Alias gate key used during connect orchestration.
        get_connected_client_status_locked : Callable[[BLEClient], tuple[bool, bool]] | None, optional
            Optional lock-held ownership/closing probe.

        Returns
        -------
        _OwnershipSnapshot
            Snapshot used to decide whether connected publication is still safe.
        """
        iface = self._iface
        get_connected_status_locked = (
            get_connected_client_status_locked
            or self._get_connected_client_status_locked
        )
        lost_gate_ownership = iface._has_lost_gate_ownership(
            connected_device_key,
            connection_alias_key,
        )
        with self._session.lock:
            still_owned, is_closing = get_connected_status_locked(connected_client)
            prior_ever_connected = self._has_ever_connected_session()
        return _OwnershipSnapshot(
            still_owned=still_owned,
            is_closing=is_closing,
            lost_gate_ownership=lost_gate_ownership,
            prior_ever_connected=prior_ever_connected,
        )

    def _has_ever_connected_session(self) -> bool:
        """Return ``True`` when this interface published a connection.

        Notes
        -----
        This method must not acquire ``self._session.lock``. Callers such as
        ``_verify_ownership_snapshot`` invoke it while already holding that lock.
        """
        return self._session.ever_connected is True

    def _emit_verified_connection_side_effects(
        self, connected_client: "BLEClient"
    ) -> None:
        """Emit reconnect wake signal and success logging after verified publish.

        Parameters
        ----------
        connected_client : BLEClient
            Client whose verified publication succeeded.

        Returns
        -------
        None
            Always returns ``None``.
        """
        iface = self._iface
        coordinator = _get_declared_member(iface, "thread_coordinator")
        with self._session.lock:
            should_emit_reconnected = bool(self._session.prior_publish_was_reconnect)
            self._session.prior_publish_was_reconnect = False
        if should_emit_reconnected and coordinator is not None:
            self._thread_access.set_event(RECONNECTED_EVENT)
        normalized_device_address = sanitize_address(
            iface._extract_client_address(connected_client)
        )
        logger.info(
            "Connection successful to %s",
            normalized_device_address or "unknown",
        )

    @staticmethod
    def _log_gate_cleanup(connected_client: "BLEClient", *, is_closing: bool) -> None:
        """Log why gate ownership cleanup is running during connect finalization."""
        if is_closing:
            logger.debug(
                _LOG_INTERFACE_CLOSED_DURING_CONNECT,
                getattr(connected_client, "address", "unknown"),
            )
            return
        logger.debug(
            _LOG_INTERFACE_LOST_OWNERSHIP_DURING_CONNECT,
            getattr(connected_client, "address", "unknown"),
        )

    def _apply_owned_client_invalidation(
        self,
        *,
        get_is_closing: Callable[[], bool],
        restored_address: str | None,
        restore_last_connection_request: str | None,
    ) -> tuple[bool, bool, bool]:
        """Apply state mutations when the invalidated client is currently bound.

        Returns
        -------
        tuple[bool, bool, bool]
            ``(should_reset_state, should_publish_disconnect, is_closing)``
            where:
            ``should_reset_state`` indicates whether disconnected-state
            correction should run, ``should_publish_disconnect`` indicates
            whether a disconnect event should be emitted, and ``is_closing``
            indicates whether shutdown is active.
        """
        iface = self._iface
        replacement_pending = bool(self._session.client_replacement_pending)
        already_notified = bool(self._session.disconnect_notified)
        is_closing = get_is_closing() or self._session.closed
        iface.client = None
        self._session.client_publish_pending = False
        self._session.client_replacement_pending = False
        self._session.disconnect_notified = True
        should_publish_disconnect = replacement_pending and not already_notified
        if not is_closing:
            iface.address = restored_address
            iface._last_connection_request = restore_last_connection_request
            self._session.connection_alias_key = None
            return True, should_publish_disconnect, is_closing
        iface._last_connection_request = None
        return False, should_publish_disconnect, is_closing

    def _apply_publish_pending_invalidation(
        self,
        *,
        get_is_closing: Callable[[], bool],
        restored_address: str | None,
        restore_last_connection_request: str | None,
    ) -> tuple[bool, bool, bool]:
        """Apply state mutations for publish-pending invalidation branch.

        Returns
        -------
        tuple[bool, bool, bool]
            ``(should_reset_state, should_publish_disconnect, is_closing)``
            using the same semantics as `_apply_owned_client_invalidation`.
        """
        iface = self._iface
        replacement_pending = bool(self._session.client_replacement_pending)
        already_notified = bool(self._session.disconnect_notified)
        self._session.client_publish_pending = False
        self._session.client_replacement_pending = False
        should_publish_disconnect = replacement_pending and not already_notified
        if should_publish_disconnect:
            self._session.disconnect_notified = True
        is_closing = get_is_closing() or self._session.closed
        if not is_closing:
            iface.address = restored_address
            iface._last_connection_request = restore_last_connection_request
            self._session.connection_alias_key = None
            return True, should_publish_disconnect, is_closing
        iface._last_connection_request = None
        return False, should_publish_disconnect, is_closing

    def _apply_post_cleanup_state_correction(
        self,
        *,
        should_reset_state: bool,
        do_reset_to_disconnected: Callable[[], bool],
        get_current_state: Callable[[], ConnectionState],
        do_transition_to_disconnected: Callable[[], bool],
    ) -> None:
        """Ensure state converges to disconnected after invalidation cleanup.

        Parameters
        ----------
        should_reset_state : bool
            Whether state correction should be attempted.
        do_reset_to_disconnected : Callable[[], bool]
            Reset helper that attempts a direct disconnected reset.
        get_current_state : Callable[[], ConnectionState]
            Current-state probe used for diagnostics.
        do_transition_to_disconnected : Callable[[], bool]
            Transition helper used when reset fails.

        Returns
        -------
        None
            Always returns ``None``.
        """
        if not should_reset_state:
            return
        if not do_reset_to_disconnected():
            current_state = get_current_state()
            logger.error(
                "Failed to reset state after invalidated connect result (alias=%s current=%s); forcing transition to %s.",
                self._session.connection_alias_key,
                getattr(current_state, "value", current_state),
                ConnectionState.DISCONNECTED.value,
            )
            if not do_transition_to_disconnected():
                fallback_state = get_current_state()
                logger.error(
                    "Failed forced transition to %s after invalidated connect result (alias=%s current=%s).",
                    ConnectionState.DISCONNECTED.value,
                    self._session.connection_alias_key,
                    getattr(fallback_state, "value", fallback_state),
                )

    def _discard_invalidated_connected_client(
        self,
        client: "BLEClient",
        *,
        restore_address: str | None = None,
        restore_last_connection_request: str | None = None,
        is_closing_getter: Callable[[], bool] | None = None,
        reset_to_disconnected: Callable[[], bool] | None = None,
        current_state_getter: Callable[[], ConnectionState] | None = None,
        transition_to_disconnected: Callable[[], bool] | None = None,
        safe_cleanup: Callable[[Callable[[], object], str], None] | None = None,
    ) -> None:
        """Clean up a client invalidated before connect publication completes.

        Parameters
        ----------
        client : BLEClient
            Invalidated client to close and discard.
        restore_address : str | None, optional
            Address restored when invalidation occurs before shutdown.
        restore_last_connection_request : str | None, optional
            Last-request marker restored on invalidation.
        is_closing_getter : Callable[[], bool] | None, optional
            Optional closing-state probe.
        reset_to_disconnected : Callable[[], bool] | None, optional
            Optional state reset helper.
        current_state_getter : Callable[[], ConnectionState] | None, optional
            Optional state probe used by reset/transition fallback.
        transition_to_disconnected : Callable[[], bool] | None, optional
            Optional transition helper.
        safe_cleanup : Callable[[Callable[[], object], str], None] | None, optional
            Optional safe-cleanup wrapper for client close.

        Returns
        -------
        None
            Always returns ``None``.
        """
        iface = self._iface
        get_is_closing = is_closing_getter or self._state_access.is_closing
        do_reset_to_disconnected = reset_to_disconnected or (
            self._state_access.reset_to_disconnected
        )
        get_current_state = current_state_getter or self._state_access.current_state
        do_transition_to_disconnected = transition_to_disconnected or (
            lambda: self._state_access.transition_to(ConnectionState.DISCONNECTED)
        )
        run_safe_cleanup = safe_cleanup or self._error_access.safe_cleanup
        restored_address = (
            restore_address.strip()
            if restore_address is not None and restore_address.strip()
            else None
        )
        should_reset_state = False
        should_publish_disconnect = False
        is_closing = False
        disconnect_session_epoch = 0
        with self._session.lock:
            disconnect_session_epoch = self._session.connection_session_epoch
            inflight_client = self._session.connected_publish_inflight_client
            if iface.client is client:
                if inflight_client is client:
                    self._session.connected_publish_inflight_client = None
                (
                    should_reset_state,
                    should_publish_disconnect,
                    is_closing,
                ) = self._apply_owned_client_invalidation(
                    get_is_closing=get_is_closing,
                    restored_address=restored_address,
                    restore_last_connection_request=restore_last_connection_request,
                )
            elif (
                iface.client is None
                and self._session.client_publish_pending
                and inflight_client is client
            ):
                self._session.connected_publish_inflight_client = None
                (
                    should_reset_state,
                    should_publish_disconnect,
                    is_closing,
                ) = self._apply_publish_pending_invalidation(
                    get_is_closing=get_is_closing,
                    restored_address=restored_address,
                    restore_last_connection_request=restore_last_connection_request,
                )

        try:
            run_safe_cleanup(
                lambda: iface._client_manager_safe_close_client(client),
                "BLE client close for invalidated connection result",
            )
        finally:
            with self._session.lock:
                same_session = (
                    self._session.connection_session_epoch == disconnect_session_epoch
                )
            if same_session:
                self._apply_post_cleanup_state_correction(
                    should_reset_state=should_reset_state,
                    do_reset_to_disconnected=do_reset_to_disconnected,
                    get_current_state=get_current_state,
                    do_transition_to_disconnected=do_transition_to_disconnected,
                )
        if should_publish_disconnect and not is_closing:
            with self._session.lock:
                publish_disconnect = (
                    self._session.connection_session_epoch == disconnect_session_epoch
                )
            if publish_disconnect:
                iface._disconnected()

    def _verify_and_publish_connected(
        self,
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        restore_address: str | None,
        restore_last_connection_request: str | None,
        verify_ownership_snapshot: (
            Callable[["BLEClient", str | None, str | None], _OwnershipSnapshot] | None
        ) = None,
        get_connected_client_status_locked: (
            Callable[["BLEClient"], tuple[bool, bool]] | None
        ) = None,
    ) -> None:
        """Publish connected state only when ownership is still valid.

        Parameters
        ----------
        connected_client : BLEClient
            Candidate connected client under verification.
        connected_device_key : str | None
            Concrete device key used by gate ownership checks.
        connection_alias_key : str | None
            Alias gate key associated with the connect attempt.
        restore_address : str | None
            Address restored if connect publication is invalidated.
        restore_last_connection_request : str | None
            Last-request marker restored if publication is invalidated.
        verify_ownership_snapshot : Callable[[BLEClient, str | None, str | None], _OwnershipSnapshot] | None, optional
            Optional snapshot provider override.
        get_connected_client_status_locked : Callable[[BLEClient], tuple[bool, bool]] | None, optional
            Optional lock-held ownership probe override.

        Returns
        -------
        None
            Always returns ``None``.
        """
        iface = self._iface

        def _raise_invalidated(snapshot: _OwnershipSnapshot) -> None:
            iface._raise_for_invalidated_connect_result(
                connected_client,
                connected_device_key,
                connection_alias_key,
                is_closing=snapshot.is_closing,
                lost_gate_ownership=snapshot.lost_gate_ownership,
                restore_address=restore_address,
                restore_last_connection_request=restore_last_connection_request,
            )

        snapshot_provider = verify_ownership_snapshot or (
            lambda client, device_key, alias_key: self._verify_ownership_snapshot(
                client,
                device_key,
                alias_key,
                get_connected_client_status_locked=get_connected_client_status_locked,
            )
        )
        get_connected_status_locked = (
            get_connected_client_status_locked
            or self._get_connected_client_status_locked
        )

        snapshot = snapshot_provider(
            connected_client,
            connected_device_key,
            connection_alias_key,
        )
        if (
            not snapshot.still_owned
            or snapshot.is_closing
            or snapshot.lost_gate_ownership
        ):
            _raise_invalidated(snapshot)
        prior_ever_connected = snapshot.prior_ever_connected

        should_publish_connected = False
        publish_claimed = False
        duplicate_publish_request = False
        with self._session.lock:
            still_owned, is_closing = get_connected_status_locked(connected_client)
            if still_owned and not is_closing:
                publish_pending = self._session.client_publish_pending
                inflight_client = self._session.connected_publish_inflight_client
                if not publish_pending:
                    self._session.client_publish_pending = True
                    self._session.connected_publish_inflight_client = connected_client
                    publish_claimed = True
                    should_publish_connected = True
                elif iface.client is connected_client and inflight_client is None:
                    # The connect flow may have already claimed publish-pending
                    # for this exact client before reaching verification.
                    self._session.connected_publish_inflight_client = connected_client
                    publish_claimed = True
                    should_publish_connected = True
                elif (
                    iface.client is connected_client
                    and inflight_client is connected_client
                ):
                    duplicate_publish_request = True
        if duplicate_publish_request:
            logger.debug(
                "Skipping duplicate connected publication attempt for active client."
            )
            return
        snapshot = snapshot_provider(
            connected_client,
            connected_device_key,
            connection_alias_key,
        )
        if not should_publish_connected:
            _raise_invalidated(snapshot)
        publish_committed = False
        if should_publish_connected:
            with self._session.lock:
                still_owned, is_closing = get_connected_status_locked(connected_client)
                if (
                    publish_claimed
                    and snapshot.still_owned
                    and not snapshot.is_closing
                    and not snapshot.lost_gate_ownership
                    and still_owned
                    and not is_closing
                ):
                    publish_committed = True
            if publish_committed:
                self._commit_and_publish_connected(
                    connected_client=connected_client,
                    connected_device_key=connected_device_key,
                    connection_alias_key=connection_alias_key,
                    snapshot_provider=snapshot_provider,
                    get_connected_status_locked=get_connected_status_locked,
                    prior_ever_connected=prior_ever_connected,
                    raise_invalidated=_raise_invalidated,
                )
                return

        if publish_claimed:
            with self._session.lock:
                if self._session.connected_publish_inflight_client is connected_client:
                    self._session.connected_publish_inflight_client = None
        post_check_snapshot = snapshot_provider(
            connected_client,
            connected_device_key,
            connection_alias_key,
        )
        _raise_invalidated(post_check_snapshot)

    def _commit_and_publish_connected(
        self,
        *,
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
        snapshot_provider: Callable[
            ["BLEClient", str | None, str | None], _OwnershipSnapshot
        ],
        get_connected_status_locked: Callable[["BLEClient"], tuple[bool, bool]],
        prior_ever_connected: bool,
        raise_invalidated: Callable[[_OwnershipSnapshot], None],
    ) -> None:
        """Run the committed connected-publication sequence.

        Parameters
        ----------
        connected_client : BLEClient
            Client whose verified publication is being committed.
        connected_device_key : str | None
            Concrete device key associated with ``connected_client``.
        connection_alias_key : str | None
            Alias gate key associated with ``connected_client``.
        snapshot_provider : Callable[[BLEClient, str | None, str | None], _OwnershipSnapshot]
            Provider used for ownership snapshot verification.
        get_connected_status_locked : Callable[[BLEClient], tuple[bool, bool]]
            Lock-held ownership/closing status probe.
        prior_ever_connected : bool
            Prior session publication marker used for reconnect side effects.
        raise_invalidated : Callable[[_OwnershipSnapshot], None]
            Callback that raises when ownership snapshots invalidate publication.

        Returns
        -------
        None
            Always returns ``None``.
        """
        iface = self._iface
        still_owned_after = True
        is_closing_after = False
        disconnect_notified = False
        published_session_epoch = 0
        publish_completed = False
        try:
            post_commit_snapshot = snapshot_provider(
                connected_client,
                connected_device_key,
                connection_alias_key,
            )
            if (
                not post_commit_snapshot.still_owned
                or post_commit_snapshot.is_closing
                or post_commit_snapshot.lost_gate_ownership
            ):
                raise_invalidated(post_commit_snapshot)
            publish_allowed = False
            with self._session.lock:
                published_session_epoch = self._session.connection_session_epoch
                publish_allowed = iface.client is connected_client
            if not publish_allowed:
                stale_snapshot = snapshot_provider(
                    connected_client,
                    connected_device_key,
                    connection_alias_key,
                )
                raise_invalidated(stale_snapshot)
            with self._session.lock:
                publish_allowed = (
                    iface.client is connected_client
                    and self._session.connection_session_epoch
                    == published_session_epoch
                )
            if not publish_allowed:
                stale_snapshot = snapshot_provider(
                    connected_client,
                    connected_device_key,
                    connection_alias_key,
                )
                raise_invalidated(stale_snapshot)
            connected_notifier = iface._connected
            try:
                connected_notifier(expected_session_epoch=published_session_epoch)
            except TypeError as error:
                error_message = str(error)
                if (
                    "unexpected keyword argument" not in error_message
                    or "expected_session_epoch" not in error_message
                ):
                    raise
                with self._session.lock:
                    fallback_allowed = (
                        iface.client is connected_client
                        and self._session.connection_session_epoch
                        == published_session_epoch
                    )
                if not fallback_allowed:
                    stale_snapshot = snapshot_provider(
                        connected_client,
                        connected_device_key,
                        connection_alias_key,
                    )
                    raise_invalidated(stale_snapshot)
                connected_notifier()
            with self._session.lock:
                publish_completed = (
                    iface.client is connected_client
                    and self._session.connection_session_epoch
                    == published_session_epoch
                )
                if publish_completed:
                    self._session.ever_connected = True
                    self._session.prior_publish_was_reconnect = prior_ever_connected
            if publish_completed:
                self._emit_verified_connection_side_effects(connected_client)
        finally:
            with self._session.lock:
                if self._session.connected_publish_inflight_client is connected_client:
                    self._session.connected_publish_inflight_client = None
                if iface.client is connected_client:
                    self._session.client_publish_pending = False
                    if publish_completed:
                        self._session.client_replacement_pending = False
                still_owned_after, is_closing_after = get_connected_status_locked(
                    connected_client
                )
                disconnect_notified = self._session.disconnect_notified
        if (
            publish_completed
            and not still_owned_after
            and disconnect_notified
            and not is_closing_after
        ):
            logger.debug(
                "Connected publication raced with disconnect; emitting compensating disconnect event."
            )
            with self._session.lock:
                same_session = (
                    self._session.connection_session_epoch == published_session_epoch
                )
            if same_session:
                iface._disconnected()

    def _finalize_connection_gates(
        self,
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        get_connected_client_status: (
            Callable[["BLEClient"], tuple[bool, bool]] | None
        ) = None,
        get_connected_client_status_locked: (
            Callable[["BLEClient"], tuple[bool, bool]] | None
        ) = None,
    ) -> None:
        """Finalize address-gate ownership after successful connection.

        Parameters
        ----------
        connected_client : BLEClient
            Client that completed verified connected publication.
        connected_device_key : str | None
            Concrete device key bound to ``connected_client``.
        connection_alias_key : str | None
            Alias gate key associated with ``connected_client``.
        get_connected_client_status : Callable[[BLEClient], tuple[bool, bool]] | None, optional
            Optional unlocked ownership probe override.
        get_connected_client_status_locked : Callable[[BLEClient], tuple[bool, bool]] | None, optional
            Optional lock-held ownership probe override.

        Returns
        -------
        None
            Always returns ``None``.
        """
        iface = self._iface
        get_status = get_connected_client_status or self._get_connected_client_status
        get_status_locked = (
            get_connected_client_status_locked
            or self._get_connected_client_status_locked
        )
        still_active, is_closing = get_status(connected_client)

        if still_active:
            should_clear_gate_keys = False
            with self._session.lock:
                still_active, is_closing = get_status_locked(connected_client)
                if still_active:
                    self._session.connection_alias_key = connection_alias_key
                else:
                    active_client = iface.client
                    owns_alias = (
                        self._session.connection_alias_key == connection_alias_key
                    )
                    should_clear_gate_keys = owns_alias and (
                        active_client is connected_client or active_client is None
                    )
                    if should_clear_gate_keys:
                        self._session.connection_alias_key = None
            if not still_active:
                self._log_gate_cleanup(connected_client, is_closing=is_closing)
                if should_clear_gate_keys:
                    iface._mark_address_keys_disconnected(
                        connected_device_key, connection_alias_key
                    )
                return

            iface._mark_address_keys_connected(
                connected_device_key, connection_alias_key
            )
            needs_cleanup = False
            should_clear_gate_keys = False
            with self._session.lock:
                still_active, is_closing = get_status_locked(connected_client)
                if not still_active:
                    self._log_gate_cleanup(connected_client, is_closing=is_closing)
                    active_client = iface.client
                    owns_alias = (
                        self._session.connection_alias_key == connection_alias_key
                    )
                    should_clear_gate_keys = owns_alias and (
                        active_client is connected_client or active_client is None
                    )
                    if should_clear_gate_keys:
                        self._session.connection_alias_key = None
                    needs_cleanup = True
            if needs_cleanup and should_clear_gate_keys:
                iface._mark_address_keys_disconnected(
                    connected_device_key, connection_alias_key
                )
        elif is_closing:
            logger.debug(
                "Skipping connect gate marking during shutdown for stale client result (%s).",
                getattr(connected_client, "address", "unknown"),
            )
        else:
            logger.debug(
                "Skipping connect gate marking for client result that lost ownership (%s).",
                getattr(connected_client, "address", "unknown"),
            )

    def _is_owned_connected_client(self, client: "BLEClient") -> bool:
        """Return whether interface still owns the provided connected client.

        Parameters
        ----------
        client : BLEClient
            Client instance to evaluate.

        Returns
        -------
        bool
            ``True`` when this interface still owns ``client``.
        """
        is_owned, _ = self._get_connected_client_status(client)
        return is_owned
