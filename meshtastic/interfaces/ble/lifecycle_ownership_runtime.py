"""Connection ownership lifecycle coordinator runtime ownership for BLE."""

from collections.abc import Callable
from typing import TYPE_CHECKING

from meshtastic.interfaces.ble.constants import RECONNECTED_EVENT, logger
from meshtastic.interfaces.ble.lifecycle_primitives import (
    _LifecycleErrorAccess,
    _LifecycleStateAccess,
    _LifecycleThreadAccess,
    _OwnershipSnapshot,
)
from meshtastic.interfaces.ble.state import ConnectionState
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_member,
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
    """Own verified-connection publication and ownership/finalization behavior."""

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind connection ownership coordination to a specific interface."""
        self._iface = iface
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
        """Return ownership and closing flags for `client` while holding lock."""
        iface = self._iface
        get_is_closing = is_closing_getter or self._state_access.is_closing
        get_state_connected = state_connected_getter or self._state_access.is_connected
        get_client_connected = (
            client_connected_getter or self._state_access.client_is_connected
        )
        is_closing = get_is_closing() or iface._closed
        state_connected = get_state_connected()
        client_connected = get_client_connected(client)
        is_owned = (
            not iface._closed
            and iface.client is client
            and state_connected
            and client_connected
        )
        return is_owned, is_closing

    def _get_connected_client_status(
        self,
        client: "BLEClient",
        *,
        is_closing_getter: Callable[[], bool] | None = None,
        state_connected_getter: Callable[[], bool] | None = None,
        client_connected_getter: Callable[["BLEClient"], bool] | None = None,
    ) -> tuple[bool, bool]:
        """Return ownership and closing flags with internal state locking."""
        with self._iface._state_lock:
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
        """Capture connect-result ownership snapshot for this interface."""
        iface = self._iface
        get_connected_status_locked = (
            get_connected_client_status_locked
            or self._get_connected_client_status_locked
        )
        lost_gate_ownership = iface._has_lost_gate_ownership(
            connected_device_key,
            connection_alias_key,
        )
        with iface._state_lock:
            still_owned, is_closing = get_connected_status_locked(connected_client)
            prior_ever_connected = self._has_ever_connected_session()
        return _OwnershipSnapshot(
            still_owned=still_owned,
            is_closing=is_closing,
            lost_gate_ownership=lost_gate_ownership,
            prior_ever_connected=prior_ever_connected,
        )

    def _has_ever_connected_session(self) -> bool:
        """Return mock-safe `True` when this interface published a connection."""
        raw_ever_connected = getattr(self._iface, "_ever_connected", False)
        if _is_unconfigured_mock_member(raw_ever_connected):
            return False
        return raw_ever_connected is True

    def _emit_verified_connection_side_effects(
        self, connected_client: "BLEClient"
    ) -> None:
        """Emit reconnect wake signal and success logging after verified publish."""
        iface = self._iface
        coordinator = getattr(iface, "thread_coordinator", None)
        if iface._prior_publish_was_reconnect and coordinator is not None:
            self._thread_access.set_event(RECONNECTED_EVENT)
        iface._prior_publish_was_reconnect = False
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

    @staticmethod
    def _apply_owned_client_invalidation(
        iface: "BLEInterface",
        *,
        get_is_closing: Callable[[], bool],
        restored_address: str | None,
        restore_last_connection_request: str | None,
    ) -> tuple[bool, bool, bool]:
        """Apply state mutations when the invalidated client is currently bound."""
        replacement_pending = bool(getattr(iface, "_client_replacement_pending", False))
        already_notified = bool(getattr(iface, "_disconnect_notified", False))
        is_closing = get_is_closing() or iface._closed
        iface.client = None
        iface._client_publish_pending = False
        iface._client_replacement_pending = False
        iface._disconnect_notified = True
        should_publish_disconnect = replacement_pending and not already_notified
        if not is_closing:
            iface.address = restored_address
            iface._last_connection_request = restore_last_connection_request
            iface._connection_alias_key = None
            return True, should_publish_disconnect, is_closing
        iface._last_connection_request = None
        return False, should_publish_disconnect, is_closing

    @staticmethod
    def _apply_publish_pending_invalidation(
        iface: "BLEInterface",
        *,
        get_is_closing: Callable[[], bool],
        restored_address: str | None,
        restore_last_connection_request: str | None,
    ) -> tuple[bool, bool, bool]:
        """Apply state mutations for publish-pending invalidation branch."""
        replacement_pending = bool(getattr(iface, "_client_replacement_pending", False))
        already_notified = bool(getattr(iface, "_disconnect_notified", False))
        iface._client_publish_pending = False
        iface._client_replacement_pending = False
        should_publish_disconnect = replacement_pending and not already_notified
        if should_publish_disconnect:
            iface._disconnect_notified = True
        is_closing = get_is_closing() or iface._closed
        if not is_closing:
            iface.address = restored_address
            iface._last_connection_request = restore_last_connection_request
            iface._connection_alias_key = None
            return True, should_publish_disconnect, is_closing
        iface._last_connection_request = None
        return False, should_publish_disconnect, is_closing

    @staticmethod
    def _apply_post_cleanup_state_correction(
        iface: "BLEInterface",
        *,
        should_reset_state: bool,
        do_reset_to_disconnected: Callable[[], bool],
        get_current_state: Callable[[], ConnectionState],
        do_transition_to_disconnected: Callable[[], bool],
    ) -> None:
        """Ensure state converges to disconnected after invalidation cleanup."""
        if not should_reset_state:
            return
        with iface._state_lock:
            if not do_reset_to_disconnected():
                current_state = get_current_state()
                logger.error(
                    "Failed to reset state after invalidated connect result (alias=%s current=%s); forcing transition to %s.",
                    iface._connection_alias_key,
                    getattr(current_state, "value", current_state),
                    ConnectionState.DISCONNECTED.value,
                )
                if not do_transition_to_disconnected():
                    fallback_state = get_current_state()
                    logger.error(
                        "Failed forced transition to %s after invalidated connect result (alias=%s current=%s).",
                        ConnectionState.DISCONNECTED.value,
                        iface._connection_alias_key,
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
        """Clean up a client invalidated before connect publication completes."""
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
        with iface._state_lock:
            if iface.client is client:
                (
                    should_reset_state,
                    should_publish_disconnect,
                    is_closing,
                ) = self._apply_owned_client_invalidation(
                    iface,
                    get_is_closing=get_is_closing,
                    restored_address=restored_address,
                    restore_last_connection_request=restore_last_connection_request,
                )
            elif iface.client is None and bool(
                getattr(iface, "_client_publish_pending", False)
            ):
                (
                    should_reset_state,
                    should_publish_disconnect,
                    is_closing,
                ) = self._apply_publish_pending_invalidation(
                    iface,
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
            self._apply_post_cleanup_state_correction(
                iface,
                should_reset_state=should_reset_state,
                do_reset_to_disconnected=do_reset_to_disconnected,
                get_current_state=get_current_state,
                do_transition_to_disconnected=do_transition_to_disconnected,
            )
        if should_publish_disconnect and not is_closing:
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
        """Publish connected state only when ownership is still valid."""
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
        with iface._state_lock:
            still_owned, is_closing = get_connected_status_locked(connected_client)
            if still_owned and not is_closing and not iface._client_publish_pending:
                iface._client_publish_pending = True
                iface._client_replacement_pending = False
            if still_owned and not is_closing:
                should_publish_connected = True
        snapshot = snapshot_provider(
            connected_client,
            connected_device_key,
            connection_alias_key,
        )
        publish_committed = False
        if should_publish_connected:
            with iface._state_lock:
                still_owned, is_closing = get_connected_status_locked(connected_client)
                if (
                    snapshot.still_owned
                    and not snapshot.is_closing
                    and not snapshot.lost_gate_ownership
                    and still_owned
                    and not is_closing
                ):
                    publish_committed = True
            if publish_committed:
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
                    _raise_invalidated(post_commit_snapshot)
                iface._connected()
                with iface._state_lock:
                    iface._ever_connected = True
                    iface._prior_publish_was_reconnect = prior_ever_connected
                self._emit_verified_connection_side_effects(connected_client)
                with iface._state_lock:
                    if iface.client is connected_client:
                        iface._client_publish_pending = False
                        iface._client_replacement_pending = False
                    still_owned_after, is_closing_after = get_connected_status_locked(
                        connected_client
                    )
                    disconnect_notified = iface._disconnect_notified
                if (
                    not still_owned_after
                    and disconnect_notified
                    and not is_closing_after
                ):
                    logger.debug(
                        "Connected publication raced with disconnect; emitting compensating disconnect event."
                    )
                    iface._disconnected()
                return

        _raise_invalidated(snapshot)

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
        """Finalize address-gate ownership after successful connection."""
        iface = self._iface
        get_status = get_connected_client_status or self._get_connected_client_status
        get_status_locked = (
            get_connected_client_status_locked
            or self._get_connected_client_status_locked
        )
        still_active, is_closing = get_status(connected_client)

        if still_active:
            with iface._state_lock:
                still_active, is_closing = get_status_locked(connected_client)
                if still_active:
                    iface._connection_alias_key = connection_alias_key
                else:
                    iface._connection_alias_key = None
            if not still_active:
                self._log_gate_cleanup(connected_client, is_closing=is_closing)
                iface._mark_address_keys_disconnected(
                    connected_device_key, connection_alias_key
                )
                return

            iface._mark_address_keys_connected(
                connected_device_key, connection_alias_key
            )
            needs_cleanup = False
            with iface._state_lock:
                still_active, is_closing = get_status_locked(connected_client)
                if not still_active:
                    self._log_gate_cleanup(connected_client, is_closing=is_closing)
                    iface._connection_alias_key = None
                    needs_cleanup = True
            if needs_cleanup:
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
        """Return whether interface still owns the provided connected client."""
        is_owned, _ = self._get_connected_client_status(client)
        return is_owned
