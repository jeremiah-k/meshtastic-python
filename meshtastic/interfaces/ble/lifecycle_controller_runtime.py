"""Top-level lifecycle controller runtime ownership for BLE."""

from collections.abc import Callable
from typing import TYPE_CHECKING

from bleak import BleakClient as BleakRootClient

from meshtastic.interfaces.ble.lifecycle_disconnect_runtime import (
    BLEDisconnectLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_compat_service import (
    _ORIGINAL_FINALIZE_CONNECTION_GATES,
    _ORIGINAL_GET_CONNECTED_CLIENT_STATUS,
    _ORIGINAL_GET_CONNECTED_CLIENT_STATUS_LOCKED,
    _ORIGINAL_IS_OWNED_CONNECTED_CLIENT,
    _ORIGINAL_VERIFY_OWNERSHIP_SNAPSHOT,
)
from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
    BLEConnectionOwnershipLifecycleCoordinator,
)
from meshtastic.interfaces.ble.lifecycle_primitives import _OwnershipSnapshot
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


class BLELifecycleController:
    """Instance-bound collaborator for BLE lifecycle responsibilities."""

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind lifecycle orchestration helpers to a specific interface."""
        self._iface = iface
        self._receive = BLEReceiveLifecycleCoordinator(iface)
        self._disconnect = BLEDisconnectLifecycleCoordinator(iface)
        self._connection_ownership = BLEConnectionOwnershipLifecycleCoordinator(iface)
        self._shutdown = BLEShutdownLifecycleCoordinator(iface)

    def set_receive_wanted(self, *, want_receive: bool) -> None:
        """Request or clear the receive loop on the bound interface."""
        self._receive.set_receive_wanted(want_receive=want_receive)

    def should_run_receive_loop(self) -> bool:
        """Return whether receive loop should continue for the bound interface."""
        return self._receive.should_run_receive_loop()

    def start_receive_thread(self, *, name: str, reset_recovery: bool = True) -> None:
        """Start receive thread for the bound interface."""
        self._receive.start_receive_thread(
            name=name,
            reset_recovery=reset_recovery,
        )

    def handle_disconnect(
        self,
        source: str,
        *,
        client: "BLEClient | None" = None,
        bleak_client: BleakRootClient | None = None,
    ) -> bool:
        """Handle disconnect sequence for the bound interface."""
        return self._disconnect.handle_disconnect(
            source,
            client=client,
            bleak_client=bleak_client,
        )

    def on_ble_disconnect(self, client: BleakRootClient) -> None:
        """Handle Bleak disconnect callback for the bound interface."""
        self._disconnect.on_ble_disconnect(client)

    def schedule_auto_reconnect(self) -> None:
        """Schedule reconnect worker for the bound interface."""
        self._disconnect.schedule_auto_reconnect()

    def verify_and_publish_connected(
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
        """Verify ownership and publish connected side effects."""
        if self._uses_compat_connection_status_overrides():
            from meshtastic.interfaces.ble import (
                lifecycle_service as lifecycle_service_mod,
            )

            if verify_ownership_snapshot is None:

                def _compat_verify_snapshot(
                    client: "BLEClient",
                    device_key: str | None,
                    alias_key: str | None,
                ) -> _OwnershipSnapshot:
                    return (
                        lifecycle_service_mod.BLELifecycleService._verify_ownership_snapshot(  # noqa: E501
                            self._iface,
                            client,
                            device_key,
                            alias_key,
                        )
                    )

                verify_ownership_snapshot = _compat_verify_snapshot
            if get_connected_client_status_locked is None:

                def _compat_get_status_locked(
                    client: "BLEClient",
                ) -> tuple[bool, bool]:
                    return lifecycle_service_mod.BLELifecycleService._get_connected_client_status_locked(  # noqa: E501
                        self._iface,
                        client,
                    )

                get_connected_client_status_locked = _compat_get_status_locked
        self._connection_ownership._verify_and_publish_connected(
            connected_client,
            connected_device_key,
            connection_alias_key,
            restore_address=restore_address,
            restore_last_connection_request=restore_last_connection_request,
            verify_ownership_snapshot=verify_ownership_snapshot,
            get_connected_client_status_locked=get_connected_client_status_locked,
        )

    def emit_verified_connection_side_effects(
        self, connected_client: "BLEClient"
    ) -> None:
        """Emit verified-connection side effects for the bound interface."""
        self._connection_ownership._emit_verified_connection_side_effects(
            connected_client
        )

    def discard_invalidated_connected_client(
        self,
        client: "BLEClient",
        *,
        restore_address: str | None = None,
        restore_last_connection_request: str | None = None,
    ) -> None:
        """Discard stale connect result for the bound interface."""
        from meshtastic.interfaces.ble import (
            lifecycle_service as lifecycle_service_mod,
        )

        iface = self._iface
        self._connection_ownership._discard_invalidated_connected_client(
            client,
            restore_address=restore_address,
            restore_last_connection_request=restore_last_connection_request,
            is_closing_getter=lambda: lifecycle_service_mod.BLELifecycleService._state_manager_is_closing(  # noqa: E501
                iface
            ),
            reset_to_disconnected=lambda: lifecycle_service_mod.BLELifecycleService._state_manager_reset_to_disconnected(  # noqa: E501
                iface
            ),
            current_state_getter=lambda: lifecycle_service_mod.BLELifecycleService._state_manager_current_state(  # noqa: E501
                iface
            ),
            transition_to_disconnected=lambda: lifecycle_service_mod.BLELifecycleService._state_manager_transition_to(  # noqa: E501
                iface,
                ConnectionState.DISCONNECTED,
            ),
            safe_cleanup=lambda cleanup, operation_name: lifecycle_service_mod.BLELifecycleService._error_handler_safe_cleanup(  # noqa: E501
                iface,
                cleanup,
                operation_name,
            ),
        )

    def _uses_compat_connection_status_overrides(self) -> bool:
        """Return whether lifecycle service status helpers were monkeypatched."""
        from meshtastic.interfaces.ble import lifecycle_service as lifecycle_service_mod

        service_get_status = (
            lifecycle_service_mod.BLELifecycleService._get_connected_client_status
        )
        service_get_status_locked = (
            lifecycle_service_mod.BLELifecycleService._get_connected_client_status_locked
        )
        service_verify_snapshot = (
            lifecycle_service_mod.BLELifecycleService._verify_ownership_snapshot
        )
        service_finalize_connection_gates = (
            lifecycle_service_mod.BLELifecycleService._finalize_connection_gates
        )
        service_is_owned_connected_client = (
            lifecycle_service_mod.BLELifecycleService._is_owned_connected_client
        )
        return (
            service_get_status is not _ORIGINAL_GET_CONNECTED_CLIENT_STATUS
            or service_get_status_locked
            is not _ORIGINAL_GET_CONNECTED_CLIENT_STATUS_LOCKED
            or service_verify_snapshot is not _ORIGINAL_VERIFY_OWNERSHIP_SNAPSHOT
            or service_finalize_connection_gates is not _ORIGINAL_FINALIZE_CONNECTION_GATES
            or service_is_owned_connected_client
            is not _ORIGINAL_IS_OWNED_CONNECTED_CLIENT
        )

    def finalize_connection_gates(
        self,
        connected_client: "BLEClient",
        connected_device_key: str | None,
        connection_alias_key: str | None,
    ) -> None:
        """Finalize gate ownership after successful connect."""
        if self._uses_compat_connection_status_overrides():
            from meshtastic.interfaces.ble import (
                lifecycle_service as lifecycle_service_mod,
            )

            lifecycle_service_mod.BLELifecycleService._finalize_connection_gates(
                self._iface,
                connected_client,
                connected_device_key,
                connection_alias_key,
            )
            return
        self._connection_ownership._finalize_connection_gates(
            connected_client,
            connected_device_key,
            connection_alias_key,
        )

    def is_owned_connected_client(self, client: "BLEClient") -> bool:
        """Return whether the bound interface still owns the provided client."""
        if self._uses_compat_connection_status_overrides():
            from meshtastic.interfaces.ble import (
                lifecycle_service as lifecycle_service_mod,
            )

            return lifecycle_service_mod.BLELifecycleService._is_owned_connected_client(
                self._iface, client
            )
        return self._connection_ownership._is_owned_connected_client(client)

    def has_ever_connected_session(self) -> bool:
        """Return whether this interface has published at least one connection."""
        return self._connection_ownership._has_ever_connected_session()

    def is_connection_closing(self) -> bool:
        """Return whether shutdown is in progress for the bound interface."""
        return self._shutdown.is_connection_closing()

    def close(
        self,
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
    ) -> None:
        """Close the bound interface and lifecycle resources."""
        self._shutdown.close(
            management_shutdown_wait_timeout=management_shutdown_wait_timeout,
            management_wait_poll_seconds=management_wait_poll_seconds,
        )

    def disconnect_and_close_client(self, client: "BLEClient") -> None:
        """Disconnect and close the provided client for the bound interface."""
        self._disconnect.disconnect_and_close_client(client)
